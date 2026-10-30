"""Project manifest (YAML) schema. The manifest is owner-authored and trusted;
agent output never modifies it."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

HARDWARE_STRICT_NO_GO = [
    "firmware_flashing", "camera_operations", "sd_card_writing",
    "boot_manipulation", "firmware_deployment", "hardware_trial",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectSection(_Strict):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{1,40}$")
    name: str
    repo_path: str
    remote_url: str | None = None
    remote_name: str = "origin"
    base_branch: str = "main"
    tech_lead: str
    default_worker: str
    workers: list[str] = Field(default_factory=list)
    reviewers: list[str] = Field(default_factory=list)
    review_policy: Literal["never", "risk", "always"] = "risk"
    review_risk_paths: list[str] = Field(default_factory=list)


class ValidationSection(_Strict):
    commands: dict[str, list[str]] = Field(default_factory=dict)
    default: list[str] = Field(default_factory=list)
    timeout_s: int = 600

    @field_validator("default")
    @classmethod
    def _known(cls, v, info):
        cmds = info.data.get("commands", {})
        missing = [n for n in v if n not in cmds]
        if missing:
            raise ValueError(f"default validation names not defined: {missing}")
        return v


class PolicySection(_Strict):
    forbidden_paths: list[str] = Field(default_factory=lambda: [".github/workflows/**", "**/*.pem", "**/.env", "**/id_rsa*"])
    hardware_no_go: list[str] = Field(default_factory=list)
    hardware_policy: Literal["none", "STRICT_NO_GO"] = "none"
    auto_start: bool = True               # False: new goals wait for owner approval
    # True: the owner pre-authorises the Tech Lead to keep a milestone moving on its own (retry,
    # revise, reassign, review) instead of asking; only scope changes still go to the owner.
    autonomous: bool = False
    # True: when a milestone completes, the Tech Lead plans and starts the next one itself
    # (PROPOSE_MILESTONE) or stops with NO_FURTHER_WORK. Bounded by auto_next_max_per_day.
    auto_next: bool = False
    auto_next_max_per_day: int = 4
    # True: a new milestone starts from the previous milestone's integration branch while that
    # branch is not yet merged into the base branch (stacked work), instead of from the base.
    stack_on_unmerged: bool = True
    draft_pr: Literal["never", "approval", "auto"] = "approval"
    # True: the owner delegates approvals to the Tech Lead. Draft PRs, scope changes and engine
    # stops (Tech Lead cap / repeated failures) are approved at once as `tech_lead:self`; planning
    # no longer stops to wait for owner decisions. Hardware policy, forbidden paths, protected
    # branches and ROM byte ranges are never covered.
    self_approve: bool = False
    protected_branches: list[str] = Field(default_factory=list)
    max_changed_files: int = 50

    def denied_categories(self) -> list[str]:
        cats = list(self.hardware_no_go)
        if self.hardware_policy == "STRICT_NO_GO":
            cats += [c for c in HARDWARE_STRICT_NO_GO if c not in cats]
        return cats


class LimitOverrides(_Strict):
    max_task_depth: int | None = None
    max_tasks_per_milestone: int | None = None
    max_revisions: int | None = None
    max_validation_retries: int | None = None
    default_task_timeout_s: int | None = None


class Manifest(_Strict):
    project: ProjectSection
    validation: ValidationSection = Field(default_factory=ValidationSection)
    policy: PolicySection = Field(default_factory=PolicySection)
    limits: LimitOverrides = Field(default_factory=LimitOverrides)
    charter: str = ""
    context: list[str] = Field(default_factory=list)
    completed_milestones: list[str] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "Manifest":
        p = Path(path)
        data = yaml.safe_load(p.read_text()) or {}
        m = cls.model_validate(data)
        rp = Path(m.project.repo_path).expanduser()
        if not rp.is_absolute():
            rp = (p.parent / rp).resolve()
        m.project.repo_path = str(rp)
        return m
