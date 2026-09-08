"""Performance benchmarks (plan Task 16 / Section 18).

Measures the real separation stack on this machine and prints a JSON report:

- cold vs warm model load (first load downloads/reads the checkpoint; warm
  reuses the in-process cache)
- separation throughput at several fixture durations (vocals_instrumental)
- per-output-format encode timing (mp3, wav, flac, ogg, m4a)
- peak RSS, disk footprint, and total wall time

Everything is offline: fixtures are synthesized tones, and the checkpoint is
the normal allowlisted htdemucs download cached under data/models.

Usage (from worker/):

    PYTHONPATH="$PWD/.runtime" python -m worker.benchmark --durations 5 15 30
    PYTHONPATH="$PWD/.runtime" python -m worker.benchmark --quick   # 5s fixture

Results feed docs/BENCHMARKS.md and the duration-limit decision (plan 18).
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import tempfile
import time
import tracemalloc
from dataclasses import asdict, dataclass, field
from pathlib import Path

from worker.models.demucs import _LOADED_MODELS, load_model, separate
from worker.models.profiles import DEFAULT_PROFILE, get_profile
from worker.pipeline import encode_stems_stage

# Small by default: a 4-core CPU laptop separates roughly real-time-per-stem,
# so keep total runtime minutes, not hours. Durations are seconds of audio.
DEFAULT_DURATIONS = (5.0, 15.0)
ALL_FORMATS = ("mp3", "wav", "flac", "ogg", "m4a")


@dataclass
class BenchmarkReport:
    environment: dict = field(default_factory=dict)
    model_load: dict = field(default_factory=dict)
    separation: list[dict] = field(default_factory=list)
    encoding: list[dict] = field(default_factory=list)
    totals: dict = field(default_factory=dict)


def _environment() -> dict:
    import multiprocessing

    import torch

    machine = platform.machine()
    ram_gb = None
    try:
        import psutil

        ram_gb = round(psutil.virtual_memory().total / 1024**3, 1)
    except ImportError:
        pass
    return {
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()} {machine}",
        "processor": platform.processor(),
        "cpu_count": multiprocessing.cpu_count(),
        "torch_threads": torch.get_num_threads(),
        "ram_gb": ram_gb,
        "cuda_available": torch.cuda.is_available(),
        "cuda_device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "torch_version": torch.__version__,
        "profile": DEFAULT_PROFILE.profile_id,
        "model_id": DEFAULT_PROFILE.model_id,
        "device_policy": DEFAULT_PROFILE.device_policy,
    }


def _make_tone_wav(path: Path, seconds: float, sample_rate: int = 44100) -> float:
    """Synthesize a stereo test tone and return the file's size in bytes.

    Uses numpy -> wave directly (no ffmpeg dependency for fixture generation).
    """
    import wave

    import numpy as np

    rng = np.random.default_rng(20240916)  # deterministic content
    t = np.linspace(0.0, seconds, int(sample_rate * seconds), endpoint=False)
    # A chord-ish sum: keeps the encoder honest without being pure silence.
    audio = (
        0.25 * np.sin(2 * np.pi * 220.0 * t)
        + 0.2 * np.sin(2 * np.pi * 330.0 * t)
        + 0.1 * rng.standard_normal(t.shape[0])
    ).astype("float32")
    stereo = np.stack([audio, audio], axis=1)  # (samples, channels)
    pcm = (np.clip(stereo, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
    return path.stat().st_size


def _rss_mb() -> float:
    try:
        import psutil

        return psutil.Process().memory_info().rss / 1024 / 1024
    except ImportError:
        return float("nan")


def _peak_rss_mb() -> float:
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS reports bytes, Linux reports KiB.
        return peak / 1024 if peak > 10**7 else float(peak) / 1024
    except (ImportError, OSError):
        return float("nan")


def measure_model_load(profile_id: str) -> dict:
    """Cold and warm load timings; the cold run may download the checkpoint."""
    profile = get_profile(profile_id)
    _LOADED_MODELS.clear()  # force the cold path

    start = time.perf_counter()
    load_model(profile)
    cold = time.perf_counter() - start

    start = time.perf_counter()
    load_model(profile)
    warm = time.perf_counter() - start

    checkpoint_path = _find_checkpoint(profile)
    return {
        "cold_seconds": round(cold, 2),
        "warm_seconds": round(warm, 2),
        "checkpoint_bytes": checkpoint_path.stat().st_size if checkpoint_path else None,
    }


def _find_checkpoint(profile) -> Path | None:
    """Locate the cached checkpoint file (torch.hub layout) for size reporting."""
    import os

    cache_root = Path(os.environ.get("STEMIFY_MODEL_DIR", "data/models"))
    for candidate in cache_root.rglob("*.th"):
        if profile.checkpoint_checksum in candidate.name:
            return candidate
    return None


def measure_separation(seconds: float, profile_id: str) -> dict:
    """Separate one fixture and record time, RSS, and output disk footprint."""
    profile = get_profile(profile_id)
    with tempfile.TemporaryDirectory(prefix="stemify-bench-") as raw_dir:
        job_dir = Path(raw_dir)
        fixture = job_dir / "fixture.wav"
        _make_tone_wav(fixture, seconds)

        from worker.input_audio import decode_to_canonical_wav, validate_source

        probe = validate_source(fixture)
        canonical = job_dir / "input.wav"
        decode_to_canonical_wav(fixture, canonical)

        tracemalloc.start()
        rss_before = _rss_mb()
        start = time.perf_counter()
        stems = separate(_load_waveform(canonical), profile)
        elapsed = time.perf_counter() - start
        rss_peak = _rss_mb()
        _, peak_malloc = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Encode to mp3 to get a realistic on-disk footprint per duration.
        encoded = encode_stems_stage(stems, job_dir, "mp3")
        disk_bytes = sum(row["size_bytes"] for row in encoded)

        return {
            "duration_seconds": seconds,
            "separation_seconds": round(elapsed, 2),
            "realtime_factor": round(elapsed / seconds, 3),  # <1 means faster than realtime
            "rss_mb": round(rss_peak, 1),
            "rss_delta_mb": round(rss_peak - rss_before, 1),
            "python_peak_alloc_mb": round(peak_malloc / 1024 / 1024, 1),
            "encoded_mp3_bytes": disk_bytes,
            "sample_rate": probe.sample_rate,
            "channels": probe.channels,
        }


def _load_waveform(canonical_wav: Path):
    """Reuse the production decoder so the benchmark measures the real path."""
    from worker.input_audio import decode_to_waveform

    waveform, _probe = decode_to_waveform(canonical_wav)
    return waveform


def measure_encoding(seconds: float, profile_id: str, formats: tuple[str, ...]) -> list[dict]:
    """Per-format encode timing over one separation result."""
    profile = get_profile(profile_id)
    with tempfile.TemporaryDirectory(prefix="stemify-bench-enc-") as raw_dir:
        job_dir = Path(raw_dir)
        fixture = job_dir / "fixture.wav"
        _make_tone_wav(fixture, seconds)
        from worker.input_audio import decode_to_canonical_wav, decode_to_waveform

        canonical = job_dir / "input.wav"
        decode_to_canonical_wav(fixture, canonical)
        waveform, _ = decode_to_waveform(canonical)
        stems = separate(waveform, profile)

        rows = []
        for fmt in formats:
            start = time.perf_counter()
            encoded = encode_stems_stage(stems, job_dir, fmt)
            elapsed = time.perf_counter() - start
            disk_bytes = sum(row["size_bytes"] for row in encoded)
            rows.append(
                {
                    "format": fmt,
                    "encode_seconds": round(elapsed, 3),
                    "bytes": disk_bytes,
                }
            )
            shutil.rmtree(job_dir / "outputs", ignore_errors=True)
        return rows


def run_benchmark(durations: list[float], formats: tuple[str, ...], profile_id: str) -> BenchmarkReport:
    overall_start = time.perf_counter()
    report = BenchmarkReport(
        environment=_environment(),
        model_load=measure_model_load(profile_id),
    )

    for seconds in durations:
        report.separation.append(measure_separation(seconds, profile_id))

    longest = max(durations) if durations else DEFAULT_DURATIONS[0]
    report.encoding = measure_encoding(longest, profile_id, formats)

    rss = _rss_mb()
    report.totals = {
        "total_seconds": round(time.perf_counter() - overall_start, 1),
        "final_rss_mb": round(rss, 1),
        "python_version": platform.python_version(),
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(prog="worker.benchmark", description=__doc__)
    parser.add_argument(
        "--durations",
        type=float,
        nargs="+",
        default=list(DEFAULT_DURATIONS),
        help="fixture durations in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--formats",
        nargs="+",
        default=list(ALL_FORMATS),
        choices=list(ALL_FORMATS),
        help="output formats to time (default: all)",
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE.profile_id)
    parser.add_argument(
        "--quick",
        action="store_true",
        help="shorthand for --durations 5 (a fast sanity run)",
    )
    args = parser.parse_args()
    if args.quick:
        args.durations = [5.0]

    report = run_benchmark(args.durations, tuple(args.formats), args.profile)
    print(json.dumps(asdict(report), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
