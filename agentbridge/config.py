"""Runtime configuration.

Sources, lowest to highest precedence:
  1. Built-in defaults (below)
  2. YAML config file (AGENTBRIDGE_CONFIG, default ./agentbridge.yaml)
  3. Environment variables (AGENTBRIDGE_*)

Secrets (admin token, webhook secret, API keys) are only ever read from the
environment and are never written to the database or logs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Limits:
    max_task_depth: int = 2
    max_tasks_per_milestone: int = 20
    max_revisions: int = 3
    max_validation_retries: int = 2
    max_concurrent_agents: int = 3
    default_task_timeout_s: int = 1800
    tech_lead_timeout_s: int = 600
    max_event_attempts: int = 3
    max_quota_requeues: int = 3
    max_decisions_per_response: int = 12
    max_tasks_per_decision: int = 8
    max_tl_invocations_per_milestone: int = 60
    max_execution_failures: int = 3


@dataclass
class Settings:
    data_dir: Path
    config_file: Path | None = None
    bind_host: str = "127.0.0.1"
    port: int = 8787
    poll_interval_s: float = 2.0
    quota_poll_s: float = 600.0          # free provider quota probes (Antigravity /usage); 0 = off
    limits: Limits = field(default_factory=Limits)
    agents: list[dict[str, Any]] = field(default_factory=list)
    github: dict[str, Any] = field(default_factory=dict)
    telegram: dict[str, Any] = field(default_factory=dict)
    # global hard allowlist of executables usable in validation commands
    validation_executables: list[str] = field(default_factory=lambda: [
        "python", "python3", "pytest", "make", "npm", "node", "cargo", "go",
        "ruff", "mypy", "sh", "bash", "git", "true", "false",
    ])
    protected_branches: list[str] = field(default_factory=lambda: [
        "main", "master", "release/*", "production", "stable"])

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agentbridge.db"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "worktrees"

    @property
    def admin_token(self) -> str | None:
        return os.environ.get("AGENTBRIDGE_ADMIN_TOKEN") or None

    @property
    def session_secret(self) -> str:
        return os.environ.get("AGENTBRIDGE_SESSION_SECRET") or (self.admin_token or "") + ":session"

    @property
    def webhook_secret(self) -> str | None:
        return os.environ.get("GITHUB_WEBHOOK_SECRET") or None

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.artifacts_dir, self.workspaces_dir):
            p.mkdir(parents=True, exist_ok=True)


SYSTEM_CONFIG = Path("/etc/agentbridge/agentbridge.yaml")
SYSTEM_DATA_DIR = Path("/var/lib/agentbridge")
SYSTEM_ENV_FILE = Path("/etc/agentbridge/agentbridge.env")


def load_env_file(path: Path) -> list[str]:
    """Load KEY=VALUE lines into os.environ without overriding existing values.

    systemd units get this file via EnvironmentFile=; manual CLI runs (e.g.
    `agentbridge agents smoke mindlogic-reviewer`) need the same secrets.
    Returns the names loaded (never the values).
    """
    loaded: list[str] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return loaded
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip().removeprefix("export ").strip(), val.strip().strip('"').strip("'")
        if key.isidentifier() and val and key not in os.environ:
            os.environ[key] = val
            loaded.append(key)
    return loaded

DEFAULT_AGENTS: list[dict[str, Any]] = [
    # Mock identities: always available, consume no subscription quota.
    {"id": "mock-lead", "provider": "mock", "roles": ["tech_lead"], "max_concurrency": 1},
    {"id": "mock-worker", "provider": "mock", "roles": ["worker"], "max_concurrency": 2},
    {"id": "mock-worker-b", "provider": "mock", "roles": ["worker"], "max_concurrency": 1},
    {"id": "mock-reviewer", "provider": "mock", "roles": ["reviewer"], "max_concurrency": 1,
     "daily_invocation_limit": 50, "reserved_review_invocations": 5},
]


def load_settings(config_file: str | os.PathLike | None = None,
                  data_dir: str | os.PathLike | None = None) -> Settings:
    explicit = config_file or os.environ.get("AGENTBRIDGE_CONFIG")
    if explicit:
        cfg_path = Path(explicit)
    else:
        # ./agentbridge.yaml for development, else the system install's config
        cfg_path = next((p for p in (Path("agentbridge.yaml"), SYSTEM_CONFIG) if p.exists()), Path("agentbridge.yaml"))
    if cfg_path.resolve() == SYSTEM_CONFIG:
        load_env_file(Path(os.environ.get("AGENTBRIDGE_ENV_FILE", SYSTEM_ENV_FILE)))
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text()) or {}
    default_dir = SYSTEM_DATA_DIR if cfg_path.resolve() == SYSTEM_CONFIG else Path("./var")
    ddir = Path(data_dir or os.environ.get("AGENTBRIDGE_DATA_DIR") or raw.get("data_dir") or default_dir).resolve()
    limits = Limits(**(raw.get("limits") or {}))
    s = Settings(data_dir=ddir, config_file=cfg_path if cfg_path.exists() else None, limits=limits)
    s.bind_host = os.environ.get("AGENTBRIDGE_BIND", raw.get("bind_host", s.bind_host))
    s.port = int(os.environ.get("AGENTBRIDGE_PORT", raw.get("port", s.port)))
    s.poll_interval_s = float(raw.get("poll_interval_s", s.poll_interval_s))
    s.quota_poll_s = float(raw.get("quota_poll_s", s.quota_poll_s))
    s.agents = raw.get("agents") or list(DEFAULT_AGENTS)
    s.github = raw.get("github") or {}
    s.telegram = raw.get("telegram") or {}
    if raw.get("validation_executables"):
        s.validation_executables = list(raw["validation_executables"])
    if raw.get("protected_branches"):
        s.protected_branches = list(raw["protected_branches"])
    return s
