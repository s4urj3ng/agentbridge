"""The owner's project review rules reach every reviewer."""
from __future__ import annotations

from agentbridge.orchestrator.context import build_review_prompt
from agentbridge.policies.manifest import ProjectSection

TASK = {"id": 1, "key": "t1", "title": "Title", "description": "Do it", "acceptance_json": '["works"]'}


def test_review_prompt_carries_project_review_rules():
    p = build_review_prompt(TASK, diff="", validation_summary="ok", guidance="Wording issues are notes, not blockers.")
    assert "## Project review rules (owner)\nWording issues are notes, not blockers." in p


def test_review_prompt_without_rules_has_no_rules_section():
    assert "Project review rules" not in build_review_prompt(TASK, diff="", validation_summary="ok")
    assert ProjectSection(slug="p1", name="P", repo_path="/r", tech_lead="l", default_worker="w").review_guidance == ""
