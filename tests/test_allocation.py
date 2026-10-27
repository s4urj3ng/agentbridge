"""Quota-aware work allocation: rolling windows, per-event Tech Lead model, profiles, model guards."""
from __future__ import annotations

import json
from pathlib import Path

from agentbridge.adapters.antigravity import AntigravityAdapter
from agentbridge.db import now_iso
from agentbridge.orchestrator import quota

from .conftest import make_engine, q, register, run, script_agent


def _usage(engine, agent, n, purpose="work"):
    for _ in range(n):
        engine.conn.execute("INSERT INTO quota_usage (agent_id, purpose, started_at) VALUES (?,?,?)",
                            (agent, purpose, now_iso()))


def test_rolling_window_limits(settings):
    for a in settings.agents:
        if a["id"] == "mock-worker":
            a["rolling_limits"] = [{"hours": 5, "max": 3, "reserve_for_review": 1}, {"hours": 168, "max": 10}]
    e = make_engine(settings)
    _usage(e, "mock-worker", 2)
    ok, why = quota.admission(e.conn, "mock-worker", "work")
    assert not ok and "5h window" in why                    # 2 >= 3 - 1 reserved
    assert quota.admission(e.conn, "mock-worker", "review")[0]
    _usage(e, "mock-worker", 1, "review")
    assert not quota.admission(e.conn, "mock-worker", "review")[0]
    assert quota.admission(e.conn, "mock-worker-b", "work")[0]  # other agents unaffected


def test_tech_lead_model_per_event_and_profiles(settings, tmp_path):
    for a in settings.agents:
        if a["id"] == "mock-lead":
            a["model"] = "light"
            a["event_models"] = {"GOAL_SUBMITTED": "strong"}
        if a["id"] == "mock-worker-b":
            a["profile"] = "cheap and abundant; use for routine work"
    e = make_engine(settings)
    register(e, tmp_path)
    e.submit_goal("demo-calc", "Fix add() and implement mul()", "A1")
    run(e, 120)
    tl = q(e, "SELECT e.type, a.run_dir FROM execution_attempts a JOIN events e ON e.id=a.event_id "
              "WHERE a.purpose='tech_lead' ORDER BY a.id")
    models = {}
    for r in tl:
        argv = json.loads((Path(r["run_dir"]) / "argv.json").read_text())
        models.setdefault(r["type"], set()).add(argv[argv.index("--model") + 1])
    assert models["GOAL_SUBMITTED"] == {"strong"}
    assert models["TASK_READY_FOR_REVIEW"] == {"light"}
    prompt = (Path(tl[0]["run_dir"]) / "prompt.redacted.txt").read_text()
    assert "cheap and abundant" in prompt


def test_antigravity_external_models_refused():
    ad = AntigravityAdapter("agy", {"model": "claude-opus-5-5-high"})
    assert not ad.model_allowed("claude-opus-5-5-high")[0]
    assert not ad.model_allowed("gpt-oss-120b-medium")[0]
    assert not ad.model_allowed(None)[0]
    assert ad.model_allowed("gemini-3.8-flash-high")[0]
    assert AntigravityAdapter("agy", {"allow_external_models": True}).model_allowed("claude-opus-5-5-high")[0]


def test_task_for_misconfigured_agent_is_blocked_not_stuck(settings, tmp_path):
    settings.agents.append({"id": "agy-ext", "provider": "antigravity", "roles": ["worker"],
                            "model": "gpt-oss-120b-medium"})
    script_agent(settings, "mock-lead", [
        {"rationale": "x", "decisions": [{"type": "CREATE_TASKS", "tasks": [
            {"key": "t1", "title": "notes", "description": "notes", "kind": "research", "assignee": "agy-ext",
             "acceptance_criteria": ["ok"], "validation": []}]}]},
        {"rationale": "x", "decisions": [{"type": "REQUEST_HUMAN_APPROVAL", "action": "other", "reason": "reassign"}]},
    ], tmp_path)
    e = make_engine(settings)
    register(e, tmp_path, project_overrides={"workers": ["agy-ext"]})
    e.submit_goal("demo-calc", "notes", "A2")
    run(e, 30)
    t = q(e, "SELECT state, blocked_reason FROM tasks")[0]
    assert t["state"] == "BLOCKED" and "external-model quota" in t["blocked_reason"]
    assert q(e, "SELECT COUNT(*) c FROM execution_attempts WHERE agent_id='agy-ext'")[0]["c"] == 0


def test_config_disable_wins_over_db(settings):
    e = make_engine(settings)
    e.conn.execute("UPDATE agents SET enabled=1 WHERE id='mock-reviewer'")
    for a in settings.agents:
        if a["id"] == "mock-reviewer":
            a["enabled"] = False
    e.sync_agents()
    assert q(e, "SELECT enabled FROM agents WHERE id='mock-reviewer'")[0]["enabled"] == 0
