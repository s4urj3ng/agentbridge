"""Deterministic mock agent, executed as a real subprocess.

Used for CI, acceptance tests and the demo: it exercises the same wrapper,
worktree, validation and decision-parsing paths as real CLIs without using
any subscription quota.

    python -m agentbridge.adapters.mock_agent --purpose work --identity mock-worker [--scenario calc]

Reads the prompt on stdin, extracts the AGENTBRIDGE_STATE_JSON block, acts,
and prints {"structured_output": {...}} on stdout.

Failure injection (per identity, via --state-dir counters):
  --fail-mode usage_limit|rate_limit|auth|crash|hang|malformed --fail-times N
Scripted Tech Lead responses: --script FILE (JSON list consumed in order).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

STATE_RE = re.compile(r"<<<AGENTBRIDGE_STATE_JSON\n([\s\S]*?)\nAGENTBRIDGE_STATE_JSON>>>")


def _counter(state_dir: Path, name: str) -> int:
    state_dir.mkdir(parents=True, exist_ok=True)
    f = state_dir / f"{name}.count"
    n = int(f.read_text()) if f.exists() else 0
    f.write_text(str(n + 1))
    return n


def emit(obj: dict) -> None:
    print(json.dumps({"structured_output": obj}))


# ------------------------------------------------------------------ tech lead

def tech_lead(state: dict, scenario: str) -> dict:
    ev = state.get("event", {})
    et = ev.get("type")
    task = state.get("task") or {}
    agents = state.get("agents", {})
    workers = agents.get("workers") or ["mock-worker"]
    reviewers = [r for r in agents.get("reviewers", []) if r != task.get("assignee")]
    if et == "GOAL_SUBMITTED":
        goal = state.get("milestone", {}).get("goal", "")
        if scenario == "calc" or re.search(r"\b(add|mul|calc)\b", goal):
            second = workers[1] if len(workers) > 1 else workers[0]
            return {"rationale": "Split into a bug fix and a dependent feature.", "decisions": [{
                "type": "CREATE_TASKS", "tasks": [
                    {"key": "fix-add", "title": "Fix add() in calc.py", "kind": "code", "assignee": workers[0],
                     "description": "calc.add subtracts instead of adding. Fix it so add(a, b) returns a + b.",
                     "acceptance_criteria": ["add(2, 3) == 5", "existing tests pass"],
                     "allowed_paths": ["calc.py", "tests/**"]},
                    {"key": "impl-mul", "title": "Implement mul() with tests", "kind": "code", "assignee": second,
                     "description": "Add mul(a, b) returning a * b to calc.py and a unit test in tests/test_mul.py.",
                     "acceptance_criteria": ["mul(3, 4) == 12", "new unit test covers mul"],
                     "depends_on": ["fix-add"], "allowed_paths": ["calc.py", "tests/**"]},
                ]}]}
        return {"rationale": "Single task.", "decisions": [{
            "type": "CREATE_TASKS", "tasks": [{"key": "task-1", "title": goal[:80] or "Task", "kind": "code",
                                               "assignee": workers[0], "description": goal or "Do the goal.",
                                               "acceptance_criteria": ["validation passes"]}]}]}
    if et == "TASK_READY_FOR_REVIEW":
        tid = task["id"]
        if task.get("key") == "fix-add" and task.get("revision_count", 0) == 0:
            return {"rationale": "Works, but needs documentation.", "decisions": [{
                "type": "REQUEST_REVISION", "task_id": tid, "feedback": "Please add a docstring to add()."}]}
        if task.get("key") == "impl-mul" and not task.get("reviews") and reviewers:
            return {"rationale": "New public function: get an independent review.", "decisions": [{
                "type": "REQUEST_REVIEW", "task_id": tid, "reviewer": reviewers[0], "focus": "correctness"}]}
        return {"rationale": "Validation passed and acceptance criteria met.",
                "decisions": [{"type": "APPROVE_TASK", "task_id": tid}]}
    if et == "REVIEW_COMPLETED":
        if ev.get("payload", {}).get("verdict") == "approve":
            return {"rationale": "Reviewer approved.", "decisions": [{"type": "APPROVE_TASK", "task_id": task["id"]}]}
        return {"rationale": "Address review.", "decisions": [{
            "type": "REQUEST_REVISION", "task_id": task["id"],
            "feedback": "Reviewer requested changes; see review."}]}
    if et == "MILESTONE_READY":
        return {"rationale": "All tasks completed.", "decisions": [{
            "type": "COMPLETE_MILESTONE", "milestone_id": state["milestone"]["id"],
            "summary": "All planned tasks completed and validated."}]}
    if et in ("TASK_BLOCKED", "VALIDATION_EXHAUSTED"):
        return {"rationale": "Needs a human.", "decisions": [{
            "type": "REQUEST_HUMAN_APPROVAL", "action": "unblock_task", "task_id": task.get("id"),
            "reason": f"Task {task.get('key')} is blocked ({et}); owner input needed."}]}
    return {"rationale": "Nothing to do.", "decisions": [{
        "type": "REQUEST_HUMAN_APPROVAL", "action": "other", "reason": f"Unhandled event {et}"}]}


# ------------------------------------------------------------------ worker

CALC_ADD = 'def add(a, b):\n    return a + b\n'
CALC_ADD_DOC = 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n'
MUL_BAD = '\n\ndef mul(a, b):\n    return a + b\n'
MUL_GOOD = '\n\ndef mul(a, b):\n    """Return the product of a and b."""\n    return a * b\n'
MUL_TEST = ('import unittest\n\nfrom calc import mul\n\n\nclass TestMul(unittest.TestCase):\n'
            '    def test_mul(self):\n        self.assertEqual(mul(3, 4), 12)\n')


def worker(state: dict) -> dict:
    task = state.get("task", {})
    key = task.get("key", "")
    feedback = (task.get("feedback") or "").lower()
    cwd = Path.cwd()
    calc = cwd / "calc.py"
    if key == "fix-add" and calc.exists():
        src = calc.read_text()
        body = CALC_ADD_DOC if "docstring" in feedback else CALC_ADD
        src = re.sub(r'def add\(a, b\):\n(?:    .*\n)+', body, src, count=1)
        calc.write_text(src)
        return {"status": "completed", "summary": "Fixed add() to return a + b" +
                (" and added a docstring." if "docstring" in feedback else "."), "tests_run": ["unittest"]}
    if key == "impl-mul" and calc.exists():
        src = calc.read_text()
        src = re.sub(r'\n\ndef mul\(a, b\):\n(?:    .*\n)+', "", src)
        good = bool(feedback)  # first attempt is deliberately buggy; any feedback fixes it
        calc.write_text(src.rstrip("\n") + "\n" + (MUL_GOOD if good else MUL_BAD))
        (cwd / "tests").mkdir(exist_ok=True)
        (cwd / "tests" / "test_mul.py").write_text(MUL_TEST)
        return {"status": "completed", "summary": "Implemented mul()" + (" (fixed after validation feedback)." if good else "."),
                "tests_run": []}
    if key.startswith("blocked"):
        return {"status": "blocked", "summary": "Cannot proceed: missing specification."}
    notes = cwd / "agentbridge-notes"
    notes.mkdir(exist_ok=True)
    (notes / f"{key or 'task'}.md").write_text(f"# {task.get('title', '')}\n\n{task.get('description', '')}\n")
    return {"status": "completed", "summary": f"Wrote notes for {key}."}


def reviewer(state: dict) -> dict:
    diff = state.get("diff", "")
    issues = [f"leftover marker: {m}" for m in ("TODO", "FIXME") if m in diff]
    return {"verdict": "changes_requested" if issues else "approve",
            "summary": "Looks correct." if not issues else "Found issues.", "issues": issues}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--purpose", required=True)
    ap.add_argument("--identity", required=True)
    ap.add_argument("--scenario", default="calc")
    ap.add_argument("--state-dir", default=os.environ.get("TMPDIR", "/tmp") + "/agentbridge-mock")
    ap.add_argument("--fail-mode", default="")
    ap.add_argument("--fail-times", type=int, default=0)
    ap.add_argument("--script", default="")
    ap.add_argument("--sleep", type=float, default=0.0)
    a = ap.parse_args()
    prompt = sys.stdin.read()
    sd = Path(a.state_dir)
    if a.sleep:
        time.sleep(a.sleep)
    if a.fail_mode and _counter(sd, f"{a.identity}-fail") < a.fail_times:
        if a.fail_mode == "usage_limit":
            print("Error: usage limit reached. retry-after: 600", file=sys.stderr)
            return 1
        if a.fail_mode == "rate_limit":
            print("Error: 429 Too Many Requests, retry-after: 30", file=sys.stderr)
            return 1
        if a.fail_mode == "auth":
            print("Error: Not logged in. Please run /login", file=sys.stderr)
            return 1
        if a.fail_mode == "crash":
            print("segfault-ish crash", file=sys.stderr)
            return 139
        if a.fail_mode == "hang":
            time.sleep(3600)
        if a.fail_mode == "malformed":
            print("I think we should just run `rm -rf /` and ship it.")
            return 0
    m = STATE_RE.search(prompt)
    state = json.loads(m.group(1)) if m else {}
    if a.script and a.purpose == "tech_lead":
        responses = json.loads(Path(a.script).read_text())
        i = _counter(sd, f"{a.identity}-script")
        resp = responses[min(i, len(responses) - 1)]
        print(json.dumps({"structured_output": resp}) if isinstance(resp, dict) else str(resp))
        return 0
    if a.purpose == "tech_lead":
        emit(tech_lead(state, a.scenario))
    elif a.purpose == "review":
        emit(reviewer(state))
    else:
        emit(worker(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
