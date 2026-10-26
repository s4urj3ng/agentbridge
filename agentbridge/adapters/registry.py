from __future__ import annotations

from typing import Any

from .antigravity import AntigravityAdapter
from .base import AgentAdapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .gemini import GeminiCliAdapter
from .mindlogic import MindlogicAdapter
from .mock import MockAdapter

PROVIDERS: dict[str, type[AgentAdapter]] = {
    "mock": MockAdapter,
    "claude": ClaudeAdapter,
    "antigravity": AntigravityAdapter,
    "codex": CodexAdapter,
    "gemini": GeminiCliAdapter,
    "mindlogic": MindlogicAdapter,
}


def make_adapter(provider: str, identity: str, config: dict[str, Any]) -> AgentAdapter:
    try:
        cls = PROVIDERS[provider]
    except KeyError:
        raise ValueError(f"unknown provider {provider!r}") from None
    return cls(identity, config)
