"""Owner runtime overrides of role assignment and routing order.

Set from the Telegram "⚙️ Phân công" screen (gated by `telegram.allow_config_changes`).
They are standing configuration and take precedence over the YAML config and the
project manifest:

  project (target = slug): workers   — ONE ordered list: item 1 is the default worker, the
                                       rest are its fallbacks in order;
                           reviewers — ONE ordered list: item 1 is the preferred reviewer.
  agent   (target = id):   enabled, max_concurrency (and legacy fallback_to, still honoured).

When a project has a role-order override, the quota fallback for that role is simply the
next admitted agent below in the same list (wrapping to the ones above); see
`Engine._pick_agent`. Without one, the manifest order plus each agent's `fallback_to` from the
YAML apply as before. Legacy `default_worker` overrides are still read (moved to the front).

Project overrides are applied when the engine loads a manifest (`apply_project`). Agent
`enabled` / `max_concurrency` are written to the `agents` row and re-applied after every
`sync_agents()`. Every change keeps a snapshot in `override_history` for ↩️ Hoàn tác and is
audited. Hard safety policy (hardware no-go, protected branches, adapter model guards) is not
configurable here.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from ..db import audit, jdump, jload, now_iso, row, rows, tx

PROJECT_KEYS = ("workers", "reviewers")
LEGACY_PROJECT_KEYS = ("default_worker",)
AGENT_KEYS = ("fallback_to", "enabled", "max_concurrency")
MAX_CONCURRENCY = 8
ROLE_KEY = {"worker": "workers", "reviewer": "reviewers"}


def get(conn: sqlite3.Connection, scope: str, target: str) -> dict[str, Any]:
    return {r["key"]: jload(r["value_json"]) for r in rows(
        conn, "SELECT key, value_json FROM overrides WHERE scope=? AND target=?", scope, target)}


def all_agent_overrides(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in rows(conn, "SELECT target, key, value_json FROM overrides WHERE scope='agent'"):
        out.setdefault(r["target"], {})[r["key"]] = jload(r["value_json"])
    return out


# ---------------------------------------------------------------- effective values
def _code_list(ov: dict, default_worker: str, workers: list[str]) -> list[str]:
    code = list(ov["workers"]) if "workers" in ov else list(dict.fromkeys([default_worker, *workers]))
    dw = ov.get("default_worker")          # legacy override: that agent first
    if dw:
        code = [dw] + [a for a in code if a != dw]
    return code


def apply_project(conn: sqlite3.Connection, m):
    """The manifest with the project's overrides applied (in place; returns it)."""
    ov = get(conn, "project", m.project.slug)
    if "workers" in ov or "default_worker" in ov:
        code = _code_list(ov, m.project.default_worker, m.project.workers)
        m.project.default_worker, m.project.workers = code[0], code
    if "reviewers" in ov:
        m.project.reviewers = list(ov["reviewers"])
    return m


def has_role_order(conn: sqlite3.Connection, slug: str, role: str) -> bool:
    """True when the owner set this role's order for the project (fallback then follows the list)."""
    ov = get(conn, "project", slug)
    return ("reviewers" in ov) if role == "reviewer" else ("workers" in ov or "default_worker" in ov)


def fallback_chain(conn: sqlite3.Connection, agent_id: str, cfg: dict | None = None) -> list[str]:
    ov = get(conn, "agent", agent_id)
    if "fallback_to" in ov:
        return list(ov["fallback_to"])
    if cfg is None:
        r = row(conn, "SELECT config_json FROM agents WHERE id=?", agent_id)
        cfg = jload(r["config_json"], {}) if r else {}
    return list(cfg.get("fallback_to") or [])


def apply_agent_rows(conn: sqlite3.Connection) -> None:
    """Write enabled / max_concurrency overrides onto the agents table (after a config sync)."""
    for aid, ov in all_agent_overrides(conn).items():
        if "enabled" in ov:
            conn.execute("UPDATE agents SET enabled=? WHERE id=?", (int(bool(ov["enabled"])), aid))
        if "max_concurrency" in ov:
            conn.execute("UPDATE agents SET max_concurrency=? WHERE id=?", (int(ov["max_concurrency"]), aid))


def _agents(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["id"]: {"enabled": bool(r["enabled"]), "roles": jload(r["roles_json"], [])}
            for r in rows(conn, "SELECT id, enabled, roles_json FROM agents")}


def eligible(conn: sqlite3.Connection, role: str) -> list[str]:
    """Enabled identities that have `role`, sorted."""
    return sorted(a for a, v in _agents(conn).items() if v["enabled"] and role in v["roles"])


def effective_project(conn: sqlite3.Connection, slug: str) -> dict[str, Any]:
    """The ordered CODE (workers) and REVIEW lists after overrides, plus the read-only Tech Lead."""
    from ..policies.manifest import Manifest
    r = row(conn, "SELECT manifest_json FROM projects WHERE slug=?", slug)
    if r is None:
        raise ValueError(f"unknown project {slug!r}")
    m = Manifest.model_validate_json(r["manifest_json"])
    ov = get(conn, "project", slug)
    return {"workers": _code_list(ov, m.project.default_worker, m.project.workers),
            "reviewers": list(ov.get("reviewers", m.project.reviewers)), "tech_lead": m.project.tech_lead,
            "overridden": sorted(ov)}


# ---------------------------------------------------------------- validation
def validate(conn: sqlite3.Connection, scope: str, target: str, key: str, value: Any) -> Any:
    """Normalised value, or ValueError with a short reason."""
    agents = _agents(conn)
    if scope == "project":
        if key not in PROJECT_KEYS:
            raise ValueError(f"unknown project setting {key!r}")
        cur = effective_project(conn, target)
        role = "reviewer" if key == "reviewers" else "worker"
        ids = list(value or []) if isinstance(value, list) else None
        if ids is None or not all(isinstance(a, str) for a in ids):
            raise ValueError("expected a list of agent ids")
        if not ids:
            raise ValueError("a role needs at least one agent")
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate agent in list")
        for a in ids:
            if a not in agents:
                raise ValueError(f"unknown agent {a!r}")
            if role not in agents[a]["roles"]:
                raise ValueError(f"{a} does not have the {role} role")
            if not agents[a]["enabled"] and a not in cur[key]:   # agents already listed may be paused/disabled
                raise ValueError(f"{a} is disabled")
        dw = (ids if key == "workers" else cur["workers"])[0]
        reviewers = ids if key == "reviewers" else cur["reviewers"]
        if reviewers and all(r == dw for r in reviewers):
            # a reviewer is never the task's author: someone other than the default worker must review
            raise ValueError("reviewers would only contain the default worker (a task's author cannot review it)")
        return ids
    if scope == "agent":
        if target not in agents:
            raise ValueError(f"unknown agent {target!r}")
        if key == "fallback_to":
            ids = list(value or [])
            if len(set(ids)) != len(ids):
                raise ValueError("duplicate agent in list")
            for a in ids:
                if a not in agents:
                    raise ValueError(f"unknown agent {a!r}")
                if a == target:
                    raise ValueError("an agent cannot fall back to itself")
                if not set(agents[a]["roles"]) & set(agents[target]["roles"]):
                    raise ValueError(f"{a} shares no role with {target}")
            return ids
        if key == "enabled":
            if not isinstance(value, bool):
                raise ValueError("enabled must be true/false")
            return value
        if key == "max_concurrency":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_CONCURRENCY:
                raise ValueError(f"max_concurrency must be 0..{MAX_CONCURRENCY}")
            return value
        raise ValueError(f"unknown agent setting {key!r}")
    raise ValueError(f"unknown scope {scope!r}")


# ---------------------------------------------------------------- writes
def _snapshot(conn: sqlite3.Connection, scope: str, target: str, actor: str) -> None:
    conn.execute("INSERT INTO override_history (scope, target, before_json, actor, created_at) VALUES (?,?,?,?,?)",
                 (scope, target, jdump(get(conn, scope, target)), actor, now_iso()))


def _put(conn: sqlite3.Connection, scope: str, target: str, key: str, value: Any, actor: str) -> None:
    conn.execute("INSERT INTO overrides (scope, target, key, value_json, updated_by, updated_at) VALUES "
                 "(?,?,?,?,?,?) ON CONFLICT(scope, target, key) DO UPDATE SET value_json=excluded.value_json, "
                 "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                 (scope, target, key, jdump(value), actor, now_iso()))


def set_override(conn: sqlite3.Connection, scope: str, target: str, key: str, value: Any, actor: str) -> Any:
    with tx(conn):
        value = validate(conn, scope, target, key, value)
        if scope == "project":
            old = effective_project(conn, target)[key]
        else:
            old = get(conn, scope, target).get(key)
        _snapshot(conn, scope, target, actor)
        _put(conn, scope, target, key, value, actor)
        if scope == "project" and key == "workers":
            conn.execute("DELETE FROM overrides WHERE scope='project' AND target=? AND key='default_worker'", (target,))
        if scope == "agent":
            apply_agent_rows(conn)
        audit(conn, actor, "config.override", scope=scope, target=target, key=key, value=value, old=old)
    return value


def edit_list(conn: sqlite3.Connection, slug: str, role: str, op: str, agent: str, actor: str) -> list[str]:
    """One tap on the ⚙️ screen: move `agent` up/down, remove it, or add it at the end."""
    key = ROLE_KEY[role]
    cur = effective_project(conn, slug)[key]
    new = list(cur)
    if op == "add":
        if agent in new:
            raise ValueError(f"{agent} is already in the list")
        new.append(agent)
    else:
        if agent not in new:
            raise ValueError(f"{agent} is not in the list")
        i = new.index(agent)
        if op == "remove":
            if len(new) == 1:
                raise ValueError("cannot remove the last agent of a role")
            new.pop(i)
        elif op in ("up", "down"):
            j = i - 1 if op == "up" else i + 1
            if not 0 <= j < len(new):
                return cur
            new[i], new[j] = new[j], new[i]
        else:
            raise ValueError(f"unknown operation {op!r}")
    return set_override(conn, "project", slug, key, new, actor)


def _restore_agent_columns(conn: sqlite3.Connection, target: str, keys: set[str],
                           config_agents: list[dict] | None) -> None:
    a = next((x for x in config_agents or [] if x.get("id") == target), None)
    if a is None:
        return
    if "enabled" in keys:
        conn.execute("UPDATE agents SET enabled=? WHERE id=?", (int(a.get("enabled", True) is not False), target))
    if "max_concurrency" in keys:
        conn.execute("UPDATE agents SET max_concurrency=? WHERE id=?", (int(a.get("max_concurrency", 1)), target))


def reset(conn: sqlite3.Connection, scope: str, target: str, actor: str,
          config_agents: list[dict] | None = None) -> list[str]:
    """Drop all overrides of one project/agent. Agent rows get their config values back."""
    with tx(conn):
        keys = sorted(get(conn, scope, target))
        if keys:
            _snapshot(conn, scope, target, actor)
        conn.execute("DELETE FROM overrides WHERE scope=? AND target=?", (scope, target))
        if scope == "agent":
            _restore_agent_columns(conn, target, set(keys), config_agents)
        audit(conn, actor, "config.reset", scope=scope, target=target, keys=keys)
    return keys


def undo(conn: sqlite3.Connection, scope: str, target: str, actor: str,
         config_agents: list[dict] | None = None) -> bool:
    """Restore the overrides as they were before the last change (False when there is nothing to undo)."""
    with tx(conn):
        h = row(conn, "SELECT * FROM override_history WHERE scope=? AND target=? ORDER BY id DESC LIMIT 1",
                scope, target)
        if h is None:
            return False
        before = jload(h["before_json"], {})
        now = get(conn, scope, target)
        conn.execute("DELETE FROM overrides WHERE scope=? AND target=?", (scope, target))
        for k, v in before.items():
            _put(conn, scope, target, k, v, actor)
        if scope == "agent":
            _restore_agent_columns(conn, target, set(now) - set(before), config_agents)
            apply_agent_rows(conn)
        conn.execute("DELETE FROM override_history WHERE id=?", (h["id"],))
        audit(conn, actor, "config.undo", scope=scope, target=target)
    return True
