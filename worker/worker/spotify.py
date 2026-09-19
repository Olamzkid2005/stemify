"""Optional Spotify input (Spotify plan, milestone S1).

Metadata only: this module resolves *what* a Spotify link points at. It never
fetches audio — that is milestone S2 (the librespot backend), and until it
exists the worker has no Spotify download path at all.

The shape mirrors `worker/youtube.py`: an allowlisted link, worker-side
re-validation (the worker is the final policy authority, plan Section 8), a
kill switch, fixed outgoing requests and no shell. Two things differ:

- Spotify input needs operator configuration (a Web API app for metadata, a
  Premium account for audio in S2), so the kill switch defaults **off** here
  while YouTube defaults on.
- Only the public, documented Web API is used, and only for metadata. The
  client-credentials flow returns an app token that can read catalogue
  metadata and cannot stream audio, so nothing here authenticates as a user
  and no credentials ever reach the web app or the database (plan Section 4).

v1 is single-track by design: album and playlist links (and `spotify.link`
short links, which need a network redirect to resolve) are rejected here and
documented as the batch fan-out follow-up (plan Section 9).
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple
from urllib.parse import urlparse

from worker.errors import ErrorCode
from worker.input_audio import InputAudioError
from worker.youtube import sanitize_title  # shared download-naming sanitizer (Task 14)

# Same allowlist as the web app's check (apps/web/lib/jobs.ts). `spotify.link`
# short links are deliberately absent: resolving one requires a network
# redirect, so accepting it here would mean accepting a link whose target is
# unverifiable at job-creation time.
ALLOWED_HOSTS = frozenset({"open.spotify.com"})
MAX_URL_LENGTH = 2048

# Spotify base-62 object ids: exactly 22 characters, case-sensitive.
TRACK_ID = re.compile(r"^[A-Za-z0-9]{22}$")
# /track/<id>, optionally behind a locale prefix Spotify itself adds (/intl-de/).
_TRACK_PATH = re.compile(r"^/(?:intl-[a-z]{2}(?:-[A-Za-z]{2})?/)?track/([A-Za-z0-9]{22})$")
_TRACK_URI = re.compile(r"^spotify:track:([A-Za-z0-9]{22})$")

# Metadata is a convenience, never a dependency: the download is named from the
# track id when this fails, exactly like a missing yt-dlp title.
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"
DEFAULT_HTTP_TIMEOUT_SECONDS = 15


class SpotifyError(InputAudioError):
    """Spotify-stage failure carrying a stable public error code (S2 onwards)."""


def spotify_enabled() -> bool:
    """Local kill switch: Spotify input stays off until the operator opts in.

    Default **off** (unlike YouTube): metadata needs a Web API app and audio
    needs a Premium account, so an unconfigured machine must not accept
    Spotify jobs at all.
    """
    return os.environ.get("STEMIFY_SPOTIFY_ENABLED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def parse_track_id(value: str) -> str | None:
    """Extract the track id from an allowlisted link or `spotify:track:` URI.

    Also serves as the worker-side allowlist: the web app validates the URL at
    job creation, and this re-validation is the final say. Userinfo
    (`user@host`) is rejected outright, because `urlparse(...).hostname` takes
    the text *after* the last `@` and can therefore mask a spoofed host.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_URL_LENGTH:
        return None

    uri = _TRACK_URI.match(value)
    if uri:
        return uri.group(1)

    if "@" in value:  # never allow userinfo in the URL
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        return None
    path_match = _TRACK_PATH.match(parsed.path)
    return path_match.group(1) if path_match else None


def is_allowed_spotify_url(value: str) -> bool:
    """Worker-side re-validation of the web app's Spotify allowlist."""
    return parse_track_id(value) is not None


def spotify_credentials() -> tuple[str, str] | None:
    """Operator-supplied Web API app credentials, or None when unconfigured.

    Env only, by design: the web app and the database never see these, and
    nothing here reads per-job input.
    """
    client_id = os.environ.get("STEMIFY_SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("STEMIFY_SPOTIFY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None
    return client_id, client_secret


def _http_timeout_seconds() -> int:
    """Operator override for metadata requests, bounded below."""
    raw = os.environ.get("STEMIFY_SPOTIFY_HTTP_TIMEOUT_SECONDS", str(DEFAULT_HTTP_TIMEOUT_SECONDS))
    try:
        return max(5, int(raw))
    except ValueError:
        return DEFAULT_HTTP_TIMEOUT_SECONDS


def _request_json(
    url: str,
    *,
    data: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, object] | None:
    """One JSON request; returns None on any failure (naming is best-effort).

    This is the module's single network seam, so tests never touch the network:
    fixed URLs from constants, fixed form bodies, no shell, and a hard timeout.
    """
    body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        method="POST" if body is not None else "GET",
        headers=headers or {},
    )
    try:
        with urllib.request.urlopen(request, timeout=_http_timeout_seconds()) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


class TrackMetadata(NamedTuple):
    """The catalogue fields the pipeline needs for naming and the manifest."""

    track_id: str
    title: str
    artist: str
    album: str
    duration_ms: int


def _access_token(client_id: str, client_secret: str) -> str | None:
    """Client-credentials token. Reads catalogue metadata only, no user login."""
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    payload = _request_json(
        TOKEN_URL,
        data={"grant_type": "client_credentials"},
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    token = payload.get("access_token") if payload else None
    return token if isinstance(token, str) and token else None


def fetch_track_metadata(track_id: str) -> TrackMetadata | None:
    """Look up one track's catalogue metadata.

    Returns None when the feature is disabled, the id is not a valid track id,
    the operator has not configured credentials, or the request fails — the
    caller falls back to naming the job from the track id.
    """
    if not TRACK_ID.match(track_id) or not spotify_enabled():
        return None
    credentials = spotify_credentials()
    if credentials is None:
        return None
    token = _access_token(*credentials)
    if token is None:
        return None
    payload = _request_json(
        f"{API_BASE}/tracks/{track_id}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    if payload is None:
        return None

    title = payload.get("name")
    if not isinstance(title, str) or not title.strip():
        return None
    artists = payload.get("artists")
    names: list[str] = []
    if isinstance(artists, list):
        for entry in artists:
            name = entry.get("name") if isinstance(entry, dict) else None
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    album = payload.get("album")
    album_name = album.get("name") if isinstance(album, dict) else None
    duration = payload.get("duration_ms")
    return TrackMetadata(
        track_id=track_id,
        title=title.strip(),
        artist=", ".join(names).strip(),
        album=album_name.strip() if isinstance(album_name, str) else "",
        duration_ms=duration if isinstance(duration, int) and duration >= 0 else 0,
    )


def resolve_title(url: str) -> str | None:
    """Best-effort `Artist - Title` for job/download naming (roadmap A2).

    Mirrors `youtube.resolve_title`: returns None whenever the feature is
    disabled, the link is not an allowlisted track, credentials are missing, or
    the lookup fails, so naming falls back to the stored source filename
    without failing the job.
    """
    if not spotify_enabled():
        return None
    track_id = parse_track_id(url)
    if track_id is None:
        return None
    metadata = fetch_track_metadata(track_id)
    if metadata is None:
        return None
    name = f"{metadata.artist} - {metadata.title}" if metadata.artist else metadata.title
    return sanitize_title(name) or None


def unavailable_error() -> SpotifyError:
    """The failure to raise when a Spotify job reaches a machine that cannot serve it."""
    if not spotify_enabled():
        return SpotifyError(
            ErrorCode.DOWNLOAD_FAILED, "Spotify input is disabled on this machine"
        )
    return SpotifyError(ErrorCode.DOWNLOAD_FAILED, "Spotify input is not configured on this machine")
