"""Fetch one Spotify track's audio stream as Ogg Vorbis (plan milestone S2).

Run as a separate process (`python -m worker.spotify_fetch --track <id> --out
<path>`) so the worker can bound it with a hard deadline and kill its whole
tree, exactly like the yt-dlp backend: the open-source client library is alpha,
holds protocol state, and a stalled stream must never block the
single-threaded worker. The parent owns policy (allowlist, kill switch,
credentials, size/duration limits); this child owns one fetch.

Exit codes (the parent maps them to public failures and keeps the detail local):

| Code | Meaning |
|---|---|
| 0 | track written to `--out` |
| 2 | usage problem: bad arguments, bad track id, missing credentials file |
| 3 | the `librespot` client library is not installed |
| 4 | the client could not authenticate the session |
| 5 | the stream failed part-way through |
| 6 | the output could not be written |

Progress is reported as `progress: <bytes>` lines on stdout. The exact stream
length is only known when it ends, so the parent converts bytes to a percentage
with a bitrate estimate. Output is written to `<out>.part` and renamed on
success, so a failed fetch never leaves a file the validation ladder could pick
up as real media.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from worker.spotify import TRACK_ID

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DEPENDENCY = 3
EXIT_AUTH = 4
EXIT_STREAM = 5
EXIT_WRITE = 6

CREDENTIALS_ENV = "STEMIFY_SPOTIFY_CREDENTIALS_FILE"
PART_SUFFIX = ".part"
CHUNK_BYTES = 65536
PROGRESS_PREFIX = "progress: "
PROGRESS_PATTERN = re.compile(r"^progress:\s*(\d+)\s*$")


class UsageError(Exception):
    """Bad invocation: the parent always passes fixed, valid arguments."""


def _parse_args(argv: list[str]) -> tuple[str, Path, Path]:
    """Strict argument parsing: two known flags, each exactly once."""
    track_id: str | None = None
    out_path: str | None = None
    credentials: str | None = None
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag in {"--track", "--out", "--credentials"} and index + 1 < len(argv):
            value = argv[index + 1]
            if flag == "--track":
                track_id = value
            elif flag == "--out":
                out_path = value
            else:
                credentials = value
            index += 2
            continue
        raise UsageError(f"unsupported argument: {flag[:40]}")

    if not track_id or not TRACK_ID.match(track_id):
        raise UsageError("--track requires a 22-character Spotify track id")
    if not out_path:
        raise UsageError("--out requires a destination file path")

    credentials_path = credentials or os.environ.get(CREDENTIALS_ENV, "")
    if not credentials_path:
        raise UsageError(f"no credentials file (set {CREDENTIALS_ENV} or pass --credentials)")
    if not Path(credentials_path).is_file():
        raise UsageError("the Spotify credentials file does not exist")
    return track_id, Path(out_path), Path(credentials_path)


def create_session(credentials_file: Path) -> Any:
    """Build a client session from the operator's stored credentials.

    Cached credentials produced by the librespot CLI work as-is (the library
    reads both the Python and the Rust credential formats), which is what keeps
    the one-time interactive login a separate, manual step.
    """
    from librespot.core import Session

    return Session.Builder().stored_file(str(credentials_file)).create()


def open_stream(track_id: str, session: Any) -> Any:
    """The library-coupled call: request the highest-quality Ogg Vorbis stream."""
    from librespot.audio.decoders import (
        AudioQuality,
        VorbisOnlyAudioQuality,
    )
    from librespot.metadata import TrackId

    track = TrackId.from_uri(f"spotify:track:{track_id}")
    quality = VorbisOnlyAudioQuality(AudioQuality.VERY_HIGH)
    stream = session.content_feeder().load(track, quality, False, None)
    return stream.input_stream.stream()


def stream_track(
    track_id: str,
    session: Any,
    out_path: Path,
    report: Callable[[int], None],
) -> None:
    """Stream one track to out_path, reporting cumulative bytes written."""
    source = open_stream(track_id, session)

    part_path = out_path.with_name(out_path.name + PART_SUFFIX)
    written = 0
    try:
        with part_path.open("wb") as sink:
            while True:
                chunk = source.read(CHUNK_BYTES)
                # The client library signals the end of the voice data with -1;
                # bytes or None also mean "no more data".
                if isinstance(chunk, int) or not chunk:
                    break
                sink.write(chunk)
                written += len(chunk)
                report(written)
    except BaseException:
        part_path.unlink(missing_ok=True)
        raise
    os.replace(part_path, out_path)


def main(argv: list[str] | None = None) -> int:
    """Fetch one track; every failure is a distinct exit code, never a traceback."""
    arguments = sys.argv[1:] if argv is None else argv
    try:
        track_id, out_path, credentials_file = _parse_args(arguments)
    except UsageError as error:
        print(f"spotify-fetch: {error}", file=sys.stderr)
        return EXIT_USAGE

    def report(bytes_written: int) -> None:
        print(f"{PROGRESS_PREFIX}{bytes_written}", flush=True)

    try:
        session = create_session(credentials_file)
    except ImportError as error:
        print(f"spotify-fetch: the librespot library is not installed ({error})", file=sys.stderr)
        return EXIT_DEPENDENCY
    except Exception as error:  # noqa: BLE001 - a failed login must become an exit code
        print(f"spotify-fetch: sign-in failed: {type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_AUTH

    try:
        stream_track(track_id, session, out_path, report)
    except ImportError as error:
        print(f"spotify-fetch: the librespot library is incomplete ({error})", file=sys.stderr)
        return EXIT_DEPENDENCY
    except OSError as error:
        print(f"spotify-fetch: could not write the output ({error})", file=sys.stderr)
        return EXIT_WRITE
    except Exception as error:  # noqa: BLE001 - a stream failure must become an exit code
        # The class name and message travel to job_events, never to the browser.
        print(f"spotify-fetch: stream failed: {type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_STREAM
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
