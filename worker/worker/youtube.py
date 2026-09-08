"""Optional YouTube input (plan Task 14 / Section 9.6).

Downloads audio for `source_type='youtube'` jobs with controlled `yt-dlp`
arguments only: an allowlisted-hostname HTTPS URL, a fixed argument array (no
shell, no user-supplied downloader options), and the same size/duration limits
as uploads enforced by `input_audio.validate_source` as the final authority.

The web server validates the URL at job creation; this module re-validates it
because the worker is the final policy authority (plan Section 8).

yt-dlp is optional: the module prefers the `yt-dlp` binary on PATH and falls
back to `python -m yt_dlp`. Missing/unavailable yt-dlp fails the job with
DOWNLOAD_FAILED — the upload flow never depends on YouTube (plan Task 14).

This feature must not bypass DRM, private access controls, age gates, or other
restrictions; yt-dlp failures for such sources surface as DOWNLOAD_FAILED.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from worker.errors import ErrorCode
from worker.input_audio import InputAudioError

# Same allowlist as apps/web/lib/jobs.ts (isAllowedYouTubeUrl).
ALLOWED_HOSTS = frozenset({"youtube.com", "www.youtube.com", "youtu.be"})
MAX_URL_LENGTH = 2048
DEFAULT_TIMEOUT_SECONDS = 300

# yt-dlp writes these while a download is in flight; never treat them as media.
_INCOMPLETE_SUFFIXES = (".part", ".temp", ".ytdl")


class DownloadError(InputAudioError):
    """Download-stage failure carrying a stable public error code."""


def youtube_enabled() -> bool:
    """Local kill switch: STEMIFY_YOUTUBE_ENABLED=0 disables YouTube import."""
    return os.environ.get("STEMIFY_YOUTUBE_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }


def is_allowed_youtube_url(value: str) -> bool:
    """Worker-side re-validation of the web app's URL allowlist (Section 9.6).

    Stricter than the web check: userinfo (`user@host`) is rejected outright
    because `urlparse(...).hostname` takes the text *after* the last `@`, which
    can mask the real host in a spoofed netloc.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_URL_LENGTH:
        return False
    if "@" in value:  # never allow userinfo in the URL
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() in ALLOWED_HOSTS
        and bool(parsed.path)
        and parsed.path != "/"
    )


def _timeout_seconds() -> int:
    raw = os.environ.get("YT_DLP_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    try:
        return max(10, int(raw))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def yt_dlp_command() -> list[str]:
    """Locate yt-dlp: PATH binary first, then `python -m yt_dlp`."""
    binary = shutil.which("yt-dlp")
    if binary:
        return [binary]
    try:
        import yt_dlp  # noqa: F401
    except ImportError as error:
        raise DownloadError(
            ErrorCode.DOWNLOAD_FAILED,
            "yt-dlp is not installed on this machine",
        ) from error
    return [sys.executable, "-m", "yt_dlp"]


def download_audio(url: str, dest_dir: Path) -> Path:
    """Download one YouTube source as audio into dest_dir and validate it.

    Fixed arguments only (plan Section 13.6: no shell, no user options).
    Raises DownloadError with DOWNLOAD_FAILED or LIMIT_EXCEEDED.
    """
    if not youtube_enabled():
        raise DownloadError(ErrorCode.DOWNLOAD_FAILED, "YouTube import is disabled")
    if not is_allowed_youtube_url(url):
        raise DownloadError(ErrorCode.DOWNLOAD_FAILED, f"disallowed YouTube URL: {url[:100]}")

    command = yt_dlp_command()

    from worker.input_audio import MAX_FILE_BYTES

    args = [
        *command,
        "--no-playlist",  # one video, never a whole playlist
        "--no-warnings",
        "--no-progress",
        "--no-part",  # no partial-download leftovers in the job directory
        "--restrict-filenames",
        "--extract-audio",
        "--audio-format",
        "mp3",  # lands in the upload allowlist so validate_source accepts it
        "--max-filesize",
        str(MAX_FILE_BYTES),  # advisory; validate_source enforces the hard cap
        "--paths",
        str(dest_dir),
        "--output",
        "%(id)s.%(ext)s",
        "--",
        url,  # allowlist guarantees an https:// URL; `--` blocks option injection
    ]
    try:
        proc = subprocess.run(args, capture_output=True, timeout=_timeout_seconds(), check=False)
    except subprocess.TimeoutExpired as error:
        raise DownloadError(ErrorCode.DOWNLOAD_FAILED, "yt-dlp timed out") from error
    except OSError as error:
        raise DownloadError(ErrorCode.DOWNLOAD_FAILED, "yt-dlp could not be executed") from error

    if proc.returncode != 0:
        raise DownloadError(
            ErrorCode.DOWNLOAD_FAILED,
            f"yt-dlp failed rc={proc.returncode}: "
            f"{proc.stderr.decode(errors='replace')[:200]}",
        )

    candidates = sorted(
        path
        for path in dest_dir.iterdir()
        if path.is_file() and path.suffix.lower() not in _INCOMPLETE_SUFFIXES
    )
    if len(candidates) != 1:
        raise DownloadError(
            ErrorCode.DOWNLOAD_FAILED,
            f"expected exactly one media file, found {len(candidates)}",
        )
    media = candidates[0]

    # Final authority: the same validation ladder as uploads (extension, size,
    # ffprobe media checks, duration limit).
    from worker.input_audio import validate_source

    validate_source(media)
    return media
