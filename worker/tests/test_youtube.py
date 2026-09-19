"""Task 14 tests: controlled yt-dlp download (plan Section 9.6).

Covers the URL allowlist re-validation, the kill switch, argument policy
(fixed args, `--`, no shell), timeout, size/duration enforcement, and the
missing-yt-dlp path. Network access is never required: subprocess calls are
stubbed, and the happy path validates a real local MP3 through the real
`validate_source` ladder.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from worker.errors import ErrorCode
from worker.input_audio import InputAudioError, _require_ffmpeg_tool
from worker.youtube import (
    DownloadError,
    download_audio,
    is_allowed_youtube_url,
    youtube_enabled,
    yt_dlp_command,
)

VALID_URL = "https://www.youtube.com/watch?v=abc123"


def stub_yt_dlp_subprocess(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """Intercept yt-dlp invocations only; ffprobe/ffmpeg calls stay real.

    `_run_yt_dlp` is the module's single seam for launching yt-dlp, and it is
    patched on `worker.youtube` alone — the shared `subprocess` module is left
    untouched, so `validate_source` still runs the real ffprobe/ffmpeg ladder.
    """
    real_which = shutil.which

    def selective_which(name: str, *args: Any, **kwargs: Any) -> Any:
        # Only yt-dlp is faked: ffprobe/ffmpeg must resolve for real, because
        # shutil is a shared module and validate_source needs the real tools.
        return "yt-dlp" if name == "yt-dlp" else real_which(name, *args, **kwargs)

    def fake_run(args: Any, *, timeout_seconds: int, on_progress: Any = None) -> Any:
        return handler(args, on_progress=on_progress)

    monkeypatch.setattr("worker.youtube.shutil.which", selective_which)
    monkeypatch.setattr("worker.youtube._run_yt_dlp", fake_run)


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> Any:
    """Build the result object the real runner returns."""
    from worker.youtube import _YtDlpResult

    return _YtDlpResult(returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# URL allowlist (worker-side re-validation)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc123",
        "https://youtube.com/watch?v=abc123",
        "https://youtu.be/abc123",
        "https://www.youtube.com/shorts/abc123",
    ],
)
def test_allowlist_accepts_youtube_hosts(url: str) -> None:
    assert is_allowed_youtube_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.youtube.com/watch?v=abc123",  # not https
        "https://evil.example.com/watch?v=x",  # wrong host
        "https://youtube.com.evil.com/watch?v=x",  # suffix spoof
        "https://user@evil.com@www.youtube.com/watch?v=x",  # userinfo trick
        "https://www.youtube.com/",  # empty path
        "not a url at all",
        "",
        "https://www.youtube.com/watch?v=" + "x" * 2100,  # over 2048 chars
    ],
)
def test_allowlist_rejects_everything_else(url: str) -> None:
    assert not is_allowed_youtube_url(url)


def test_allowlist_rejects_non_string() -> None:
    assert not is_allowed_youtube_url(None)  # type: ignore[arg-type]
    assert not is_allowed_youtube_url(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Kill switch and yt-dlp presence
# ---------------------------------------------------------------------------


def test_kill_switch_disables_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STEMIFY_YOUTUBE_ENABLED", "0")
    assert youtube_enabled() is False
    with pytest.raises(DownloadError) as excinfo:
        download_audio(VALID_URL, tmp_path)
    assert excinfo.value.code == ErrorCode.DOWNLOAD_FAILED


def test_missing_yt_dlp_fails_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No binary on PATH and no module: DOWNLOAD_FAILED, never a crash."""
    import sys

    monkeypatch.setattr("worker.youtube.shutil.which", lambda name: None)
    monkeypatch.setitem(sys.modules, "yt_dlp", None)  # forces ImportError
    real_import = __import__

    def block_yt_dlp(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "yt_dlp":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", block_yt_dlp)
    with pytest.raises(DownloadError) as excinfo:
        yt_dlp_command()
    assert excinfo.value.code == ErrorCode.DOWNLOAD_FAILED


def test_yt_dlp_prefers_path_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("worker.youtube.shutil.which", lambda name: "/usr/local/bin/yt-dlp")
    assert yt_dlp_command() == ["/usr/local/bin/yt-dlp"]


# ---------------------------------------------------------------------------
# Argument policy
# ---------------------------------------------------------------------------


def test_download_uses_fixed_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One video, audio-only, fixed size cap, `--` before the URL, no shell."""
    captured: dict[str, Any] = {}

    def handler(args: Any, on_progress: Any = None) -> Any:
        captured["args"] = args
        # A real decodable MP3 (the mp3 header matters to ffprobe downstream).
        subprocess.run(
            [
                _require_ffmpeg_tool("ffmpeg"),
                "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
                "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k",
                str(tmp_path / "abc123.mp3"),
            ],
            check=True,
            capture_output=True,
        )
        return _result(0)

    stub_yt_dlp_subprocess(monkeypatch, handler)

    try:
        _require_ffmpeg_tool("ffmpeg")
    except Exception:  # noqa: BLE001
        pytest.skip("ffmpeg not available")

    download_audio(VALID_URL, tmp_path)

    args = captured["args"]
    assert "--no-playlist" in args
    assert "--extract-audio" in args
    assert "--" in args
    assert args[-1] == VALID_URL  # URL comes last, after `--`
    from worker.input_audio import MAX_FILE_BYTES

    assert str(MAX_FILE_BYTES) in args  # size cap is passed to yt-dlp


def test_spawn_uses_isolated_process_group_and_no_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child must be killable as a tree and never run through a shell."""
    from worker.youtube import _spawn_yt_dlp

    captured: dict[str, Any] = {}

    class _FakePopen:
        def __init__(self, args: Any, **kwargs: Any) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr("worker.youtube.subprocess.Popen", _FakePopen)
    _spawn_yt_dlp(["yt-dlp", "--version"])

    assert captured["kwargs"]["shell"] is False
    if os.name == "nt":
        assert captured["kwargs"]["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert captured["kwargs"]["start_new_session"] is True


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_disallowed_url_fails_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("yt-dlp must not run for disallowed URLs")

    monkeypatch.setattr("worker.youtube._run_yt_dlp", explode)
    with pytest.raises(DownloadError) as excinfo:
        download_audio("https://evil.example.com/watch?v=x", tmp_path)
    assert excinfo.value.code == ErrorCode.DOWNLOAD_FAILED


def test_nonzero_exit_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_yt_dlp_subprocess(
        monkeypatch, lambda *a, **k: _result(1, stderr="Video unavailable")
    )
    with pytest.raises(DownloadError) as excinfo:
        download_audio(VALID_URL, tmp_path)
    assert excinfo.value.code == ErrorCode.DOWNLOAD_FAILED


# ---------------------------------------------------------------------------
# Hard timeout and process-tree kill
# ---------------------------------------------------------------------------


def test_run_yt_dlp_kills_process_tree_on_timeout() -> None:
    """A timed-out yt-dlp must not leave a grandchild holding the pipes.

    The download runs ffmpeg as a child of yt-dlp. Killing only the parent left
    ffmpeg alive holding the inherited stdout/stderr pipes, and the reader then
    blocked forever — the worker looked hung while its heartbeat stayed fresh.
    """
    from worker.youtube import _run_yt_dlp

    child = "import time; time.sleep(120)"
    script = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "time.sleep(120)"
    )
    started = time.monotonic()
    with pytest.raises(DownloadError) as excinfo:
        _run_yt_dlp([sys.executable, "-c", script], timeout_seconds=10)
    elapsed = time.monotonic() - started

    assert excinfo.value.code == ErrorCode.DOWNLOAD_FAILED
    assert "timeout" in str(excinfo.value)
    # The point of the test: it returns promptly instead of blocking forever.
    assert elapsed < 60


def test_run_yt_dlp_streams_download_progress() -> None:
    """Progress lines reach the callback while the process runs."""
    from worker.youtube import _run_yt_dlp

    script = (
        "print('[download]   0.0% of 1MiB'); "
        "print('[download]  50.0% of 1MiB'); "
        "print('[download] 100.0% of 1MiB')"
    )
    seen: list[float] = []
    result = _run_yt_dlp(
        [sys.executable, "-c", script], timeout_seconds=30, on_progress=seen.append
    )

    assert result.returncode == 0
    assert seen == [0.0, 50.0, 100.0]


def test_run_yt_dlp_buffers_stderr_and_exit_code() -> None:
    from worker.youtube import _run_yt_dlp

    result = _run_yt_dlp(
        [sys.executable, "-c", "import sys; print('boom', file=sys.stderr); sys.exit(3)"],
        timeout_seconds=30,
    )

    assert result.returncode == 3
    assert "boom" in result.stderr


def test_download_forwards_progress_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """yt-dlp's download percent reaches the worker's progress callback."""
    seen: list[float] = []

    def handler(args: Any, on_progress: Any = None) -> Any:
        on_progress(42.0)
        (tmp_path / "abc123.mp3").write_bytes(b"not audio")
        return _result(0)

    stub_yt_dlp_subprocess(monkeypatch, handler)
    # The junk file still fails the real validation ladder, but only after the
    # progress callback has been forwarded.
    with pytest.raises(InputAudioError):
        download_audio(VALID_URL, tmp_path, progress_callback=seen.append)
    assert seen == [42.0]


def test_zero_or_multiple_files_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_yt_dlp_subprocess(monkeypatch, lambda *a, **k: _result(0))  # creates nothing
    with pytest.raises(DownloadError):
        download_audio(VALID_URL, tmp_path)

    def two_files(*args: Any, **kwargs: Any) -> Any:
        (tmp_path / "a.mp3").write_bytes(b"m")
        (tmp_path / "b.mp3").write_bytes(b"m")
        return _result(0)

    stub_yt_dlp_subprocess(monkeypatch, two_files)
    with pytest.raises(DownloadError):
        download_audio(VALID_URL, tmp_path)


def test_part_files_are_not_media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A .part leftover must not satisfy the exactly-one-media check."""
    stub_yt_dlp_subprocess(monkeypatch, lambda *a, **k: _result(0))
    (tmp_path / "abc.mp3.part").write_bytes(b"m")
    with pytest.raises(DownloadError):
        download_audio(VALID_URL, tmp_path)


# ---------------------------------------------------------------------------
# Happy path: real validation ladder on a real MP3
# ---------------------------------------------------------------------------


def test_happy_path_validates_media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real MP3 fixture is downloaded and passes the real validate_source."""
    try:
        _require_ffmpeg_tool("ffmpeg")
    except Exception:  # noqa: BLE001
        pytest.skip("ffmpeg not available")

    def make_tone(args: Any, on_progress: Any = None) -> Any:
        # Create the media file the fake yt-dlp invocation would have produced.
        subprocess.run(
            [
                _require_ffmpeg_tool("ffmpeg"),
                "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
                "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k",
                str(tmp_path / "abc123.mp3"),
            ],
            check=True,
            capture_output=True,
        )
        return _result(0)

    stub_yt_dlp_subprocess(monkeypatch, make_tone)

    media = download_audio(VALID_URL, tmp_path)
    assert media.name == "abc123.mp3"
    assert media.stat().st_size > 0


def test_happy_path_rejects_oversize_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if yt-dlp overshoots its own cap, validate_source stops it."""

    def oversize(args: Any, on_progress: Any = None) -> Any:
        from worker.input_audio import MAX_FILE_BYTES

        (tmp_path / "big.mp3").write_bytes(b"\0" * (MAX_FILE_BYTES + 1))
        return _result(0)

    stub_yt_dlp_subprocess(monkeypatch, oversize)
    # validate_source raises the InputAudioError parent (LIMIT_EXCEEDED),
    # which is exactly the code uploads get — the limits are shared.
    with pytest.raises(InputAudioError) as excinfo:
        download_audio(VALID_URL, tmp_path)
    assert excinfo.value.code == ErrorCode.LIMIT_EXCEEDED
