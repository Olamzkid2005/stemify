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

The track's own catalogue metadata is printed once as `metadata: <json>` before
the stream starts. It comes from the session this process already has, so the
parent can name the job and size the progress estimate on a machine that never
configured a Web API application. It is strictly optional: nothing about the
fetch depends on it succeeding.

When `--artwork` is given, the album's cover art is saved there from Spotify's
public image CDN as a verified JPEG. Nothing leaves this machine for it — the
job page serves the local file — and, like the metadata, a failure is silent.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from worker.spotify import TRACK_ID, cache_dir_path, ensure_cache_dir

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
METADATA_PREFIX = "metadata: "

# Album art lives on Spotify's public image CDN, keyed by the image id in the
# track's own metadata: no session, no credentials, no Web API application. The
# bytes are checked for the JPEG signature before anything is written, because
# the caller serves the file as image/jpeg and the parent does not rename it.
IMAGE_CDN = "https://i.scdn.co/image/{image_id}"
JPEG_MAGIC = b"\xff\xd8\xff"
ARTWORK_TIMEOUT_SECONDS = 10
# A cover is a few hundred KB at most; anything bigger is not cover art.
MAX_ARTWORK_BYTES = 2_000_000


class UsageError(Exception):
    """Bad invocation: the parent always passes fixed, valid arguments."""


def _parse_args(argv: list[str]) -> tuple[str, Path, Path, Path | None]:
    """Strict argument parsing: known flags, each exactly once."""
    track_id: str | None = None
    out_path: str | None = None
    credentials: str | None = None
    artwork_path: str | None = None
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag in {"--track", "--out", "--credentials", "--artwork"} and index + 1 < len(argv):
            value = argv[index + 1]
            if flag == "--track":
                track_id = value
            elif flag == "--out":
                out_path = value
            elif flag == "--artwork":
                artwork_path = value
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
    return (
        track_id,
        Path(out_path),
        Path(credentials_path),
        Path(artwork_path) if artwork_path else None,
    )


def create_session(credentials_file: Path) -> Any:
    """Build a client session from the operator's stored credentials.

    Cached credentials produced by the librespot CLI work as-is (the library
    reads both the Python and the Rust credential formats), which is what keeps
    the one-time interactive login a separate, manual step.

    Credential writing is switched **off**: a job already has credentials, and
    the library's default destination is `./credentials.json` under the process
    cwd, so leaving it on would drop a secrets file next to the source tree on
    every fetch. Every process only *reads* that one shared file, which is what
    makes concurrent fetches safe there — the only writer is the one-time login,
    and it publishes its result atomically (`worker.spotify.login`).

    The session cache is shared by every fetch on the same credentials, on
    purpose: each job is its own short-lived process, and one cache per job
    would mean a cold cache every time and a directory to clean up per fetch.
    Sharing it safely rests on four things:

    - **One path**, computed by `worker.spotify.cache_dir_path` from the
      credentials file, so the login and all fetches cannot disagree.
    - **Created with `exist_ok`** before the session is built, so two jobs
      starting together converge on one directory instead of racing.
    - **Clean-up is off.** `do_cache_clean_up` makes the library delete entries
      older than a fixed threshold, and the deletion has no idea that a
      concurrent fetch is reading one of them; a fetch has no business
      deciding what another one's cache may keep.
    - **We never write or delete in it ourselves.** The library is the only
      writer, and in the client version we pin `CacheManager` is an
      unimplemented stub that reads neither `cache_dir` nor `cache_enabled`:
      a fetch leaves the directory exactly as it found it.

    That last point is the assumption worth watching, so the tests pin our side
    of it (the configuration we hand over, the directory surviving a fetch
    untouched, and a pre-existing entry never being removed). If a future
    library version starts writing, this needs a lock or a per-fetch
    subdirectory — not merely this comment.
    """
    from librespot.core import Session

    # Created up front (idempotently) so concurrent fetches all find the same
    # directory already there instead of racing to create it.
    ensure_cache_dir(credentials_file)
    configuration = (
        Session.Configuration.Builder()
        .set_store_credentials(False)
        .set_stored_credential_file(str(credentials_file))
        .set_cache_enabled(True)
        .set_do_cache_clean_up(False)
        .set_cache_dir(str(cache_dir_path(credentials_file)))
        .build()
    )
    return Session.Builder(configuration).stored_file(str(credentials_file)).create()


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


def _cover_image_id(track: Any) -> bytes | None:
    """The album's largest cover image id, or None when the metadata has none.

    Image ids are the CDN key, not a URL, so this stays true regardless of how
    the image URL scheme changes. `cover_group` is the current field and
    `cover` its older single-image form.
    """
    album = getattr(track, "album", None)
    if album is None:
        return None
    group = getattr(album, "cover_group", None)
    images = getattr(group, "image", None)
    if not images:
        images = getattr(album, "cover", None)
    best: bytes | None = None
    best_rank = -1
    for image in images or []:
        file_id = getattr(image, "file_id", None)
        if not isinstance(file_id, (bytes, bytearray)) or not file_id:
            continue
        # Size is a protobuf enum int that grows with the image (0=default,
        # 3=xlarge); an int is all that is ever compared here.
        size = getattr(image, "size", None)
        rank = size if isinstance(size, int) else 0
        if rank >= best_rank:
            best, best_rank = bytes(file_id), rank
    return best


def _open_image(request: urllib.request.Request) -> Any:
    """The module's single image seam, so tests never touch the CDN."""
    return urllib.request.urlopen(request, timeout=ARTWORK_TIMEOUT_SECONDS)


def _write_artwork(image_id: bytes, out_path: Path) -> bool:
    """Save one CDN image to out_path, but only when it really is a JPEG.

    The caller serves this file as image/jpeg and never renames it, so a
    response that is not the JPEG its URL claims is discarded rather than
    written under a lying extension. Best-effort throughout: every failure is
    False, never an exception, because cover art is decoration.
    """
    try:
        request = urllib.request.Request(
            IMAGE_CDN.format(image_id=image_id.hex()),
            headers={"User-Agent": "stemify"},
        )
        with _open_image(request) as response:
            data = response.read(MAX_ARTWORK_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError):
        return False
    if not data.startswith(JPEG_MAGIC) or len(data) > MAX_ARTWORK_BYTES:
        return False

    part_path = out_path.with_name(out_path.name + PART_SUFFIX)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with part_path.open("wb") as sink:
            sink.write(data)
        os.replace(part_path, out_path)
    except OSError:
        part_path.unlink(missing_ok=True)
        return False
    return True


def track_metadata(
    track_id: str,
    session: Any,
    artwork_path: Path | None = None,
) -> dict[str, Any] | None:
    """The track's own catalogue metadata, or None when it cannot be read.

    Strictly best-effort: naming, the parent's progress estimate and the album
    artwork are all conveniences, so every failure here must leave the fetch
    untouched. Read from the session that is already signed in — librespot
    issues the same request internally while loading the track — so this needs
    no Web API application and no extra operator setup.

    With `artwork_path`, the album's cover is fetched from the public image CDN
    and written there. The payload then carries `artwork`, which is true only
    when a verified JPEG landed on disk.
    """
    try:
        from librespot.metadata import TrackId

        track = session.api().get_metadata_4_track(
            TrackId.from_uri(f"spotify:track:{track_id}")
        )
    except Exception:  # noqa: BLE001 - metadata must never fail a fetch
        return None

    title = getattr(track, "name", None)
    if not isinstance(title, str) or not title.strip():
        return None
    artists = [
        entry.name
        for entry in (getattr(track, "artist", None) or [])
        if isinstance(getattr(entry, "name", None), str) and entry.name.strip()
    ]
    album_name = getattr(getattr(track, "album", None), "name", None)
    duration = getattr(track, "duration", 0)
    payload: dict[str, Any] = {
        "track_id": track_id,
        "title": title.strip(),
        "artist": ", ".join(artists),
        "album": album_name.strip() if isinstance(album_name, str) else "",
        "duration_ms": int(duration) if isinstance(duration, int) and duration > 0 else 0,
    }
    if artwork_path is not None:
        image_id = _cover_image_id(track)
        payload["artwork"] = _write_artwork(image_id, artwork_path) if image_id else False
    return payload


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
        track_id, out_path, credentials_file, artwork_path = _parse_args(arguments)
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

    # Optional, and printed before the stream so the parent can size its
    # progress estimate from the first line it reads.
    metadata = track_metadata(track_id, session, artwork_path)
    if metadata is not None:
        print(f"{METADATA_PREFIX}{json.dumps(metadata, ensure_ascii=False)}", flush=True)

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
