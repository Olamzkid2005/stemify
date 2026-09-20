"""Spotify plan milestone S2: the fetch child process.

No network and no real dependency: a minimal fake `librespot` package is
injected into sys.modules, so the child's real logic (argument parsing, the
stream loop, part-file handling, session configuration, exit codes) is
exercised without the alpha library installed. The operator login is covered
separately in `tests/test_spotify.py`.
"""

from __future__ import annotations

import builtins
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Self

import pytest

from worker import spotify_fetch
from worker.spotify import cache_dir_path
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


class _Api:
    """Mimics `session.api().get_metadata_4_track(...)`."""

    def __init__(
        self, metadata: dict[str, Any], seen: dict[str, Any], error: Exception | None
    ) -> None:
        self._metadata = metadata
        self._seen = seen
        self._error = error

    def get_metadata_4_track(self, track: Any) -> Any:
        self._seen["metadata_track"] = track
        if self._error is not None:
            raise self._error
        covers = [
            SimpleNamespace(file_id=file_id, size=size)
            for file_id, size in self._metadata.get("covers", [])
        ]
        return SimpleNamespace(
            name=self._metadata.get("name"),
            artist=[SimpleNamespace(name=name) for name in self._metadata.get("artists", [])],
            duration=self._metadata.get("duration", 0),
            album=SimpleNamespace(
                name=self._metadata.get("album"),
                cover_group=SimpleNamespace(image=covers),
            ),
        )


def install_fake_librespot(
    monkeypatch: pytest.MonkeyPatch,
    *,
    chunks: list[Any] | None = None,
    session_error: Exception | None = None,
    stream_error: Exception | None = None,
    track_metadata: dict[str, Any] | None = None,
    metadata_error: Exception | None = None,
) -> dict[str, Any]:
    """Inject a minimal client library and record what the child requested.

    `track_metadata` is off by default, which is what a library without the
    metadata call looks like: the child must then fetch exactly as before.
    """
    seen: dict[str, Any] = {}
    stream = _FakeStream(CHUNKS if chunks is None else chunks, stream_error)

    class _ConfigurationBuilder:
        def __init__(self) -> None:
            self.settings: dict[str, Any] = {}

        def set_store_credentials(self, value: Any) -> Any:
            self.settings["store_credentials"] = value
            return self

        def set_stored_credential_file(self, value: Any) -> Any:
            self.settings["stored_credentials_file"] = value
            return self

        def set_cache_dir(self, value: Any) -> Any:
            self.settings["cache_dir"] = value
            return self

        def set_cache_enabled(self, value: Any) -> Any:
            self.settings["cache_enabled"] = value
            return self

        def set_do_cache_clean_up(self, value: Any) -> Any:
            self.settings["do_cache_clean_up"] = value
            return self

        def build(self) -> Any:
            return SimpleNamespace(**self.settings)

    class _Builder:
        def __init__(self, configuration: Any = None) -> None:
            seen["configuration"] = configuration

        def stored_file(self, path: str) -> Any:
            seen["credentials"] = path
            return self

        def create(self) -> Any:
            # What the library sees on disk once the session is built, which is
            # exactly when a real one would start using the cache directory.
            seen["cache_dir_at_create"] = cache_dir_path(Path(seen["credentials"])).is_dir()
            if session_error is not None:
                raise session_error
            session = SimpleNamespace(content_feeder=lambda: _ContentFeeder(stream, seen))
            if track_metadata is not None or metadata_error is not None:
                session.api = lambda: _Api(track_metadata or {}, seen, metadata_error)
            return session

    class _Session:
        Builder = _Builder
        Configuration = SimpleNamespace(Builder=_ConfigurationBuilder)

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


def test_the_tracks_own_metadata_is_reported_before_the_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Naming and the parent's progress estimate need no Web API application."""
    seen = install_fake_librespot(
        monkeypatch,
        track_metadata={
            "name": "Tease Me",
            "artists": ["Zaylevelten"],
            "album": "Tease Me",
            "duration": 240_000,
        },
    )
    credentials = write_credentials(tmp_path)
    out = tmp_path / f"{TRACK_ID}.ogg"

    code = spotify_fetch.main(
        ["--track", TRACK_ID, "--out", str(out), "--credentials", str(credentials)]
    )

    assert code == EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    metadata_line = next(line for line in lines if line.startswith("metadata: "))
    assert json.loads(metadata_line[len("metadata: ") :]) == {
        "track_id": TRACK_ID,
        "title": "Tease Me",
        "artist": "Zaylevelten",
        "album": "Tease Me",
        "duration_ms": 240_000,
    }
    assert seen["metadata_track"] == f"spotify:track:{TRACK_ID}"
    # The metadata line is printed before any byte count, so the parent can size
    # its estimate from the very first progress line.
    assert lines.index(metadata_line) < next(
        index for index, line in enumerate(lines) if line.startswith("progress: ")
    )


def test_metadata_failures_never_disturb_the_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Metadata is a convenience: a failing call must still produce the audio."""
    install_fake_librespot(monkeypatch, metadata_error=RuntimeError("no metadata"))
    credentials = write_credentials(tmp_path)
    out = tmp_path / f"{TRACK_ID}.ogg"

    code = spotify_fetch.main(
        ["--track", TRACK_ID, "--out", str(out), "--credentials", str(credentials)]
    )

    assert code == EXIT_OK
    assert out.read_bytes() == b"".join(CHUNKS)
    assert not [line for line in capsys.readouterr().out.splitlines() if "metadata: " in line]


def test_a_library_without_the_metadata_call_still_fetches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default fake exposes no `api()`: the child must carry on regardless."""
    install_fake_librespot(monkeypatch)
    credentials = write_credentials(tmp_path)
    out = tmp_path / f"{TRACK_ID}.ogg"

    code = spotify_fetch.main(
        ["--track", TRACK_ID, "--out", str(out), "--credentials", str(credentials)]
    )

    assert code == EXIT_OK
    assert out.read_bytes() == b"".join(CHUNKS)
    assert not [line for line in capsys.readouterr().out.splitlines() if "metadata: " in line]


def test_an_unusable_title_is_not_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A blank title would name a job "" — report nothing and fall back."""
    install_fake_librespot(
        monkeypatch, track_metadata={"name": "   ", "artists": [], "duration": 0}
    )
    credentials = write_credentials(tmp_path)
    out = tmp_path / f"{TRACK_ID}.ogg"

    code = spotify_fetch.main(
        ["--track", TRACK_ID, "--out", str(out), "--credentials", str(credentials)]
    )

    assert code == EXIT_OK
    assert not [line for line in capsys.readouterr().out.splitlines() if "metadata: " in line]


# ---------------------------------------------------------------------------
# Album artwork (local-first: the worker fetches it, the app serves the file)
# ---------------------------------------------------------------------------

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"jpeg-body" * 4
# Two covers, deliberately out of order: the larger one must win.
COVERS = [(b"\xaa" * 16, 1), (b"\xdd" * 16, 3)]
LARGEST_COVER_URL = f"https://i.scdn.co/image/{COVERS[1][0].hex()}"


class _FakeImageResponse:
    """Mimics the context-managed response `urlopen` returns."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, size: int) -> bytes:
        return self._data[:size]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exception: object) -> bool:
        return False


def stub_cdn(
    monkeypatch: pytest.MonkeyPatch,
    *,
    data: bytes | None = None,
    error: Exception | None = None,
) -> list[str]:
    """Serve the image CDN from memory and record the URLs requested."""
    requested: list[str] = []

    def fake_open(request: Any) -> Any:
        requested.append(request.full_url)
        if error is not None:
            raise error
        return _FakeImageResponse(JPEG_BYTES if data is None else data)

    monkeypatch.setattr(spotify_fetch, "_open_image", fake_open)
    return requested


def run_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, artwork: Path | None = None
) -> tuple[int, list[str], Path]:
    """Run the child once with a metadata-bearing fake library."""
    install_fake_librespot(
        monkeypatch,
        track_metadata={
            "name": "Tease Me",
            "artists": ["Zaylevelten"],
            "album": "Tease Me",
            "duration": 240_000,
            "covers": COVERS,
        },
    )
    credentials = write_credentials(tmp_path)
    out = tmp_path / f"{TRACK_ID}.ogg"
    argv = ["--track", TRACK_ID, "--out", str(out), "--credentials", str(credentials)]
    if artwork is not None:
        argv += ["--artwork", str(artwork)]
    return spotify_fetch.main(argv), argv, out


def test_artwork_is_saved_locally_from_the_largest_cover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The cover ends up on disk, so the browser never leaves this machine."""
    requested = stub_cdn(monkeypatch)
    artwork = tmp_path / "results" / "artwork.jpg"

    code, _, out = run_fetch(tmp_path, monkeypatch, artwork=artwork)

    assert code == EXIT_OK
    assert artwork.read_bytes() == JPEG_BYTES
    assert requested == [LARGEST_COVER_URL]
    assert not (tmp_path / "results" / "artwork.jpg.part").exists()
    lines = capsys.readouterr().out.splitlines()
    payload = json.loads(next(line for line in lines if line.startswith("metadata: "))[10:])
    assert payload["album"] == "Tease Me"
    assert payload["artwork"] is True
    assert out.read_bytes() == b"".join(CHUNKS), "the audio is unaffected"


def test_no_artwork_flag_means_no_cdn_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requested = stub_cdn(monkeypatch)

    code, _, _ = run_fetch(tmp_path, monkeypatch)

    assert code == EXIT_OK
    assert requested == []
    lines = capsys.readouterr().out.splitlines()
    payload = json.loads(next(line for line in lines if line.startswith("metadata: "))[10:])
    assert "artwork" not in payload, "nothing was asked for, nothing is claimed"


@pytest.mark.parametrize("data", [b"<html>nope</html>", b"", b"PNG\x89"], ids=["html", "empty", "png"])
def test_a_response_that_is_not_a_jpeg_is_never_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    data: bytes,
) -> None:
    """The file is served as image/jpeg, so a non-JPEG must not land at that path."""
    stub_cdn(monkeypatch, data=data)
    artwork = tmp_path / "results" / "artwork.jpg"

    code, _, _ = run_fetch(tmp_path, monkeypatch, artwork=artwork)

    assert code == EXIT_OK
    assert not artwork.exists()
    lines = capsys.readouterr().out.splitlines()
    payload = json.loads(next(line for line in lines if line.startswith("metadata: "))[10:])
    assert payload["artwork"] is False


def test_an_oversized_artwork_response_is_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cover is a few hundred KB; anything past the cap is not cover art."""
    stub_cdn(monkeypatch, data=b"\xff\xd8\xff" + b"x" * (spotify_fetch.MAX_ARTWORK_BYTES + 1))
    artwork = tmp_path / "results" / "artwork.jpg"

    code, _, _ = run_fetch(tmp_path, monkeypatch, artwork=artwork)

    assert code == EXIT_OK
    assert not artwork.exists()


def test_a_cdn_failure_never_disturbs_the_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cover art is decoration: losing it costs a thumbnail, never the audio."""
    stub_cdn(monkeypatch, error=OSError("cdn unreachable"))
    artwork = tmp_path / "results" / "artwork.jpg"

    code, _, out = run_fetch(tmp_path, monkeypatch, artwork=artwork)

    assert code == EXIT_OK
    assert out.read_bytes() == b"".join(CHUNKS)
    assert not artwork.exists()
    lines = capsys.readouterr().out.splitlines()
    payload = json.loads(next(line for line in lines if line.startswith("metadata: "))[10:])
    assert payload["artwork"] is False
    assert payload["title"] == "Tease Me", "the rest of the metadata still arrives"


def test_metadata_without_a_cover_reports_no_artwork(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A track whose metadata carries no images downloads nothing."""
    requested = stub_cdn(monkeypatch)
    artwork = tmp_path / "results" / "artwork.jpg"
    install_fake_librespot(
        monkeypatch,
        track_metadata={"name": "Tease Me", "artists": [], "duration": 1000, "covers": []},
    )
    credentials = write_credentials(tmp_path)
    out = tmp_path / f"{TRACK_ID}.ogg"

    code = spotify_fetch.main(
        [
            "--track", TRACK_ID, "--out", str(out), "--credentials", str(credentials),
            "--artwork", str(artwork),
        ]
    )

    assert code == EXIT_OK
    assert requested == []
    assert not artwork.exists()
    lines = capsys.readouterr().out.splitlines()
    payload = json.loads(next(line for line in lines if line.startswith("metadata: "))[10:])
    assert payload["artwork"] is False


def test_the_session_never_writes_credentials_and_stays_out_of_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job must never drop a secrets file: the client library's default
    destination is `./credentials.json` under the process cwd."""
    seen = install_fake_librespot(monkeypatch)
    credentials = write_credentials(tmp_path)
    out = tmp_path / f"{TRACK_ID}.ogg"

    code = spotify_fetch.main(
        ["--track", TRACK_ID, "--out", str(out), "--credentials", str(credentials)]
    )

    assert code == EXIT_OK
    configuration = seen["configuration"]
    assert configuration.store_credentials is False
    assert configuration.stored_credentials_file == str(credentials)
    assert configuration.cache_dir == str(cache_dir_path(credentials))
    # Sharing one session cache between concurrent fetches is only safe while
    # the library cannot delete from it on one fetch's behalf.
    assert configuration.cache_enabled is True
    assert configuration.do_cache_clean_up is False
    # The shared directory exists before the session is built, so a real client
    # never has to create it (and two fetches cannot race to create it either).
    assert seen["cache_dir_at_create"] is True


def test_concurrent_fetches_converge_on_one_session_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two jobs fetching at once share exactly one session cache directory.

    The point of sharing is that the second fetch finds the cache the first one
    is filling rather than a cold directory of its own — and that the *same*
    directory is used whether or not it already exists, because that is what a
    simultaneous start looks like from the loser's side.
    """
    install_fake_librespot(monkeypatch)
    credentials = write_credentials(tmp_path)
    shared = cache_dir_path(credentials)
    assert not shared.exists()

    # The first fetch has to create the directory itself...
    spotify_fetch.create_session(credentials)
    assert shared.is_dir()
    # ...and something another fetch (or the library) put there afterwards.
    entry = shared / "existing-chunk"
    entry.write_bytes(b"cached-audio")

    # The second fetch starts on the very same directory it already finds.
    spotify_fetch.create_session(credentials)

    assert shared.is_dir(), "a second fetch must not fail on an existing cache"
    assert entry.read_bytes() == b"cached-audio", "no fetch clears its peers' cache"
    assert list(shared.iterdir()) == [entry], "nothing else is written to it"


def test_a_cache_directory_that_cannot_be_created_never_fails_a_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache is an optimisation, so an unwritable location is not fatal.

    The session is still built with the shared path: the client library decides
    for itself whether that matters, and failing here would turn a perfectly
    fetchable track into a download failure.
    """
    seen = install_fake_librespot(monkeypatch)
    credentials = write_credentials(tmp_path)

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError("read-only cache root")

    monkeypatch.setattr(Path, "mkdir", refuse)

    code = spotify_fetch.main(
        ["--track", TRACK_ID, "--out", str(tmp_path / "out.ogg"), "--credentials", str(credentials)]
    )

    assert code == EXIT_OK
    assert seen["configuration"].cache_dir == str(cache_dir_path(credentials))


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
