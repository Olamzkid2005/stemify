"""Input audio pipeline (plan Task 8 / Section 13.6).

Retrieves a local source into a per-job temporary directory, validates it with
ffprobe (authoritative — never trust extensions or MIME), enforces limits, and
decodes to the canonical representation: float32, stereo, model sample rate.

Failure paths raise InputAudioError with a stable public code; temporary
directories are removed by the caller's context manager on every path.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from worker.errors import ErrorCode

# Limits mirror the plan (Section 9), .env.example and CLIENT_LIMITS in
# apps/web. start.sh exports the same file to both processes, so a value set
# there is honored *here* — an unenforced cap is indistinguishable from no cap.
# An unset or unusable value falls back to the shipped default, never to
# "unlimited": a typo must not switch a limit off.
DEFAULT_MAX_FILE_BYTES = 100 * 1024 * 1024  # 100 MB
DEFAULT_MAX_DURATION_SECONDS = 480.0  # 8 minutes
# The 3-stem split is ~1.5x the work of the 2-stem split, so it carries a
# lower ceiling (plan Section 9).
DEFAULT_FULL_STEMS_MAX_DURATION_SECONDS = 360.0  # 6 minutes
CANONICAL_SAMPLE_RATE = 44100
CANONICAL_CHANNELS = 2


def _positive_env_number(name: str, default: float) -> float:
    """One numeric cap from the environment; anything unusable keeps `default`.

    Blank, non-numeric, non-finite and non-positive values all fall back, so the
    only way to change a cap is to state a usable number.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    return value


def _limits_from_env() -> tuple[int, float, float]:
    """(max file bytes, max duration seconds, 3-stem max duration seconds)."""
    return (
        int(_positive_env_number("MAX_UPLOAD_BYTES", DEFAULT_MAX_FILE_BYTES)),
        _positive_env_number("MAX_DURATION_SECONDS", DEFAULT_MAX_DURATION_SECONDS),
        _positive_env_number(
            "MAX_FULL_STEMS_DURATION_SECONDS", DEFAULT_FULL_STEMS_MAX_DURATION_SECONDS
        ),
    )


MAX_FILE_BYTES, MAX_DURATION_SECONDS, FULL_STEMS_MAX_DURATION_SECONDS = _limits_from_env()


def max_duration_for_mode(mode: str) -> float:
    """Duration cap for one job's mode (plan Section 9).

    Refine jobs (`drum_breakdown`) work on a drum stem this worker produced, so
    the input is never longer than the upload that already passed this ladder;
    they take the general cap.
    """
    if mode == "full_stems":
        return FULL_STEMS_MAX_DURATION_SECONDS
    return MAX_DURATION_SECONDS

_ALLOWED_EXTENSIONS = {".mp3", ".wav", ".flac", ".ogg", ".m4a"}


class InputAudioError(Exception):
    """Validation/decode failure carrying a stable public error code."""

    def __init__(self, code: ErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code


class JobTempDir:
    """Unique per-job temporary directory; cleanup runs on every path."""

    def __init__(self) -> None:
        self._path: Path | None = None

    def __enter__(self) -> Path:
        self._path = Path(tempfile.mkdtemp(prefix="stemify-job-"))
        return self._path

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._path and self._path.exists():
            shutil.rmtree(self._path, ignore_errors=True)
        self._path = None


def _require_ffmpeg_tool(name: str) -> str:
    # Prefer the vendored static binaries (worker/bin), then PATH.
    vendored = Path(__file__).resolve().parent.parent / "bin" / name
    if vendored.is_file() and vendored.stat().st_mode & 0o111:
        return str(vendored)
    path = shutil.which(name)
    if not path:
        raise InputAudioError(ErrorCode.UNKNOWN, f"{name} is not installed on this machine")
    return path


@dataclass(frozen=True)
class ProbeResult:
    duration_seconds: float
    sample_rate: int
    channels: int
    codec_name: str
    has_audio: bool
    has_video: bool
    size_bytes: int


def run_ffprobe(path: Path) -> ProbeResult:
    """Run ffprobe with a JSON output schema and parse the essentials."""
    ffprobe = _require_ffmpeg_tool("ffprobe")
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
    except subprocess.TimeoutExpired as err:
        raise InputAudioError(ErrorCode.INVALID_AUDIO, "ffprobe timed out") from err

    if proc.returncode != 0:
        raise InputAudioError(
            ErrorCode.INVALID_AUDIO,
            f"ffprobe failed: {proc.stderr.decode(errors='replace')[:200]}",
        )

    try:
        data = json.loads(proc.stdout.decode())
    except json.JSONDecodeError as err:
        raise InputAudioError(ErrorCode.INVALID_AUDIO, "ffprobe output unreadable") from err

    streams = data.get("streams", [])
    fmt = data.get("format", {})
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    video_streams = [s for s in streams if s.get("codec_type") == "video"]

    if not audio_streams:
        raise InputAudioError(
            ErrorCode.INVALID_AUDIO, "no audio stream (video-only, image, or corrupt file)"
        )

    audio = audio_streams[0]
    try:
        duration = float(fmt.get("duration") or audio["duration"])
        sample_rate = int(audio["sample_rate"])
        channels = int(audio["channels"])
    except (KeyError, TypeError, ValueError) as err:
        raise InputAudioError(ErrorCode.INVALID_AUDIO, "missing audio properties") from err

    return ProbeResult(
        duration_seconds=duration,
        sample_rate=sample_rate,
        channels=channels,
        codec_name=str(audio.get("codec_name", "unknown")),
        has_audio=True,
        has_video=bool(video_streams),
        size_bytes=path.stat().st_size,
    )


def validate_source(
    path: Path,
    max_file_bytes: int | None = None,
    max_duration_seconds: float | None = None,
) -> ProbeResult:
    """Full validation ladder: extension, size, ffprobe, duration (plan §32.4).

    Both caps default to the module limits (read from the environment at
    import). Callers pass an explicit duration for a mode-specific ceiling
    (`max_duration_for_mode`) and an explicit size for worker-produced stems,
    which legitimately exceed the upload cap.
    """
    size_cap = MAX_FILE_BYTES if max_file_bytes is None else max_file_bytes
    duration_cap = (
        MAX_DURATION_SECONDS if max_duration_seconds is None else max_duration_seconds
    )
    if path.suffix.lower() not in _ALLOWED_EXTENSIONS:
        raise InputAudioError(ErrorCode.INVALID_AUDIO, f"unsupported extension {path.suffix!r}")

    size = path.stat().st_size
    if size == 0:
        raise InputAudioError(ErrorCode.INVALID_AUDIO, "file is empty")
    if size > size_cap:
        raise InputAudioError(
            ErrorCode.LIMIT_EXCEEDED, f"file is {size} bytes, over the {size_cap} byte limit"
        )

    probe = run_ffprobe(path)

    if probe.duration_seconds <= 0:
        raise InputAudioError(ErrorCode.INVALID_AUDIO, "non-positive duration")
    if probe.duration_seconds > duration_cap:
        raise InputAudioError(
            ErrorCode.LIMIT_EXCEEDED,
            f"duration {probe.duration_seconds:.1f}s exceeds {duration_cap:.0f}s",
        )
    if probe.channels not in (1, 2):
        raise InputAudioError(
            ErrorCode.INVALID_AUDIO, f"unsupported channel count {probe.channels}"
        )
    return probe


def decode_to_canonical_wav(source: Path, dest_wav: Path) -> None:
    """Decode to float32 stereo WAV at the canonical sample rate (§32.5).

    Uses an argument array only — no shell interpolation of any input (§13.6).
    """
    ffmpeg = _require_ffmpeg_tool("ffmpeg")
    dest_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-v",
        "error",
        "-i",
        str(source),
        "-vn",  # drop any cover-art video stream
        "-ar",
        str(CANONICAL_SAMPLE_RATE),
        "-ac",
        str(CANONICAL_CHANNELS),
        "-c:a",
        "pcm_f32le",
        "-f",
        "wav",
        str(dest_wav),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
    except subprocess.TimeoutExpired as err:
        raise InputAudioError(ErrorCode.INVALID_AUDIO, "decode timed out") from err

    if proc.returncode != 0 or not dest_wav.exists() or dest_wav.stat().st_size == 0:
        raise InputAudioError(
            ErrorCode.INVALID_AUDIO,
            f"decode failed: {proc.stderr.decode(errors='replace')[:200]}",
        )


def prepare_source(
    source: Path,
    job_dir: Path,
    max_file_bytes: int | None = None,
    max_duration_seconds: float | None = None,
) -> tuple[Path, ProbeResult]:
    """Validate a source inside job_dir and decode it to canonical input.wav."""
    resolved = source.resolve()
    job_resolved = job_dir.resolve()
    if job_resolved not in resolved.parents:
        raise InputAudioError(ErrorCode.INVALID_AUDIO, "source path escapes the job directory")
    if not resolved.is_file():
        raise InputAudioError(ErrorCode.INVALID_AUDIO, "source file does not exist")

    probe = validate_source(resolved, max_file_bytes, max_duration_seconds)
    dest = job_dir / "input.wav"
    decode_to_canonical_wav(resolved, dest)
    return dest, probe


def decode_to_waveform(canonical_wav: Path) -> tuple[Any, ProbeResult]:
    """Load a canonical WAV into a float32 (channels, samples) numpy array.

    Re-probes so callers get the decoded properties (rate/channels/duration)
    without trusting the original source probe. Shape is (channels, samples),
    the layout demucs expects. Requires numpy (worker/requirements.txt).
    """
    try:
        import numpy as np
        import torchaudio
    except ImportError as error:
        raise InputAudioError(
            ErrorCode.MODEL_LOAD_FAILED,
            "numpy/torchaudio are required for separation; "
            "run: pip install -r worker/requirements.txt",
        ) from error

    probe = run_ffprobe(canonical_wav)
    if probe.sample_rate != CANONICAL_SAMPLE_RATE or probe.channels != CANONICAL_CHANNELS:
        raise InputAudioError(
            ErrorCode.INVALID_AUDIO,
            f"canonical WAV must be {CANONICAL_SAMPLE_RATE} Hz stereo, "
            f"got {probe.sample_rate} Hz / {probe.channels}ch",
        )

    try:
        loaded, rate = torchaudio.load(str(canonical_wav), channels_first=True)
    except Exception as error:
        raise InputAudioError(ErrorCode.INVALID_AUDIO, f"could not read decoded WAV: {error}") from error
    if rate != CANONICAL_SAMPLE_RATE:
        raise InputAudioError(ErrorCode.INVALID_AUDIO, "unexpected sample rate in decoded WAV")

    waveform = loaded.numpy().astype(np.float32, copy=False)
    if not np.isfinite(waveform).all():
        raise InputAudioError(ErrorCode.INVALID_AUDIO, "decoded audio contains NaN or infinity")
    if waveform.shape[1] == 0:
        raise InputAudioError(ErrorCode.INVALID_AUDIO, "decoded audio is empty")
    return waveform, probe
