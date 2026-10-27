from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from agentbridge.config import DEFAULT_AGENTS, Settings
from agentbridge.demo import create_demo_repo, demo_manifest
from agentbridge.orchestrator.engine import Engine
from agentbridge.policies.manifest import Manifest


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path / "data")
    s.agents = [dict(a) for a in DEFAULT_AGENTS]
    s.poll_interval_s = 0.05
    s.quota_poll_s = 0          # never call real provider CLIs from tests
    s.ensure_dirs()
    return s


def make_engine(settings: Settings) -> Engine:
    e = Engine(settings)
    e.sync_agents()
    return e


@pytest.fixture
def engine(settings: Settings) -> Engine:
    return make_engine(settings)


@pytest.fixture
def demo_project(engine: Engine, tmp_path: Path):
    repo = create_demo_repo(tmp_path / "repo")
    mf = tmp_path / "manifest.yaml"
    mf.write_text(yaml.safe_dump(demo_manifest(repo)))
    engine.register_project(Manifest.load(mf))
    return repo


def register(engine: Engine, tmp_path: Path, **kw) -> Path:
    repo = create_demo_repo(tmp_path / kw.pop("repo_name", "repo"))
    mf = tmp_path / "manifest.yaml"
    data = demo_manifest(repo, **{k: v for k, v in kw.items() if k in ("slug", "reviewers", "extra_policy")})
    for k, v in kw.get("project_overrides", {}).items():
        data["project"][k] = v
    mf.write_text(yaml.safe_dump(data))
    engine.register_project(Manifest.load(mf))
    return repo


def script_agent(settings: Settings, agent_id: str, responses: list, tmp_path: Path) -> None:
    f = tmp_path / f"{agent_id}-script.json"
    f.write_text(json.dumps(responses))
    for a in settings.agents:
        if a["id"] == agent_id:
            a["script"] = str(f)


def run(engine: Engine, seconds: float = 60, until_idle: bool = True) -> None:
    asyncio.run(engine.run(until_idle=until_idle, max_seconds=seconds))


def q(engine: Engine, sql: str, *args):
    return [dict(r) for r in engine.conn.execute(sql, args).fetchall()]
