"""Worker CLI (plan Section 12.1): health diagnostics, cleanup, one-shot separation.

    python -m worker.cli health
    python -m worker.cli cleanup
    python -m worker.cli separate --input ./fixtures/song.mp3 \
        --mode vocals_instrumental --format mp3 --output ./artifacts
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def _check_ffmpeg() -> tuple[bool, str]:
    try:
        from worker.input_audio import _require_ffmpeg_tool

        ffmpeg = _require_ffmpeg_tool("ffmpeg")
        _require_ffmpeg_tool("ffprobe")
        return True, ffmpeg
    except Exception as error:  # noqa: BLE001 - diagnostic command reports, never raises
        return False, str(error)


def _check_python_packages() -> dict[str, bool]:
    results = {}
    # yt-dlp is optional (YouTube input only); report it, never gate on it.
    for module in ("numpy", "torch", "torchaudio", "soundfile", "demucs", "yt_dlp"):
        try:
            __import__(module)
            results[module] = True
        except ImportError:
            results[module] = False
    return results


def _check_data_dir() -> tuple[bool, str]:
    try:
        from worker.database import JobQueue

        queue = JobQueue()
        path = queue.database_path
        queue.close()
        return True, str(path)
    except Exception as error:  # noqa: BLE001
        return False, str(error)


def _check_model_profile() -> tuple[bool, str]:
    try:
        from worker.models.profiles import get_profile, validate_profile

        profile = get_profile(__import__("os").environ.get("STEMIFY_MODEL_PROFILE", "demucs_default"))
        validate_profile(profile)
        return True, f"{profile.profile_id} ({profile.model_id})"
    except Exception as error:  # noqa: BLE001
        return False, str(error)


def command_health(_args: argparse.Namespace) -> int:
    import platform

    print(f"python: {platform.python_version()}")
    ffmpeg_ok, ffmpeg_detail = _check_ffmpeg()
    print(f"ffmpeg/ffprobe: {'ok' if ffmpeg_ok else 'MISSING'} ({ffmpeg_detail})")

    packages = _check_python_packages()
    for name, ok in packages.items():
        print(f"{name}: {'ok' if ok else 'missing'}")

    try:
        import torch

        cuda = torch.cuda.is_available()
        print(f"cuda: {'available (' + torch.cuda.get_device_name(0) + ')' if cuda else 'not available'}")
    except ImportError:
        print("cuda: unknown (torch not installed)")

    profile_ok, profile_detail = _check_model_profile()
    print(f"model profile: {'ok' if profile_ok else 'INVALID'} ({profile_detail})")

    data_ok, data_detail = _check_data_dir()
    print(f"sqlite: {'ok' if data_ok else 'FAILED'} ({data_detail})")

    core_ok = ffmpeg_ok and packages["numpy"] and packages["soundfile"] and data_ok
    engine_ok = core_ok and packages["torch"] and packages["demucs"] and profile_ok
    youtube_ok = packages.get("yt_dlp", False)
    print(f"control plane: {'ready' if core_ok else 'NOT READY'}")
    print(f"separation engine: {'ready' if engine_ok else 'NOT READY (pip install -r worker/requirements.txt)'}")
    print(f"youtube input: {'ready' if youtube_ok else 'unavailable (optional; pip install yt-dlp)'}")
    return 0 if core_ok else 1


def command_separate(args: argparse.Namespace) -> int:
    from worker.encoding import OutputError
    from worker.input_audio import InputAudioError, JobTempDir, prepare_source
    from worker.models.base import SeparationError
    from worker.models.profiles import get_profile_for_mode
    from worker.pipeline import encode_stems_stage, package_stage, run_separation_stage

    source = Path(args.input).resolve()
    output_root = Path(args.output).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    profile = get_profile_for_mode(args.mode)

    try:
        with JobTempDir() as job_dir:
            staged = job_dir / source.name
            shutil.copy(source, staged)
            canonical, probe = prepare_source(staged, job_dir)
            print(f"validated: {probe.duration_seconds:.1f}s, {probe.sample_rate} Hz, {probe.channels}ch")

            stems, _mixture = run_separation_stage(canonical, job_dir, args.mode, profile)
            print(f"separated: {', '.join(sorted(stems))}")

            encoded = encode_stems_stage(stems, job_dir, args.format)
            packaged = package_stage(
                job_id=f"job_{'cli' + '0' * 29}",
                encoded_stems=encoded,
                job_dir=job_dir,
                mode=args.mode,
                output_format=args.format,
                source_display_name=source.name,
                source_duration_seconds=probe.duration_seconds,
                source_sample_rate=probe.sample_rate,
                source_channels=probe.channels,
                profile=profile,
                expires_at_ms=None,
            )
            for row in packaged.output_rows:
                dest = output_root / Path(row["relative_path"]).name
                shutil.copy(job_dir / "outputs" / Path(row["relative_path"]).name, dest)
                print(f"wrote {dest}")
    except (InputAudioError, SeparationError, OutputError) as error:
        print(f"error [{error.code.value}]: {error}", file=sys.stderr)
        return 1
    return 0


def command_cleanup(args: argparse.Namespace) -> int:
    """Run one idempotent cleanup pass (plan Task 13 / Section 19.3)."""
    from worker.cleanup import run_cleanup
    from worker.database import JobQueue

    queue = JobQueue()
    try:
        report = run_cleanup(queue)
    finally:
        queue.close()
    print(report.summary())
    for error in report.errors:
        print(f"error: {error}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="worker.cli", description="Stemify worker diagnostics")
    subparsers = parser.add_subparsers(dest="command", required=True)

    health = subparsers.add_parser("health", help="report FFmpeg/SQLite/model readiness")
    health.set_defaults(func=command_health)

    cleanup = subparsers.add_parser("cleanup", help="delete expired jobs, uploads, and stale temp dirs")
    cleanup.set_defaults(func=command_cleanup)

    separate = subparsers.add_parser("separate", help="one-shot separation of a local file")
    separate.add_argument("--input", required=True, help="path to a supported audio file")
    separate.add_argument("--mode", default="vocals_instrumental", choices=["vocals_instrumental", "full_stems"])
    separate.add_argument("--format", default="mp3", choices=["mp3", "wav", "flac", "ogg", "m4a"])
    separate.add_argument("--output", required=True, help="directory for the generated stems")
    separate.set_defaults(func=command_separate)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
