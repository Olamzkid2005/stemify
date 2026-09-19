"""Spotify plan milestone S2: the fetch child process.

No network and no real dependency: a minimal fake `librespot` package is
injected into sys.modules, so the child's real logic (argument parsing, the
stream loop, part-file handling, exit codes) is exercised without the alpha
library installed. The one-off interactive login is a manual step and is not
tested here.
"""

from __future__ import annotations

import builtins
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from worker import spotify_fetch
from worker.spotify_fetch import (
    CREDENTIALS_ENV,
    EXIT_AUTH,
    EXIT_DEPENDENCY,
    EXIT_OK,
    EXIT_STREAM,
    EXIT_USAGE,
    EXIT_WRITE,
)

TRACK_ID = "4cOdK2wGLETKBW3PvgPWqT"
CHUNKS = [b"OggS" + b"\x00" * 20, b"more-audio"]
PARTIAL_CHUNKS = [b"partial-data"]


class _FakeStream:
    """Mimics `stream.input_stream.stream()`: chunks, then end-of-stream."""

    def __init__(self, chunks: list[Any], error: Exception | None = None) -> None:
        self._chunks = list(chunks)
        self._error = error

    def read(self, size: int) -> Any:
        if self._chunks:
            return self._chunks.pop(0)
        if self._error is not None:
            raise self._error
        return b""


class _ContentFeeder:
    def __init__(self, stream: _FakeStream, seen: dict[str, Any]) -> None:
        self._stream = stream
        self._seen = seen

    def load(self, track: Any, quality: Any, preload: Any, timeout: Any) -> Any:
        self._seen["load"] = {
            "track": track,
            "quality": quality,
            "preload": preload,
            "timeout": timeout,
        }
        return SimpleNamespace(input_stream=SimpleNamespace(stream=lambda: self._stream))


def _module(name: str, **attributes: Any) -> ModuleType:
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def install_fake_librespot(
    monkeypatch: pytest.MonkeyPatch,
    *,
    chunks: list[Any] | None = None,
    session_error: Exception | None = None,
    stream_error: Exception | None = None,
) -> dict[str, Any]:
    """Inject a minimal client library and record what the child requested."""
    seen: dict[str, Any] = {}
    stream = _FakeStream(CHUNKS if chunks is None else chunks, stream_error)

    class _Builder:
        def stored_file(self, path: str) -> Any:
            seen["credentials"] = path
            return self

        def create(self) -> Any:
            if session_error is not None:
                raise session_error
            return SimpleNamespace(content_feeder=lambda: _ContentFeeder(stream, seen))

    class _Session:
        Builder = _Builder

    class _TrackId:
        @staticmethod
        def from_uri(uri: str) -> str:
            seen["uri"] = uri
            return uri

    class _AudioQuality:
        VERY_HIGH = "VERY_HIGH"

    class _VorbisOnlyAudioQuality:
        def __init__(self, quality: Any) -> None:
            seen["quality"] = quality

    for name, module in {
        "librespot": _module("librespot"),
        "librespot.core": _module("librespot.core", Session=_Session),
        "librespot.metadata": _module("librespot.metadata", TrackId=_TrackId),
        "librespot.audio": _module("librespot.audio"),
        "librespot.audio.decoders": _module(
            "librespot.audio.decoders",
            AudioQuality=_AudioQuality,
            VorbisOnlyAudioQuality=_VorbisOnlyAudioQuality,
        ),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    return seen


def write_credentials(tmp_path: Path) -> Path:
    credentials = tmp_path / "spotify-credentials.json"
    credentials.write_text('{"username": "operator"}', encoding="utf-8")
    return credentials


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_fetch_streams_the_track_and_renames_from_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = install_fake_librespot(monkeypatch)
    credentials = write_credentials(tmp_path)
    out = tmp_path / f"{TRACK_ID}.ogg"

    code = spotify_fetch.main(
        ["--track", TRACK_ID, "--out", str(out), "--credentials", str(credentials)]
    )

    assert code == EXIT_OK
    assert out.read_bytes() == b"".join(CHUNKS)
    assert not (tmp_path / f"{TRACK_ID}.ogg.part").exists()
    assert seen["uri"] == f"spotify:track:{TRACK_ID}"
    assert seen["quality"] == "VERY_HIGH"  # highest quality Vorbis, not a downgrade
    assert seen["load"]["preload"] is False
    assert seen["credentials"] == str(credentials)

    progress = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("progress: ")
    ]
    first = len(CHUNKS[0])
    assert progress == [f"progress: {first}", f"progress: {first + len(CHUNKS[1])}"]


def test_credentials_come_from_the_environment_when_the_flag_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = install_fake_librespot(monkeypatch)
    credentials = write_credentials(tmp_path)
    monkeypatch.setenv(CREDENTIALS_ENV, str(credentials))

    code = spotify_fetch.main(["--track", TRACK_ID, "--out", str(tmp_path / "out.ogg")])

    assert code == EXIT_OK
    assert seen["credentials"] == str(credentials)


def test_a_minus_one_read_ends_the_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client library signals end-of-voice-data with -1, not only b""."""
    install_fake_librespot(monkeypatch, chunks=[b"data", -1, b"never-written"])
    credentials = write_credentials(tmp_path)
    out = tmp_path / f"{TRACK_ID}.ogg"

    code = spotify_fetch.main(
        ["--track", TRACK_ID, "--out", str(out), "--credentials", str(credentials)]
    )

    assert code == EXIT_OK
    assert out.read_bytes() == b"data"


# ---------------------------------------------------------------------------
# Failure paths: one exit code each
# ---------------------------------------------------------------------------


def test_missing_library_exits_with_the_dependency_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = write_credentials(tmp_path)
    out = tmp_path / f"{TRACK_ID}.ogg"
    real_import = builtins.__import__

    def block_librespot(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "librespot" or name.startswith("librespot."):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_librespot)
    code = spotify_fetch.main(
        ["--track", TRACK_ID, "--out", str(out), "--credentials", str(credentials)]
    )

    assert code == EXIT_DEPENDENCY
    assert not out.exists()


def test_sign_in_failure_exits_with_the_auth_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_librespot(monkeypatch, session_error=RuntimeError("bad credentials"))
    credentials = write_credentials(tmp_path)
    out = tmp_path / f"{TRACK_ID}.ogg"

    code = spotify_fetch.main(
        ["--track", TRACK_ID, "--out", str(out), "--credentials", str(credentials)]
    )

    assert code == EXIT_AUTH
    assert not out.exists()
    assert not (tmp_path / f"{TRACK_ID}.ogg.part").exists()


def test_stream_failure_exits_stream_and_cleans_the_part_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_librespot(
        monkeypatch, chunks=PARTIAL_CHUNKS, stream_error=RuntimeError("protocol error")
    )
    credentials = write_credentials(tmp_path)
    out = tmp_path / f"{TRACK_ID}.ogg"

    code = spotify_fetch.main(
        ["--track", TRACK_ID, "--out", str(out), "--credentials", str(credentials)]
    )

    assert code == EXIT_STREAM
    assert not out.exists()
    # A partial stream must never look like media to the validation ladder.
    assert not (tmp_path / f"{TRACK_ID}.ogg.part").exists()


def test_unwritable_output_exits_with_the_write_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_librespot(monkeypatch)
    credentials = write_credentials(tmp_path)
    out = tmp_path / "missing-dir" / f"{TRACK_ID}.ogg"

    code = spotify_fetch.main(
        ["--track", TRACK_ID, "--out", str(out), "--credentials", str(credentials)]
    )

    assert code == EXIT_WRITE


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--track", "too-short"],
        ["--track", TRACK_ID],  # no --out
        ["--track", TRACK_ID, "--out", "out.ogg", "--credentials"],  # flag without a value
        ["--exec", "rm -rf /"],  # unknown flag
        ["https://open.spotify.com/track/" + TRACK_ID],  # a link, never an id
    ],
)
def test_usage_errors_are_rejected(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CREDENTIALS_ENV, str(write_credentials(tmp_path)))
    assert spotify_fetch.main(argv) == EXIT_USAGE


def test_missing_credentials_file_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(CREDENTIALS_ENV, raising=False)
    argv = ["--track", TRACK_ID, "--out", str(tmp_path / "out.ogg")]
    assert spotify_fetch.main(argv) == EXIT_USAGE

    monkeypatch.setenv(CREDENTIALS_ENV, str(tmp_path / "nope.json"))
    assert spotify_fetch.main(argv) == EXIT_USAGE


def test_no_traceback_ever_escapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every failure is an exit code plus a one-line report, never a crash."""
    install_fake_librespot(monkeypatch, session_error=ValueError("nope"))
    credentials = write_credentials(tmp_path)
    code = spotify_fetch.main(
        ["--track", TRACK_ID, "--out", str(tmp_path / "out.ogg"), "--credentials", str(credentials)]
    )
    captured = capsys.readouterr()
    assert code == EXIT_AUTH
    assert "Traceback" not in captured.out + captured.err
    assert "nope" in captured.err
