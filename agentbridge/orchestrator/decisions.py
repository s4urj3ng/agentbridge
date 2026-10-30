"""Structured Tech Lead decisions.

An LLM response is an untrusted *proposal*. It must:
  1. parse as JSON,
  2. validate against this strict schema (unknown fields rejected), and
  3. pass policy checks in engine.apply_decisions()
before anything happens. There is deliberately no decision type that runs a
command, merges into a protected branch, pushes, or edits policy.
"""
from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

KEY_RE = r"^[A-Za-z0-9][A-Za-z0-9_\-\.]{0,48}$"
AGENT_RE = r"^[a-z0-9][a-z0-9\-]{1,48}$"
VALIDATION_NAME_RE = r"^[A-Za-z0-9_\-]{1,40}$"


class _D(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NewTask(_D):
    key: str = Field(pattern=KEY_RE)
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=3, max_length=8000)
    kind: Literal["code", "research", "test"] = "code"
    assignee: str = Field(pattern=AGENT_RE)
    acceptance_criteria: list[Annotated[str, Field(max_length=500)]] = Field(min_length=1, max_length=15)
    depends_on: list[str] = Field(default_factory=list, max_length=10)   # keys in this batch or existing task keys
    validation: list[Annotated[str, Field(pattern=VALIDATION_NAME_RE)]] | None = None
    allowed_paths: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list, max_length=30)
    parent_task_id: int | None = None
    timeout_s: int | None = Field(default=None, ge=60, le=4 * 3600)
    replaces: str | None = None    # key of an existing task this one redoes (it is cancelled, dependents move here)


class CreateTasks(_D):
    type: Literal["CREATE_TASKS"]
    tasks: list[NewTask] = Field(min_length=1, max_length=8)


class AssignTask(_D):
    type: Literal["ASSIGN_TASK"]
    task_id: int
    assignee: str = Field(pattern=AGENT_RE)


class RequestRevision(_D):
    type: Literal["REQUEST_REVISION"]
    task_id: int
    feedback: str = Field(min_length=3, max_length=6000)


class RequestReview(_D):
    type: Literal["REQUEST_REVIEW"]
    task_id: int
    reviewer: str = Field(pattern=AGENT_RE)
    focus: str = Field(default="", max_length=2000)


class ApproveTask(_D):
    type: Literal["APPROVE_TASK"]
    task_id: int
    note: str = Field(default="", max_length=2000)


class BlockTask(_D):
    type: Literal["BLOCK_TASK"]
    task_id: int
    reason: str = Field(min_length=3, max_length=2000)


class CompleteMilestone(_D):
    type: Literal["COMPLETE_MILESTONE"]
    milestone_id: int
    summary: str = Field(min_length=3, max_length=4000)


class RequestHumanApproval(_D):
    type: Literal["REQUEST_HUMAN_APPROVAL"]
    action: Literal["create_draft_pr", "start_milestone", "unblock_task", "scope_change", "other"]
    reason: str = Field(min_length=3, max_length=3000)
    task_id: int | None = None


class ProposeMilestone(_D):
    """Only valid for PLAN_NEXT_MILESTONE events (policy.auto_next)."""
    type: Literal["PROPOSE_MILESTONE"]
    key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_\-\.]{1,40}$")
    goal: str = Field(min_length=20, max_length=8000)


class NoFurtherWork(_D):
    """Only valid for PLAN_NEXT_MILESTONE events: nothing worthwhile remains within charter and policy."""
    type: Literal["NO_FURTHER_WORK"]
    reason: str = Field(min_length=3, max_length=3000)


class MergeMilestone(_D):
    """Only valid for PLAN_NEXT_MILESTONE events: merge a completed milestone's PR into the base branch."""
    type: Literal["MERGE_MILESTONE"]
    milestone_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_\-\.]{1,40}$")
    reason: str = Field(min_length=3, max_length=3000)


Decision = Annotated[Union[CreateTasks, AssignTask, RequestRevision, RequestReview, ApproveTask,
                           BlockTask, CompleteMilestone, RequestHumanApproval, ProposeMilestone, NoFurtherWork,
                           MergeMilestone],
                     Field(discriminator="type")]
PLANNING_DECISIONS = {"PROPOSE_MILESTONE", "NO_FURTHER_WORK", "MERGE_MILESTONE"}


class DecisionEnvelope(_D):
    rationale: str = Field(max_length=4000)
    decisions: list[Decision] = Field(min_length=1, max_length=12)


_ENVELOPE = TypeAdapter(DecisionEnvelope)


class MalformedDecision(Exception):
    pass


def portable_schema(schema: Any) -> Any:
    """Strip keywords that strict JSON-Schema validators reject.

    Verified against Claude Code 2.1.310: `--json-schema` runs in strict mode
    and rejects Pydantic's OpenAPI-style `discriminator`. The discriminated
    union still validates as a plain `oneOf`; AgentBridge re-validates anyway.
    """
    if isinstance(schema, dict):
        return {k: portable_schema(v) for k, v in schema.items() if k != "discriminator"}
    if isinstance(schema, list):
        return [portable_schema(v) for v in schema]
    return schema


def envelope_json_schema() -> dict[str, Any]:
    """JSON Schema handed to CLIs that support schema-constrained output."""
    return portable_schema(DecisionEnvelope.model_json_schema())


def parse_envelope(raw: Any) -> DecisionEnvelope:
    """Parse untrusted model output into a DecisionEnvelope or raise MalformedDecision."""
    if isinstance(raw, str):
        raw = _extract_json(raw)
    if not isinstance(raw, dict):
        raise MalformedDecision("response is not a JSON object")
    try:
        return _ENVELOPE.validate_python(raw)
    except ValidationError as e:
        raise MalformedDecision(f"schema validation failed: {e.error_count()} error(s): "
                                f"{json.dumps(e.errors(include_url=False, include_input=False), default=str)[:1500]}") from None


def _extract_json(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    raise MalformedDecision("response does not contain a parseable JSON object")


# ----------------------------------------------------------- worker result contract

class WorkerResult(_D):
    status: Literal["completed", "blocked", "failed"]
    summary: str = Field(max_length=6000)
    tests_run: list[str] = Field(default_factory=list, max_length=30)
    notes: str = Field(default="", max_length=4000)


class ReviewResult(_D):
    verdict: Literal["approve", "changes_requested"]
    summary: str = Field(max_length=6000)
    issues: list[Annotated[str, Field(max_length=1000)]] = Field(default_factory=list, max_length=30)


def worker_result_schema() -> dict[str, Any]:
    return portable_schema(WorkerResult.model_json_schema())


def review_result_schema() -> dict[str, Any]:
    return portable_schema(ReviewResult.model_json_schema())
