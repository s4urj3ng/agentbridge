"""Detached execution wrapper.

    python -m agentbridge.workers.wrapper <run_dir> <nonce> <timeout_s> -- argv...

Runs `argv` in its own process group with stdin from run_dir/prompt.txt (if
present) and stdout/stderr to files, then atomically writes run_dir/exit.json.
Because the wrapper is started in a new session it survives an orchestrator
crash; on restart the orchestrator can tell a finished execution (exit.json
present) from a lost one (wrapper gone, no exit.json) and can re-adopt one
that is still running (wrapper alive, verified by nonce).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time


def main() -> int:
    run_dir, nonce, timeout_s = sys.argv[1], sys.argv[2], float(sys.argv[3])
    assert sys.argv[4] == "--"
    argv = sys.argv[5:]
    cwd = os.environ.pop("AB_CWD", None) or os.getcwd()
    prompt = os.path.join(run_dir, "prompt.txt")
    stdin = open(prompt, "rb") if os.path.exists(prompt) else subprocess.DEVNULL
    out = open(os.path.join(run_dir, "stdout.log"), "wb")
    err = open(os.path.join(run_dir, "stderr.log"), "wb")
    started = time.time()
    status = "exited"
    try:
        child = subprocess.Popen(argv, cwd=cwd, stdin=stdin, stdout=out, stderr=err, start_new_session=True)
    except OSError as e:
        err.write(f"wrapper: failed to start {argv[0]!r}: {e}\n".encode())
        _write_exit(run_dir, nonce, 127, started, "spawn_error")
        return 127

    def _term(signum, frame):  # orchestrator cancel → kill the child's group
        _kill_group(child.pid)
        _write_exit(run_dir, nonce, -signum, started, "killed")
        os._exit(1)

    signal.signal(signal.SIGTERM, _term)
    try:
        code = child.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_group(child.pid)
        code = child.wait()
        status = "timeout"
    _write_exit(run_dir, nonce, code, started, status)
    return 0


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
        for _ in range(50):
            time.sleep(0.1)
            os.killpg(pid, 0)
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        pass


def _write_exit(run_dir: str, nonce: str, code: int, started: float, status: str) -> None:
    tmp = os.path.join(run_dir, "exit.json.tmp")
    with open(tmp, "w") as f:
        json.dump({"nonce": nonce, "exit_code": code, "status": status,
                   "duration_s": round(time.time() - started, 3), "ended_at": time.time()}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, os.path.join(run_dir, "exit.json"))


if __name__ == "__main__":
    sys.exit(main())
