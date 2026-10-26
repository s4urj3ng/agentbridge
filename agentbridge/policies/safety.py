"""Hard safety policy. Nothing an agent outputs can relax these checks.

- Validation commands: only named commands from the project manifest, run as
  argv lists (never via a shell string from an agent), whose executable is in
  the global allowlist.
- Path confinement: every file path the orchestrator touches on behalf of a
  task is resolved (symlinks included) and must stay inside that task's
  worktree.
- Protected branches: never a merge/push target.
- Hardware no-go: project-level denied action categories are matched against
  task text before a task can be created or dispatched.
- Secret redaction for everything persisted as logs/artifacts.
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path, PurePosixPath
from typing import Iterable


class PolicyViolation(Exception):
    pass


# ---------------------------------------------------------------- paths

def confine_path(root: Path | str, candidate: str | os.PathLike) -> Path:
    """Resolve `candidate` relative to `root` and ensure it stays inside.

    Rejects absolute paths, `..` traversal and symlinks that point outside.
    """
    root_r = Path(root).resolve(strict=True)
    cand = Path(candidate)
    if cand.is_absolute():
        raise PolicyViolation(f"absolute path not allowed: {candidate}")
    if any(part == ".." for part in cand.parts):
        raise PolicyViolation(f"path traversal not allowed: {candidate}")
    if cand.parts and cand.parts[0] == ".git":
        raise PolicyViolation(f"access to .git is not allowed: {candidate}")
    full = (root_r / cand).resolve(strict=False)
    if full != root_r and root_r not in full.parents:
        raise PolicyViolation(f"path escapes worktree: {candidate}")
    return full


def read_confined(root: Path | str, rel: str, max_bytes: int = 512_000) -> str:
    p = confine_path(root, rel)
    if not p.is_file():
        raise FileNotFoundError(rel)
    with open(p, "rb") as f:
        return f.read(max_bytes).decode("utf-8", errors="replace")


def path_matches(path: str, patterns: Iterable[str]) -> bool:
    pp = PurePosixPath(path)
    for pat in patterns:
        if fnmatch.fnmatch(path, pat) or pp.match(pat):
            return True
        if pat.endswith("/**") and (path == pat[:-3] or path.startswith(pat[:-2])):
            return True
    return False


# ---------------------------------------------------------------- branches

def is_protected_branch(name: str, protected: Iterable[str]) -> bool:
    name = name.removeprefix("refs/heads/")
    return any(fnmatch.fnmatch(name, p) for p in protected)


AGENT_BRANCH_PREFIX = "agentbridge/"


def assert_agent_writable_branch(name: str, protected: Iterable[str]) -> None:
    """Orchestrator may only write branches it owns (agentbridge/*)."""
    if is_protected_branch(name, protected):
        raise PolicyViolation(f"branch {name!r} is protected")
    if not name.removeprefix("refs/heads/").startswith(AGENT_BRANCH_PREFIX):
        raise PolicyViolation(f"branch {name!r} is not an AgentBridge-owned branch")


# ---------------------------------------------------------------- commands

SHELL_META = re.compile(r"[;&|`$<>\\\n]")


def check_validation_command(name: str, manifest_cmds: dict[str, list[str]], allowed_exes: Iterable[str]) -> list[str]:
    """Return argv for a named validation command, or raise."""
    if name not in manifest_cmds:
        raise PolicyViolation(f"validation command {name!r} is not defined in the project manifest")
    argv = manifest_cmds[name]
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise PolicyViolation(f"validation command {name!r} must be a non-empty argv list")
    exe = os.path.basename(argv[0])
    if exe not in set(allowed_exes):
        raise PolicyViolation(f"executable {exe!r} is not in the validation allowlist")
    # sh -c / bash -c are only allowed when the script itself comes from the
    # manifest (owner-authored), which is the case here; still forbid any
    # network-fetch-and-execute patterns as a guard.
    joined = " ".join(argv)
    if re.search(r"\b(curl|wget)\b.*\|\s*(sh|bash)", joined):
        raise PolicyViolation("fetch-and-execute patterns are forbidden")
    return list(argv)


# ---------------------------------------------------------------- hardware / no-go

NO_GO_CATEGORIES: dict[str, list[str]] = {
    "firmware_flashing": [r"\bflash(ing|es|ed)?\b.*\bfirmware\b", r"\bfirmware\b.*\bflash", r"\bfirmware update\b", r"\bfi2\b", r"\.fir\b"],
    "camera_operations": [r"\bon[- ]camera\b", r"\bcamera (test|run|operation|trial)", r"\bptp\b", r"\bchdkptp\b", r"\bconnect(ing)? (to )?(the )?camera\b"],
    "sd_card_writing": [r"\bsd[- ]?card\b", r"\bmake ?bootable\b", r"\bdd if=", r"\b/dev/(disk|sd|mmcblk)"],
    "boot_manipulation": [r"\bboot ?(sector|flag|loader|manipulat)", r"\bdiskboot\b", r"\bpsboot\b", r"\bbootloader\b"],
    "firmware_deployment": [r"\bdeploy(ing|ment)?\b.*\b(firmware|camera|device)\b"],
    "hardware_trial": [r"\bhardware (trial|test|run|execution)\b", r"\bon[- ]device\b", r"\breal hardware\b"],
}


def hardware_no_go_hits(text: str, denied_categories: Iterable[str]) -> list[str]:
    t = text.lower()
    hits = []
    for cat in denied_categories:
        for pat in NO_GO_CATEGORIES.get(cat, []):
            if re.search(pat, t):
                hits.append(cat)
                break
    return hits


# ---------------------------------------------------------------- redaction

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd)\s*[=:]\s*['\"]?)[^\s'\"]{6,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]
_SENSITIVE_ENV = re.compile(r"(TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|CREDENTIAL|PRIVATE_KEY)", re.I)


def _env_secret_values() -> list[str]:
    return [v for k, v in os.environ.items() if _SENSITIVE_ENV.search(k) and v and len(v) >= 8]


def redact(text: str) -> str:
    if not text:
        return text
    for v in _env_secret_values():
        text = text.replace(v, "[REDACTED]")
    for pat in _SECRET_PATTERNS:
        if pat.groups:
            text = pat.sub(lambda m: m.group(1) + "[REDACTED]", text)
        else:
            text = pat.sub("[REDACTED]", text)
    return text


# Environment passed to agent/validation subprocesses: allowlist, not denylist.
SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "USER", "LOGNAME", "SHELL", "TMPDIR", "TZ")


def scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: os.environ[k] for k in SAFE_ENV_KEYS if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra:
        env.update(extra)
    return env
