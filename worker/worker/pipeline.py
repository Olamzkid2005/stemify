"""One job's pipeline: validate, separate, encode, package (plan Tasks 8-11).

Each stage function takes explicit inputs and returns explicit outputs; the
job loop owns the database state transitions and temp-directory lifetime.
Every stage raises a typed error (InputAudioError / SeparationError /
OutputError) carrying a stable public code; the loop maps those to job state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from worker.encoding import (
    FORMAT_EXTENSIONS,
    FORMAT_MIME_TYPES,
    OutputError,
    encode_stem,
    sha256_file,
    validate_encoded_output,
    write_wav_master,
)
from worker.errors import ErrorCode
from worker.input_audio import InputAudioError
from worker.models.base import ModelProfile, SeparationError
from worker.models.demucs import separate
from worker.models.profiles import get_profile
from worker.packaging import STEM_LABELS, build_manifest, build_zip, write_manifest
from worker.stages import Stage

PIPELINE_STAGES: tuple[Stage, ...] = (
    Stage.STARTING,
    Stage.DOWNLOADING,
    Stage.VALIDATING,
    Stage.PREPARING_AUDIO,
    Stage.SEPARATING,
    Stage.ENCODING,
    Stage.PACKAGING,
    Stage.CLEANUP,
)


class StageResult:
    """Container for the artifacts one completed job produces."""

    def __init__(self) -> None:
        self.output_rows: list[dict[str, Any]] = []
        self.manifest: dict[str, Any] | None = None


def run_separation_stage(
    canonical_wav: Path,
    job_dir: Path,
    mode: str,
    profile: ModelProfile | None = None,
    progress_callback: Any = None,
    cancellation_checker: Any = None,
) -> dict[str, Any]:
    """Separate the canonical waveform into named numpy stems (plan 12.5)."""
    from worker.input_audio import decode_to_waveform

    waveform, _probe = decode_to_waveform(canonical_wav)
    if progress_callback:
        progress_callback(Stage.SEPARATING, 30)
    stems = separate(
        waveform,
        profile,
        mode=mode,
        progress_callback=None,
        cancellation_checker=cancellation_checker,
    )
    del waveform
    if progress_callback:
        progress_callback(Stage.SEPARATING, 75)
    return stems


def encode_stems_stage(
    stems: dict[str, Any],
    job_dir: Path,
    output_format: str,
    progress_callback: Any = None,
) -> list[dict[str, Any]]:
    """Write WAV masters, encode each stem, and validate the outputs (plan 12.6).

    Returns [{stem_key, path, duration_seconds, sha256}, ...] in stem order.
    """
    if output_format not in FORMAT_EXTENSIONS:
        raise OutputError(ErrorCode.OUTPUT_ENCODING_FAILED, f"unsupported format {output_format!r}")

    masters_dir = job_dir / "masters"
    outputs_dir = job_dir / "outputs"
    extension = FORMAT_EXTENSIONS[output_format]
    encoded: list[dict[str, Any]] = []
    count = len(stems)
    for index, (stem_key, waveform) in enumerate(stems.items()):
        master = write_wav_master(stem_key, waveform, masters_dir)
        del waveform
        dest = outputs_dir / f"{stem_key}.{extension}"
        encode_stem(master, dest, output_format)
        probe = validate_encoded_output(dest, _master_duration(master))
        encoded.append(
            {
                "stem_key": stem_key,
                "path": dest,
                "duration_seconds": probe.duration_seconds,
                "sha256": sha256_file(dest),
                "size_bytes": dest.stat().st_size,
            }
        )
        if progress_callback:
            progress_callback(Stage.ENCODING, 75 + int((index + 1) / count * 15))
    return encoded


def package_stage(
    job_id: str,
    encoded_stems: list[dict[str, Any]],
    job_dir: Path,
    mode: str,
    output_format: str,
    source_display_name: str,
    source_duration_seconds: float,
    source_sample_rate: int,
    source_channels: int,
    profile: ModelProfile,
    expires_at_ms: int | None,
    progress_callback: Any = None,
) -> StageResult:
    """Build the manifest and ZIP from validated encoded stems (plan 12.7)."""
    result = StageResult()
    if progress_callback:
        progress_callback(Stage.PACKAGING, 90)

    manifest = build_manifest(
        job_id=job_id,
        source_display_name=source_display_name,
        source_duration_seconds=source_duration_seconds,
        source_sample_rate=source_sample_rate,
        source_channels=source_channels,
        mode=mode,
        output_format=output_format,
        stems=[
            {
                "name": stem["stem_key"],
                "durationSeconds": stem["duration_seconds"],
                "sha256": stem["sha256"],
            }
            for stem in encoded_stems
        ],
        model_id=profile.model_id,
        model_revision=profile.revision,
        mixture_consistency=profile.instrumental_policy == "mixture_minus_vocals"
        and mode == "vocals_instrumental",
    )
    write_manifest(manifest, job_dir / "outputs" / "manifest.json")

    stem_files = [(stem["stem_key"], stem["path"]) for stem in encoded_stems]
    zip_path = job_dir / "outputs" / "stems.zip"
    build_zip(stem_files, manifest, zip_path)
    if progress_callback:
        progress_callback(Stage.PACKAGING, 98)

    extension = FORMAT_EXTENSIONS[output_format]
    mime = FORMAT_MIME_TYPES[output_format]
    for stem in encoded_stems:
        result.output_rows.append(
            {
                "stem_key": stem["stem_key"],
                "label": STEM_LABELS.get(stem["stem_key"], stem["stem_key"]),
                "relative_path": f"results/{job_id}/{stem['stem_key']}.{extension}",
                "mime_type": mime,
                "size_bytes": stem["size_bytes"],
                "duration_seconds": stem["duration_seconds"],
                "sha256": stem["sha256"],
                "expires_at": expires_at_ms,
            }
        )
    result.output_rows.append(
        {
            "stem_key": "archive",
            "label": "All stems (ZIP)",
            "relative_path": f"results/{job_id}/stems.zip",
            "mime_type": "application/zip",
            "size_bytes": zip_path.stat().st_size,
            "duration_seconds": None,
            "sha256": sha256_file(zip_path),
            "expires_at": expires_at_ms,
        }
    )
    result.manifest = manifest
    return result


def _master_duration(master: Path) -> float:
    from worker.input_audio import run_ffprobe

    return run_ffprobe(master).duration_seconds


__all__ = [
    "PIPELINE_STAGES",
    "InputAudioError",
    "OutputError",
    "SeparationError",
    "StageResult",
    "encode_stems_stage",
    "get_profile",
    "package_stage",
    "run_separation_stage",
]
