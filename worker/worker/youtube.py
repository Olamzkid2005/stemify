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

Every invocation runs in its own process group under a hard wall-clock timeout;
on expiry the whole tree is killed (see `_run_yt_dlp`). yt-dlp spawns ffmpeg
when it post-processes to MP3, and killing only the parent used to leave that
child holding the inherited pipes, which blocked the single-threaded worker
indefinitely. Download percent is streamed to a callback so the job page shows
real progress instead of one long pause.

This feature must not bypass DRM, private access controls, age gates, or other
restrictions; yt-dlp failures for such sources surface as DOWNLOAD_FAILED.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlparse

from worker.errors import ErrorCode
from worker.input_audio import InputAudioError

# Same allowlist as apps/web/lib/jobs.ts (isAllowedYouTubeUrl).
ALLOWED_HOSTS = frozenset({"youtube.com", "www.youtube.com", "youtu.be"})
MAX_URL_LENGTH = 2048
# Hard wall-clock caps. yt-dlp's own retries can outlast a single request, so
# the worker enforces the ceiling itself and kills the whole process tree.
DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_PROBE_TIMEOUT_SECONDS = 60
# yt-dlp progress lines: "[download]  42.0% of 3.21MiB at ..."
_DOWNLOAD_PROGRESS = re.compile(r"\[download\]\s+(\d{1,3}(?:\.\d+)?)%")

# yt-dlp writes these while a download is in flight; never treat them as media.
_INCOMPLETE_SUFFIXES = (".part", ".temp", ".ytdl")

# Download/display naming (roadmap A2): jobs are named after the song, so the
# worker resolves the video title and stores it as the job's source filename.
_FILENAME_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MAX_TITLE_CHARS = 80


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


def _timeout_seconds(env_name: str, default: int) -> int:
    """Read an operator override, bounded below so a typo cannot disable it."""
    raw = os.environ.get(env_name, str(default))
    try:
        return max(10, int(raw))
    except ValueError:
        return default


class _YtDlpResult(NamedTuple):
    """Outcome of one yt-dlp invocation."""

    returncode: int
    stdout: str
    stderr: str


def _spawn_yt_dlp(args: list[str]) -> subprocess.Popen[str]:
    """Start yt-dlp in its own process group so a timeout can kill its tree.

    yt-dlp spawns ffmpeg for `--extract-audio`; killing only the parent leaves
    that grandchild alive holding the inherited stdout/stderr pipes, and the
    reader then blocks forever instead of reporting the timeout — the worker
    looks hung while its heartbeat keeps reporting it as alive. A new process
    group/session is what makes a tree-wide kill possible (`taskkill /T` on
    Windows, `killpg` on POSIX).
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
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    # No literal timeout here: `_run_yt_dlp` owns the deadline (wait(timeout=...)
    # plus a tree-wide kill), which is what makes the limit enforceable at all.
    return subprocess.Popen(args, **kwargs)  # timeout: enforced by _run_yt_dlp


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    """Kill the process and every descendant it spawned (ffmpeg included)."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
                timeout=15,
            )
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        with contextlib.suppress(OSError):
            process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        process.wait(timeout=10)


def _run_yt_dlp(
    args: list[str],
    *,
    timeout_seconds: int,
    on_progress: Callable[[float], None] | None = None,
) -> _YtDlpResult:
    """Run one yt-dlp command; kill the whole tree and fail on timeout.

    stdout is drained line by line (download percent goes to on_progress) and
    stderr is buffered — both on background threads, because ffmpeg logs to
    stderr and an undrained pipe would fill up and block the child. A failure
    inside on_progress is swallowed for the same reason: losing a progress
    update is fine, stalling the drain is not.
    """
    try:
        process = _spawn_yt_dlp(args)
    except OSError as error:
        raise DownloadError(
            ErrorCode.DOWNLOAD_FAILED, f"yt-dlp could not be executed: {error}"
        ) from error

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
        if on_progress is not None:
            match = _DOWNLOAD_PROGRESS.search(line)
            if match:
                on_progress(float(match.group(1)))

    readers = [
        threading.Thread(target=drain, args=(process.stdout, read_stdout), daemon=True),
        threading.Thread(
            target=drain, args=(process.stderr, stderr_lines.append), daemon=True
        ),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(process)
        returncode = process.returncode if process.returncode is not None else -1
    finally:
        for reader in readers:
            reader.join(timeout=10)
        for stream in (process.stdout, process.stderr):
            with contextlib.suppress(OSError, ValueError):
                if stream is not None:
                    stream.close()

    if timed_out:
        raise DownloadError(
            ErrorCode.DOWNLOAD_FAILED,
            f"yt-dlp exceeded the {timeout_seconds}s timeout and was terminated",
        )
    return _YtDlpResult(returncode, "".join(stdout_lines), "".join(stderr_lines))


def sanitize_title(raw: str) -> str:
    """Clean a video title for use as a display/download name.

    Mirrors the web's filename sanitizer: filesystem-illegal characters become
    spaces, whitespace collapses, and the result is capped. Non-ASCII is kept
    (downloads travel with an RFC 5987 UTF-8 filename variant).
    """
    cleaned = _FILENAME_ILLEGAL.sub(" ", raw)
    cleaned = " ".join(cleaned.split()).strip()
    return cleaned[:_MAX_TITLE_CHARS].strip().rstrip(".")


def resolve_title(url: str) -> str | None:
    """Best-effort video title lookup for download naming (roadmap A2).

    Runs one fixed-argument `yt-dlp --dump-single-json` probe against the same
    allowlisted HTTPS URL the download uses (same policy surface: no shell, no
    user options, `--` before the URL). Returns the sanitized title, or None
    when yt-dlp is unavailable/fails or the title is unusable — naming then
    falls back to the media filename without failing the job.
    """
    if not is_allowed_youtube_url(url) or not youtube_enabled():
        return None
    try:
        command = yt_dlp_command()
    except DownloadError:
        return None
    args = [
        *command,
        "--no-playlist",
        "--no-warnings",
        "--skip-download",
        "--dump-single-json",
        "--",
        url,
    ]
    try:
        result = _run_yt_dlp(
            args,
            timeout_seconds=_timeout_seconds(
                "YT_DLP_PROBE_TIMEOUT_SECONDS", DEFAULT_PROBE_TIMEOUT_SECONDS
            ),
        )
    except DownloadError:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    title = info.get("title") if isinstance(info, dict) else None
    if not isinstance(title, str):
        return None
    return sanitize_title(title) or None


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


def download_audio(
    url: str,
    dest_dir: Path,
    progress_callback: Callable[[float], None] | None = None,
) -> Path:
    """Download one YouTube source as audio into dest_dir and validate it.

    Fixed arguments only (plan Section 13.6: no shell, no user options).
    progress_callback receives yt-dlp's download percentage (0-100) so the job
    page can show real progress instead of one long "downloading" pause.
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
        "--newline",  # one progress line per update, so it can be streamed
        "--socket-timeout",
        "30",  # a stalled connection must not hold the job open
        "--retries",
        "3",
        "--fragment-retries",
        "3",
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
    result = _run_yt_dlp(
        args,
        timeout_seconds=_timeout_seconds("YT_DLP_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
        on_progress=progress_callback,
    )
    if result.returncode != 0:
        raise DownloadError(
            ErrorCode.DOWNLOAD_FAILED,
            f"yt-dlp failed rc={result.returncode}: {result.stderr[:200]}",
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
