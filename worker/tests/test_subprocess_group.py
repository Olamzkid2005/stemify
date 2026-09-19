"""The shared supervised-child-process helper (roadmap: download backends).

Both download backends spawn grandchildren of their own (yt-dlp runs ffmpeg;
the Spotify fetch child runs a protocol client), so their deadline has to kill
the whole tree. The failure mode these tests pin is a worker that looks alive
while a reader blocks forever on a pipe a surviving grandchild still holds.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

import pytest

from worker.subprocess_group import CommandResult, CommandTimeout, run_grouped, spawn_grouped


def test_spawn_is_grouped_and_shell_free(monkeypatch: pytest.MonkeyPatch) -> None:

    captured: dict[str, Any] = {}

    class _FakePopen:
        def __init__(self, args: Any, **kwargs: Any) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr("worker.subprocess_group.subprocess.Popen", _FakePopen)
    spawn_grouped(["python", "-c", "pass"], env={"STEMIFY_TEST_ONLY": "1"})

    kwargs = captured["kwargs"]
    assert kwargs["shell"] is False
    if os.name == "nt":
        assert kwargs["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kwargs["start_new_session"] is True
    # The overlay is additive: the worker's own environment must survive.
    assert kwargs["env"]["STEMIFY_TEST_ONLY"] == "1"
    assert "PATH" in kwargs["env"]


def test_timeout_kills_the_tree_and_raises() -> None:
    """A stalled child, with a grandchild of its own, returns promptly."""
    script = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
        "time.sleep(120)"
    )
    with pytest.raises(CommandTimeout) as excinfo:
        run_grouped([sys.executable, "-c", script], timeout_seconds=10)
    assert excinfo.value.timeout_seconds == 10
    assert "10s" in str(excinfo.value)


def test_stdout_lines_are_streamed_to_the_callback() -> None:
    lines: list[str] = []
    result = run_grouped(
        [sys.executable, "-c", "print('one'); print('two')"],
        timeout_seconds=30,
        on_stdout_line=lines.append,
    )
    assert result.returncode == 0
    assert [line.strip() for line in lines] == ["one", "two"]
    assert result.stdout.splitlines() == ["one", "two"]


def test_a_failing_callback_never_stops_the_drain() -> None:
    """Losing a progress update is fine; stalling the pipe is not."""

    def explosive(line: str) -> None:
        raise RuntimeError("callback blew up")

    result = run_grouped(
        [sys.executable, "-c", "print('still-drained')"],
        timeout_seconds=30,
        on_stdout_line=explosive,
    )
    assert result.returncode == 0
    assert "still-drained" in result.stdout


def test_stderr_and_exit_code_are_returned() -> None:
    result = run_grouped(
        [sys.executable, "-c", "import sys; print('boom', file=sys.stderr); sys.exit(3)"],
        timeout_seconds=30,
    )
    assert result == CommandResult(3, "", "boom\n")
