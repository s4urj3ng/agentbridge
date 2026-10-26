"""Launch and supervise wrapper processes; run short deterministic commands."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ..policies.safety import redact


@dataclass
class ExitInfo:
    exit_code: int
    status: str        # exited | timeout | killed | spawn_error
    duration_s: float


def spawn_detached(argv: list[str], *, run_dir: Path, cwd: Path, nonce: str, timeout_s: float,
                   env: dict[str, str]) -> int:
    """Start the wrapper in a new session; returns the wrapper pid."""
    run_dir.mkdir(parents=True, exist_ok=True)
    wenv = dict(env)
    wenv["AB_CWD"] = str(cwd)
    # PYTHONPATH so the wrapper module resolves even with a scrubbed env
    pkg_root = str(Path(__file__).resolve().parents[2])
    wenv["PYTHONPATH"] = pkg_root
    with open(run_dir / "wrapper.log", "ab") as log:
        p = subprocess.Popen(
            [sys.executable, "-m", "agentbridge.workers.wrapper", str(run_dir), nonce, str(timeout_s), "--", *argv],
            cwd=str(cwd), env=wenv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True)
    return p.pid


def read_exit(run_dir: Path, nonce: str) -> ExitInfo | None:
    f = run_dir / "exit.json"
    if not f.exists():
        return None
    try:
        d = json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if d.get("nonce") != nonce:
        return None
    return ExitInfo(int(d["exit_code"]), d.get("status", "exited"), float(d.get("duration_s", 0)))


def pid_alive_with_nonce(pid: int | None, nonce: str) -> bool:
    """True only if `pid` is alive AND is our wrapper (nonce in its cmdline).

    Guards against PID reuse after a reboot.
    """
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return nonce in out


def reap(pid: int) -> None:
    """Collect a finished child so it does not linger as a zombie."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass


def terminate(pid: int | None, nonce: str) -> bool:
    if not pid_alive_with_nonce(pid, nonce):
        return False
    try:
        os.kill(pid, signal.SIGTERM)  # wrapper kills the child's group, writes exit.json
    except ProcessLookupError:
        return False
    for _ in range(30):
        time.sleep(0.1)
        reap(pid)
        if not pid_alive_with_nonce(pid, nonce):
            return True
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    return True


async def wait_exit(pid: int, run_dir: Path, nonce: str, deadline: float, poll: float = 0.25) -> ExitInfo | None:
    """Poll until exit.json appears, the wrapper dies, or deadline passes."""
    while True:
        reap(pid)
        info = read_exit(run_dir, nonce)
        if info:
            return info
        if not pid_alive_with_nonce(pid, nonce):
            await asyncio.sleep(0.2)  # exit.json write may race the process exit
            return read_exit(run_dir, nonce)
        if time.time() > deadline:
            return None
        await asyncio.sleep(poll)


@dataclass
class CmdResult:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False


def run_cmd(argv: list[str], *, cwd: Path | str, timeout_s: float = 120, env: dict[str, str] | None = None,
            check: bool = False) -> CmdResult:
    """Run a short command in its own process group; kill the group on timeout."""
    t0 = time.time()
    p = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         stdin=subprocess.DEVNULL, start_new_session=True, text=True)
    try:
        out, err = p.communicate(timeout=timeout_s)
        timed_out = False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = p.communicate()
        timed_out = True
    res = CmdResult(argv, p.returncode if not timed_out else -9, out, err, time.time() - t0, timed_out)
    if check and res.exit_code != 0:
        raise RuntimeError(f"{' '.join(argv)} failed ({res.exit_code}): {redact(err.strip())[:2000]}")
    return res
