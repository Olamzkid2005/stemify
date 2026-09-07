"""Output encoding (plan Task 10 / Sections 12.6, 9.3).

Encodes validated float32 WAV masters into the user-selected format with
FFmpeg argument arrays (never shell interpolation), then re-probes every
encoded file with ffprobe before it may be published.

Format policy (documented, one place):
  mp3  -> libmp3lame, 320 kbps CBR
  wav  -> PCM signed 16-bit little-endian
  flac -> lossless (default compression)
  ogg  -> libvorbis, quality 8 (~256 kbps VBR)
  m4a  -> AAC, 256 kbps in an M4A container

All encoders resample/remix to the canonical layout (44.1 kHz stereo) so stem
durations and channel counts stay aligned across formats.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from worker.errors import ErrorCode
from worker.input_audio import (
    CANONICAL_CHANNELS,
    CANONICAL_SAMPLE_RATE,
    _require_ffmpeg_tool,
    run_ffprobe,
)

# Aligned with packages/contracts common.schema.json outputFormat.
FORMAT_EXTENSIONS: dict[str, str] = {
    "mp3": "mp3",
    "wav": "wav",
    "flac": "flac",
    "ogg": "ogg",
    "m4a": "m4a",
}

# Stored in job_outputs.mime_type; the web serves these back as download types.
FORMAT_MIME_TYPES: dict[str, str] = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "m4a": "audio/mp4",
}

# Per-format encoder arguments appended after the common flags (plan 12.6).
_ENCODER_ARGS: dict[str, tuple[str, ...]] = {
    "mp3": ("-c:a", "libmp3lame", "-b:a", "320k"),
    "wav": ("-c:a", "pcm_s16le"),
    "flac": ("-c:a", "flac"),
    "ogg": ("-c:a", "libvorbis", "-q:a", "8"),
    "m4a": ("-c:a", "aac", "-b:a", "256k"),
}

# Encoded duration may drift slightly from the master for lossy formats.
DURATION_TOLERANCE_SECONDS = 0.5

_ENCODE_TIMEOUT_SECONDS = 120


class OutputError(Exception):
    """Encoding/packaging failure carrying a stable public error code."""

    def __init__(self, code: ErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def sha256_file(path: Path) -> str:
    """Streamed sha256 hex digest; recorded in job_outputs and the manifest."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(2**20), b""):
            digest.update(block)
    return digest.hexdigest()


def encode_stem(wav_master: Path, dest: Path, output_format: str) -> None:
    """Encode one validated WAV master into dest in the selected format."""
    if output_format not in _ENCODER_ARGS:
        raise OutputError(
            ErrorCode.OUTPUT_ENCODING_FAILED,
            f"unsupported output format {output_format!r}",
        )
    ffmpeg = _require_ffmpeg_tool("ffmpeg")
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-v",
        "error",
        "-y",
        "-i",
        str(wav_master),
        "-vn",  # masters are audio-only; keep the flag for defense in depth
        "-ar",
        str(CANONICAL_SAMPLE_RATE),
        "-ac",
        str(CANONICAL_CHANNELS),
        "-map_metadata",
        "-1",  # clean output: no source metadata carried into stems
        *_ENCODER_ARGS[output_format],
        str(dest),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_ENCODE_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired as error:
        raise OutputError(ErrorCode.OUTPUT_ENCODING_FAILED, "encoding timed out") from error
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        raise OutputError(
            ErrorCode.OUTPUT_ENCODING_FAILED,
            f"encoder failed: {proc.stderr.decode(errors='replace')[:200]}",
        )


def write_wav_master(stem_name: str, waveform: Any, dest_dir: Path) -> Path:
    """Write a float32 WAV master from a numpy (channels, samples) waveform.

    Masters are the single encoding source (plan 12.6): every output format is
    encoded from these files, never directly from tensors.
    """
    try:
        import numpy as np
        import soundfile as sf
    except ImportError as error:
        raise OutputError(
            ErrorCode.OUTPUT_ENCODING_FAILED,
            "numpy/soundfile are required for encoding; "
            "run: pip install -r worker/requirements.txt",
        ) from error

    array = np.asarray(waveform, dtype=np.float32)
    if array.ndim != 2 or array.shape[0] not in (1, 2):
        raise OutputError(ErrorCode.OUTPUT_ENCODING_FAILED, f"invalid waveform shape {array.shape}")
    if not np.isfinite(array).all():
        raise OutputError(ErrorCode.OUTPUT_ENCODING_FAILED, "waveform contains NaN or infinity")

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{stem_name}.wav"
    try:
        sf.write(str(dest), array.T, CANONICAL_SAMPLE_RATE, subtype="FLOAT", format="WAV")
    except Exception as error:
        raise OutputError(ErrorCode.OUTPUT_ENCODING_FAILED, f"could not write WAV master: {error}") from error
    return dest


def validate_encoded_output(encoded: Path, master_duration_seconds: float) -> Any:
    """Re-probe an encoded stem and enforce the output contract (plan 12.6).

    Returns the ffprobe result so callers can record duration/metadata.
    """
    if not encoded.is_file() or encoded.stat().st_size == 0:
        raise OutputError(ErrorCode.OUTPUT_VALIDATION_FAILED, "encoded output is missing or empty")
    probe = run_ffprobe(encoded)
    if not probe.has_audio:
        raise OutputError(ErrorCode.OUTPUT_VALIDATION_FAILED, "encoded output has no audio stream")
    if abs(probe.duration_seconds - master_duration_seconds) > DURATION_TOLERANCE_SECONDS:
        raise OutputError(
            ErrorCode.OUTPUT_VALIDATION_FAILED,
            f"encoded duration {probe.duration_seconds:.2f}s drifts from master "
            f"{master_duration_seconds:.2f}s",
        )
    if probe.sample_rate != CANONICAL_SAMPLE_RATE or probe.channels != CANONICAL_CHANNELS:
        raise OutputError(
            ErrorCode.OUTPUT_VALIDATION_FAILED,
            f"encoded output is {probe.sample_rate} Hz / {probe.channels}ch, "
            f"expected {CANONICAL_SAMPLE_RATE} Hz / {CANONICAL_CHANNELS}ch",
        )
    return probe
