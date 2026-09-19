"""Run one child process in its own group under a hard wall-clock deadline.

Both download backends spawn grandchildren of their own (yt-dlp runs ffmpeg;
the Spotify fetch child runs its own protocol client) and both must be killable
*as a tree*. Killing only the parent leaves the grandchild holding the inherited
stdout/stderr pipes, and the reader then blocks forever instead of reporting the
timeout — the worker looks hung while its heartbeat keeps reporting it alive.

Callers own the policy: fixed argument arrays, never a shell, and what the
output means. This module owns only the supervision.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple

# A killed tree still has to be reaped: both waits are bounded so supervision
# itself can never become the thing that hangs.
KILL_TIMEOUT_SECONDS = 15
WAIT_AFTER_KILL_SECONDS = 10
READER_JOIN_SECONDS = 10


class CommandTimeout(RuntimeError):
    """The child exceeded its deadline and its whole process tree was killed."""

    def __init__(self, timeout_seconds: int) -> None:
        super().__init__(
            f"child process exceeded the {timeout_seconds}s timeout and was terminated"
        )
        self.timeout_seconds = timeout_seconds


class CommandResult(NamedTuple):
    """Outcome of one supervised child process."""

    returncode: int
    stdout: str
    stderr: str


def spawn_grouped(
    args: list[str], *, env: Mapping[str, str] | None = None
) -> subprocess.Popen[str]:
    """Start a child in its own process group so a timeout can kill its tree.

    A new process group/session is what makes a tree-wide kill possible
    (`taskkill /T` on Windows, `killpg` on POSIX). `env` overlays the parent
    environment for this child only, so job-scoped settings never leak into the
    worker's own process.
    """
    kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "shell": False,  # fixed argument array only (plan Section 13.6)
        "bufsize": 1,
    }
    if env is not None:
        kwargs["env"] = {**os.environ, **env}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    # No literal timeout here: `run_grouped` owns the deadline (wait(timeout=...)
    # plus a tree-wide kill), which is what makes the limit enforceable at all.
    return subprocess.Popen(args, **kwargs)  # timeout: enforced by run_grouped


def kill_tree(process: subprocess.Popen[str]) -> None:
    """Kill the process and every descendant it spawned."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
                timeout=KILL_TIMEOUT_SECONDS,
            )
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        with contextlib.suppress(OSError):
            process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        process.wait(timeout=WAIT_AFTER_KILL_SECONDS)


def run_grouped(
    args: list[str],
    *,
    timeout_seconds: int,
    on_stdout_line: Callable[[str], None] | None = None,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run one child; kill the whole tree and raise on timeout.

    stdout is drained line by line (`on_stdout_line` sees each line as it
    arrives) and stderr is buffered — both on background threads, because a
    child's chatter on either pipe can fill up and block it. A failure inside
    `on_stdout_line` is swallowed for the same reason: losing a progress update
    is fine, stalling the drain is not.
    """
    process = spawn_grouped(args, env=env)

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def drain(stream: Any, sink: Callable[[str], None]) -> None:
        try:
            for line in stream:
                try:
                    sink(line)
                except Exception:  # noqa: BLE001, S112 - a sink failure must not stop the drain
                    continue
        except (ValueError, OSError):  # pipe closed under us during a tree kill
            return

    def read_stdout(line: str) -> None:
        stdout_lines.append(line)
        if on_stdout_line is not None:
            on_stdout_line(line)

    readers = [
        threading.Thread(target=drain, args=(process.stdout, read_stdout), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr_lines.append), daemon=True),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(process)
        returncode = process.returncode if process.returncode is not None else -1
    finally:
        for reader in readers:
            reader.join(timeout=READER_JOIN_SECONDS)
        for stream in (process.stdout, process.stderr):
            with contextlib.suppress(OSError, ValueError):
                if stream is not None:
                    stream.close()

    if timed_out:
        raise CommandTimeout(timeout_seconds)
    return CommandResult(returncode, "".join(stdout_lines), "".join(stderr_lines))
