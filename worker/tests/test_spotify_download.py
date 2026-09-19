"""Spotify plan milestone S2: the supervised download backend.

The child process is stubbed at the module's single seam
(`worker.spotify.run_grouped`), so link policy, argument shape, progress
mapping, failure mapping and the shared validation ladder are all covered
without librespot, Spotify credentials or network access.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from worker.errors import ErrorCode
from worker.input_audio import MAX_FILE_BYTES, InputAudioError, _require_ffmpeg_tool
from worker.spotify import (
    CREDENTIALS_FILE_ENV,
    SpotifyError,
    TrackMetadata,
    download_audio,
)
from worker.spotify_fetch import EXIT_STREAM
from worker.subprocess_group import CommandResult, CommandTimeout

TRACK_ID = "4cOdK2wGLETKBW3PvgPWqT"
TRACK_URL = f"https://open.spotify.com/track/{TRACK_ID}"
DURATION_MS = 240_000  # 4 minutes
ESTIMATED_BYTES = DURATION_MS // 1000 * 40_000


@pytest.fixture(autouse=True)
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Enabled feature, a credentials file, and an offline metadata probe."""
    monkeypatch.setenv("STEMIFY_SPOTIFY_ENABLED", "1")
    credentials = tmp_path / "spotify-credentials.json"
    credentials.write_text('{"username": "operator"}', encoding="utf-8")
    monkeypatch.setenv(CREDENTIALS_FILE_ENV, str(credentials))
    monkeypatch.setattr("worker.spotify.fetch_track_metadata", lambda track_id: None)
    return credentials


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    """The download directory: empty, as the worker's job directory is."""
    path = tmp_path / "source"
    path.mkdir()
    return path


def stub_child(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[dict[str, Any]]:
    """Replace the child-process seam and record how it was invoked."""
    calls: list[dict[str, Any]] = []

    def fake_run(
        args: list[str],
        *,
        timeout_seconds: int,
        on_stdout_line: Any = None,
        env: Any = None,
    ) -> Any:
        calls.append(
            {
                "args": list(args),
                "timeout_seconds": timeout_seconds,
                "env": dict(env or {}),
            }
        )
        return handler(args, on_stdout_line=on_stdout_line)

    monkeypatch.setattr("worker.spotify.run_grouped", fake_run)
    return calls


def stub_metadata(monkeypatch: pytest.MonkeyPatch, *, duration_ms: int) -> None:
    metadata = TrackMetadata(
        track_id=TRACK_ID,
        title="Tease Me",
        artist="Zaylevelten",
        album="Tease Me",
        duration_ms=duration_ms,
    )
    monkeypatch.setattr("worker.spotify.fetch_track_metadata", lambda track_id: metadata)


def frozen_command(monkeypatch: pytest.MonkeyPatch, *, returncode: int = 0, stderr: str = "") -> None:
    monkeypatch.setattr(
        "worker.spotify.run_grouped",
        lambda *args, **kwargs: CommandResult(returncode, "", stderr),
    )


# ---------------------------------------------------------------------------
# Policy: what never reaches a child process
# ---------------------------------------------------------------------------


def test_fixed_arguments_and_env_only_credentials(
    tmp_path: Path, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = stub_child(monkeypatch, lambda *a, **k: CommandResult(0, "", ""))

    with pytest.raises(SpotifyError):  # the child wrote nothing
        download_audio(TRACK_URL, dest)

    assert len(calls) == 1
    args = calls[0]["args"]
    assert args[0] == sys.executable
    assert args[1:3] == ["-m", "worker.spotify_fetch"]
    assert args[3] == "--track" and args[4] == TRACK_ID
    assert args[5] == "--out" and args[6] == str(dest / f"{TRACK_ID}.ogg")
    # The credentials path travels through the environment, never argv.
    assert calls[0]["env"][CREDENTIALS_FILE_ENV] == str(tmp_path / "spotify-credentials.json")
    assert not any("credentials" in str(arg) for arg in args)
    assert calls[0]["timeout_seconds"] >= 60


@pytest.mark.parametrize(
    "url",
    [
        f"https://open.spotify.com/album/{TRACK_ID}",  # v1 is single-track
        "https://spotify.link/abc123",  # short link needs a redirect
        "https://open.spotify.com/track/short",
        "https://evil.example.com/track/" + TRACK_ID,
    ],
)
def test_disallowed_links_never_spawn_a_child(
    url: str, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = stub_child(monkeypatch, lambda *a, **k: CommandResult(0, "", ""))

    with pytest.raises(SpotifyError) as excinfo:
        download_audio(url, dest)

    assert excinfo.value.code == ErrorCode.DOWNLOAD_FAILED
    assert calls == []


def test_kill_switch_off_never_spawns_a_child(
    dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STEMIFY_SPOTIFY_ENABLED", "0")
    calls = stub_child(monkeypatch, lambda *a, **k: CommandResult(0, "", ""))

    with pytest.raises(SpotifyError) as excinfo:
        download_audio(TRACK_URL, dest)

    assert excinfo.value.code == ErrorCode.DOWNLOAD_FAILED
    assert "disabled" in str(excinfo.value)
    assert calls == []


def test_missing_credentials_never_spawn_a_child(
    tmp_path: Path, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(CREDENTIALS_FILE_ENV, raising=False)
    monkeypatch.setenv("STEMIFY_DATA_DIR", str(tmp_path / "empty-data"))
    calls = stub_child(monkeypatch, lambda *a, **k: CommandResult(0, "", ""))

    with pytest.raises(SpotifyError) as excinfo:
        download_audio(TRACK_URL, dest)

    assert excinfo.value.code == ErrorCode.DOWNLOAD_FAILED
    assert "not configured" in str(excinfo.value)
    assert calls == []


# ---------------------------------------------------------------------------
# Failure mapping
# ---------------------------------------------------------------------------


def test_timeout_is_reported_as_a_failed_download(
    dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(*args: Any, **kwargs: Any) -> Any:
        raise CommandTimeout(600)

    stub_child(monkeypatch, handler)

    with pytest.raises(SpotifyError) as excinfo:
        download_audio(TRACK_URL, dest)

    assert excinfo.value.code == ErrorCode.DOWNLOAD_FAILED
    assert "600s" in str(excinfo.value)


def test_child_failure_carries_its_exit_code_and_detail(
    dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The detail stays in the message, which the worker records locally."""
    frozen_command(monkeypatch, returncode=EXIT_STREAM, stderr="spotify-fetch: stream failed: Boom")

    with pytest.raises(SpotifyError) as excinfo:
        download_audio(TRACK_URL, dest)

    assert excinfo.value.code == ErrorCode.DOWNLOAD_FAILED
    assert f"rc={EXIT_STREAM}" in str(excinfo.value)
    assert "Boom" in str(excinfo.value)


def test_unspawnable_child_is_reported_as_a_failed_download(
    dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(*args: Any, **kwargs: Any) -> Any:
        raise OSError("no such interpreter")

    stub_child(monkeypatch, handler)

    with pytest.raises(SpotifyError) as excinfo:
        download_audio(TRACK_URL, dest)

    assert excinfo.value.code == ErrorCode.DOWNLOAD_FAILED


def test_exactly_one_media_file_is_required(
    dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_child(monkeypatch, lambda *a, **k: CommandResult(0, "", ""))
    with pytest.raises(SpotifyError):
        download_audio(TRACK_URL, dest)

    def two_files(args: Any, on_stdout_line: Any = None) -> Any:
        (dest / f"{TRACK_ID}.ogg").write_bytes(b"m")
        (dest / "extra.ogg").write_bytes(b"m")
        return CommandResult(0, "", "")

    stub_child(monkeypatch, two_files)
    with pytest.raises(SpotifyError):
        download_audio(TRACK_URL, dest)


def test_a_part_file_is_not_media(dest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def part_only(args: Any, on_stdout_line: Any = None) -> Any:
        (dest / f"{TRACK_ID}.ogg.part").write_bytes(b"m")
        return CommandResult(0, "", "")

    stub_child(monkeypatch, part_only)
    with pytest.raises(SpotifyError):
        download_audio(TRACK_URL, dest)


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------


def test_byte_counts_become_a_monotonic_percentage(
    dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_metadata(monkeypatch, duration_ms=DURATION_MS)
    seen: list[float] = []

    def handler(args: Any, on_stdout_line: Any = None) -> Any:
        for line in (
            "progress: 2400000\n",  # 25%
            "spotify-fetch: unrelated output\n",  # ignored
            f"progress: {ESTIMATED_BYTES}\n",  # 100% -> capped at 99
            "progress: 100\n",  # must not move backwards
            f"progress: {ESTIMATED_BYTES * 2}\n",  # still capped
        ):
            on_stdout_line(line)
        return CommandResult(0, "", "")

    stub_child(monkeypatch, handler)
    with pytest.raises(SpotifyError):
        download_audio(TRACK_URL, dest, progress_callback=seen.append)

    assert seen == [25.0, 99.0]


def test_no_duration_means_no_percentage(
    dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a known duration, reporting a percentage would be invented."""
    seen: list[float] = []

    def handler(args: Any, on_stdout_line: Any = None) -> Any:
        assert on_stdout_line is None
        return CommandResult(0, "", "")

    stub_child(monkeypatch, handler)
    with pytest.raises(SpotifyError):
        download_audio(TRACK_URL, dest, progress_callback=seen.append)

    assert seen == []


# ---------------------------------------------------------------------------
# The shared validation ladder
# ---------------------------------------------------------------------------


def _make_ogg(path: Path) -> None:
    subprocess.run(
        [
            _require_ffmpeg_tool("ffmpeg"),
            "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
            "-ar", "44100", "-ac", "2", "-c:a", "libvorbis",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def test_happy_path_validates_a_real_ogg(dest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The native Vorbis stream is already in the upload allowlist: no re-encode."""
    try:
        _require_ffmpeg_tool("ffmpeg")
    except Exception:  # noqa: BLE001
        pytest.skip("ffmpeg not available")

    def handler(args: Any, on_stdout_line: Any = None) -> Any:
        _make_ogg(dest / f"{TRACK_ID}.ogg")
        return CommandResult(0, "", "")

    stub_child(monkeypatch, handler)

    try:
        media = download_audio(TRACK_URL, dest)
    except subprocess.CalledProcessError:
        pytest.skip("ffmpeg has no libvorbis encoder")

    assert media.name == f"{TRACK_ID}.ogg"
    assert media.stat().st_size > 0


def test_oversize_child_output_is_rejected_by_the_shared_ladder(
    dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the fetch overshoots, the upload limits are the final authority."""

    def handler(args: Any, on_stdout_line: Any = None) -> Any:
        (dest / f"{TRACK_ID}.ogg").write_bytes(b"\0" * (MAX_FILE_BYTES + 1))
        return CommandResult(0, "", "")

    stub_child(monkeypatch, handler)

    with pytest.raises(InputAudioError) as excinfo:
        download_audio(TRACK_URL, dest)
    assert excinfo.value.code == ErrorCode.LIMIT_EXCEEDED
