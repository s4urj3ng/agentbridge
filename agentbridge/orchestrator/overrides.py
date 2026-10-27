"""Owner runtime overrides of role assignment and routing order.

Set from the Telegram "⚙️ Phân công" view (gated by `telegram.allow_config_changes`).
They are standing configuration and take precedence over the YAML config and the
project manifest:

  project (target = slug): default_worker, workers (ordered), reviewers (ordered, first = preferred)
  agent   (target = id):   fallback_to (ordered), enabled, max_concurrency

Project overrides are applied when the engine loads a manifest (`apply_project`), so the
Tech Lead roster, decision validation, dispatch and review routing all see them. Agent
`enabled` / `max_concurrency` are written to the `agents` row and re-applied after every
`sync_agents()`; `fallback_to` is read through `fallback_chain`.

Hard safety policy (hardware no-go, protected branches, adapter model guards) is not
configurable here. Every change is validated against the identities in the database and
audited.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from ..db import audit, jdump, jload, now_iso, row, rows, tx

PROJECT_KEYS = ("default_worker", "workers", "reviewers")
AGENT_KEYS = ("fallback_to", "enabled", "max_concurrency")
MAX_CONCURRENCY = 8


def get(conn: sqlite3.Connection, scope: str, target: str) -> dict[str, Any]:
    return {r["key"]: jload(r["value_json"]) for r in rows(
        conn, "SELECT key, value_json FROM overrides WHERE scope=? AND target=?", scope, target)}


def all_agent_overrides(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in rows(conn, "SELECT target, key, value_json FROM overrides WHERE scope='agent'"):
        out.setdefault(r["target"], {})[r["key"]] = jload(r["value_json"])
    return out


# ---------------------------------------------------------------- effective values
def apply_project(conn: sqlite3.Connection, m):
    """The manifest with the project's overrides applied (in place; returns it)."""
    ov = get(conn, "project", m.project.slug)
    for k in PROJECT_KEYS:
        if k in ov:
            setattr(m.project, k, ov[k])
    return m


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


# ---------------------------------------------------------------- validation
def _agents(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["id"]: {"enabled": bool(r["enabled"]), "roles": jload(r["roles_json"], [])}
            for r in rows(conn, "SELECT id, enabled, roles_json FROM agents")}


def eligible(conn: sqlite3.Connection, role: str) -> list[str]:
    """Enabled identities that have `role`, sorted."""
    return sorted(a for a, v in _agents(conn).items() if v["enabled"] and role in v["roles"])


def effective_project(conn: sqlite3.Connection, slug: str) -> dict[str, Any]:
    """default_worker / workers / reviewers / tech_lead after overrides, plus which keys are overridden."""
    from ..policies.manifest import Manifest
    r = row(conn, "SELECT manifest_json FROM projects WHERE slug=?", slug)
    if r is None:
        raise ValueError(f"unknown project {slug!r}")
    m = Manifest.model_validate_json(r["manifest_json"])
    ov = get(conn, "project", slug)
    apply_project(conn, m)
    return {"default_worker": m.project.default_worker, "workers": list(m.project.workers),
            "reviewers": list(m.project.reviewers), "tech_lead": m.project.tech_lead, "overridden": sorted(ov)}


def validate(conn: sqlite3.Connection, scope: str, target: str, key: str, value: Any) -> Any:
    """Normalised value, or ValueError with a short reason."""
    agents = _agents(conn)
    if scope == "project":
        if key not in PROJECT_KEYS:
            raise ValueError(f"unknown project setting {key!r}")
        cur = effective_project(conn, target)
        role = "reviewer" if key == "reviewers" else "worker"
        ids = [value] if key == "default_worker" else list(value or [])
        if not all(isinstance(a, str) for a in ids):
            raise ValueError("agent ids must be strings")
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate agent in list")
        for a in ids:
            if a not in agents:
                raise ValueError(f"unknown agent {a!r}")
            if not agents[a]["enabled"]:
                raise ValueError(f"{a} is disabled")
            if role not in agents[a]["roles"]:
                raise ValueError(f"{a} does not have the {role} role")
        if key in ("default_worker", "reviewers") and not ids:
            raise ValueError(f"{key} cannot be empty")
        dw = value if key == "default_worker" else cur["default_worker"]
        reviewers = ids if key == "reviewers" else cur["reviewers"]
        if reviewers and all(r == dw for r in reviewers):
            # a reviewer is never the task's author: someone other than the default worker must review
            raise ValueError("reviewers would only contain the default worker (a task's author cannot review it)")
        return value if key == "default_worker" else ids
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
def set_override(conn: sqlite3.Connection, scope: str, target: str, key: str, value: Any, actor: str) -> Any:
    with tx(conn):
        value = validate(conn, scope, target, key, value)
        old = get(conn, scope, target).get(key)
        conn.execute("INSERT INTO overrides (scope, target, key, value_json, updated_by, updated_at) VALUES "
                     "(?,?,?,?,?,?) ON CONFLICT(scope, target, key) DO UPDATE SET value_json=excluded.value_json, "
                     "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                     (scope, target, key, jdump(value), actor, now_iso()))
        if scope == "agent":
            apply_agent_rows(conn)
        audit(conn, actor, "config.override", scope=scope, target=target, key=key, value=value, old=old)
    return value


def reset(conn: sqlite3.Connection, scope: str, target: str, actor: str,
          config_agents: list[dict] | None = None) -> list[str]:
    """Drop all overrides of one project/agent. Agent rows get their config values back."""
    with tx(conn):
        keys = sorted(get(conn, scope, target))
        conn.execute("DELETE FROM overrides WHERE scope=? AND target=?", (scope, target))
        if scope == "agent":
            a = next((x for x in config_agents or [] if x.get("id") == target), None)
            if a is not None:
                if "enabled" in keys:
                    conn.execute("UPDATE agents SET enabled=? WHERE id=?", (int(a.get("enabled", True) is not False), target))
                if "max_concurrency" in keys:
                    conn.execute("UPDATE agents SET max_concurrency=? WHERE id=?", (int(a.get("max_concurrency", 1)), target))
        audit(conn, actor, "config.reset", scope=scope, target=target, keys=keys)
    return keys
