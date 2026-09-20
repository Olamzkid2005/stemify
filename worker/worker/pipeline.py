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


# Progress landmarks for the separation stage: the model callback's 0.0-1.0
# fraction maps onto this window, so the bar moves continuously through the
# longest part of the job instead of parking on one value.
SEPARATION_START_PCT = 30
SEPARATION_END_PCT = 75

# The model callback reports per-chunk completion as done/total; scale the
# smoothed value into the separation window's interior so 30 and 75 remain the
# stage's boundary events.
SEPARATION_INTERIOR_START = SEPARATION_START_PCT + 1  # 31
SEPARATION_INTERIOR_END = SEPARATION_END_PCT - 1  # 74


def _separation_progress(fraction: float) -> int:
    """Map a 0.0-1.0 model fraction into the separation stage's percent window."""
    clamped = min(1.0, max(0.0, fraction))
    return SEPARATION_INTERIOR_START + int(
        clamped * (SEPARATION_INTERIOR_END - SEPARATION_INTERIOR_START)
    )


def _encode_progress(done: int, total: int) -> int:
    """Map stem-encoding completion into the 75-90 window."""
    safe_total = max(1, total)
    clamped = min(1.0, max(0.0, done / safe_total))
    return 75 + int(clamped * 15)


def run_separation_stage(
    canonical_wav: Path,
    job_dir: Path,
    mode: str,
    profile: ModelProfile | None = None,
    progress_callback: Any = None,
    cancellation_checker: Any = None,
    quality: str | None = None,
) -> tuple[dict[str, Any], Any]:
    """Separate the canonical waveform into named numpy stems (plan 12.5).

    Returns (stems, mixture): the mixture is kept for the analysis stage
    (roadmap Phase C), which needs the full waveform for key detection.
    Drum subdivision (roadmap Phase B) dispatches to the drumsep adapter, which
    consumes the isolated drums stem as its "mixture". `full_stems` is the
    non-vocal rhythm-section split (drums, bass, instrumental).

    Progress: 30 on entry, 31-74 continuously from the model's per-chunk
    callback, 75 once separation returns. On engines that cannot report per
    chunk (the drift fallback), the bar still parks on 30 for the inference
    itself — audio correctness outranks bar smoothness there.
    """
    from worker.input_audio import decode_to_waveform

    waveform, _probe = decode_to_waveform(canonical_wav)
    if progress_callback:
        progress_callback(Stage.SEPARATING, SEPARATION_START_PCT)

    # The model reports every finished chunk, and a handful can land on the same
    # whole percent (there are only 43 percent steps across a multi-minute
    # inference). Each update costs a DB write and a job_events row, and the job
    # page only reads the newest one, so identical consecutive percents are
    # dropped here.
    last_percent = SEPARATION_START_PCT

    def _on_model_progress(fraction: float) -> None:
        nonlocal last_percent
        if progress_callback is None:
            return
        percent = _separation_progress(fraction)
        if percent == last_percent:
            return
        last_percent = percent
        progress_callback(Stage.SEPARATING, percent)

    if mode == "drum_breakdown":
        from worker.models.drumsep import separate as separate_drums

        stems = separate_drums(
            waveform,
            profile,
            mode=mode,
            progress_callback=_on_model_progress,
            cancellation_checker=cancellation_checker,
            quality=quality,
        )
    else:
        stems = separate(
            waveform,
            profile,
            mode=mode,
            progress_callback=_on_model_progress,
            cancellation_checker=cancellation_checker,
            quality=quality,
        )
    mixture = waveform
    if progress_callback:
        progress_callback(Stage.SEPARATING, SEPARATION_END_PCT)
    return stems, mixture


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
            progress_callback(Stage.ENCODING, _encode_progress(index + 1, count))
    return encoded


def analyze_stage(
    stems: dict[str, Any],
    mixture: Any,
    sample_rate: int = 44100,
) -> tuple[dict[str, Any] | None, str | None]:
    """BPM + key analysis (roadmap Phase C). Never raises; degrades to None."""
    from worker.analysis import analyze_track

    result = analyze_track(
        drums_stem=stems.get("drums"),
        mixture=mixture,
        sample_rate=sample_rate,
    )
    analysis = result.analysis
    return (
        {"bpm": analysis.bpm, "key": analysis.key, "camelot": analysis.camelot}
        if analysis is not None
        else None,
        result.degraded,
    )


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
    analysis: dict[str, Any] | None = None,
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
        and mode in ("vocals_instrumental", "full_stems"),
        analysis=analysis,
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
    "SEPARATION_END_PCT",
    "SEPARATION_START_PCT",
    "InputAudioError",
    "OutputError",
    "SeparationError",
    "StageResult",
    "analyze_stage",
    "encode_stems_stage",
    "get_profile",
    "package_stage",
    "run_separation_stage",
]
