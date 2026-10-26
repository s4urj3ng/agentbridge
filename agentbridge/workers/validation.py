"""Deterministic validation: runs before any additional AI review."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..policies.safety import PolicyViolation, check_validation_command, redact, scrubbed_env
from .process import run_cmd


@dataclass
class ValidationResult:
    passed: bool
    results: list[dict] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{r['name']}: {'PASS' if r['exit_code'] == 0 else 'FAIL'}" for r in self.results]
        parts += [f"POLICY: {v}" for v in self.violations]
        return "; ".join(parts) or "no validation commands configured"


def run_validation(worktree: Path, names: list[str], manifest_cmds: dict[str, list[str]],
                   allowed_exes: list[str], out_dir: Path, timeout_s: float) -> ValidationResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    res = ValidationResult(passed=True)
    for name in names:
        try:
            argv = check_validation_command(name, manifest_cmds, allowed_exes)
        except PolicyViolation as e:
            res.violations.append(str(e))
            res.passed = False
            continue
        r = run_cmd(argv, cwd=worktree, timeout_s=timeout_s,
                    env=scrubbed_env({"CI": "1", "AGENTBRIDGE_VALIDATION": "1"}))
        log = out_dir / f"validation-{name}.log"
        log.write_text(redact(f"$ {' '.join(argv)}\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}\n"))
        tail = redact((r.stdout + "\n" + r.stderr).strip())[-1200:]
        res.results.append({"name": name, "argv": argv, "exit_code": r.exit_code, "timed_out": r.timed_out,
                            "duration_s": round(r.duration_s, 2), "log": str(log), "tail": tail})
        if r.exit_code != 0:
            res.passed = False
    return res
