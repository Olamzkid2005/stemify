"""Task 16 tests: benchmark plumbing and device-policy messaging.

The heavy real-engine measurements run manually (python -m worker.benchmark);
these tests keep the benchmark module honest without requiring the model
stack: fixture synthesis, report assembly with stubbed measurements, and the
actionable-message guarantees the plan's acceptance criteria name.
"""

from __future__ import annotations

import json
import wave
from pathlib import Path
from typing import Any

import pytest

from worker.benchmark import DEFAULT_DURATIONS, _environment, _make_tone_wav, run_benchmark


def test_fixture_is_deterministic_and_valid(tmp_path: Path) -> None:
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    size_a = _make_tone_wav(a, 2.0)
    size_b = _make_tone_wav(b, 2.0)
    assert size_a == size_b  # same seed -> byte-identical content
    assert a.read_bytes() == b.read_bytes()

    with wave.open(str(a), "rb") as handle:
        assert handle.getnchannels() == 2
        assert handle.getframerate() == 44100
        assert handle.getnframes() == int(44100 * 2.0)


def test_fixture_scales_with_duration(tmp_path: Path) -> None:
    short = _make_tone_wav(tmp_path / "short.wav", 1.0)
    long = _make_tone_wav(tmp_path / "long.wav", 4.0)
    assert long > short * 3  # roughly proportional, header overhead aside


def test_environment_reports_without_cuda() -> None:
    env = _environment()
    assert env["profile"] == "demucs_default"
    assert env["model_id"] == "htdemucs"
    assert env["device_policy"] == "auto"
    # The keys the docs rely on are always present, even without psutil/CUDA.
    for key in ("python", "platform", "cpu_count", "torch_version", "cuda_available"):
        assert key in env


def test_run_benchmark_with_stubbed_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report assembly works end-to-end without loading the real model."""

    import worker.benchmark as bench

    monkeypatch.setattr(bench, "measure_model_load", lambda profile_id: {"cold_seconds": 1.0, "warm_seconds": 0.0})
    monkeypatch.setattr(
        bench,
        "measure_separation",
        lambda seconds, profile_id: {
            "duration_seconds": seconds,
            "separation_seconds": seconds * 2,
            "realtime_factor": 2.0,
            "rss_mb": 900.0,
            "rss_delta_mb": 400.0,
            "python_peak_alloc_mb": 50.0,
            "encoded_mp3_bytes": 123,
            "sample_rate": 44100,
            "channels": 2,
        },
    )
    monkeypatch.setattr(
        bench,
        "measure_encoding",
        lambda seconds, profile_id, formats: [
            {"format": fmt, "encode_seconds": 0.5, "bytes": 456} for fmt in formats
        ],
    )

    report = run_benchmark([3.0], ("mp3",), "demucs_default")
    data = json.loads(json.dumps(_dataclass_to_dict(report)))  # JSON-serializable

    assert data["model_load"]["cold_seconds"] == 1.0
    assert data["separation"][0]["duration_seconds"] == 3.0
    assert data["separation"][0]["realtime_factor"] == 2.0
    assert data["encoding"][0]["format"] == "mp3"
    assert "total_seconds" in data["totals"]


def _dataclass_to_dict(obj: Any) -> Any:
    from dataclasses import asdict, is_dataclass

    if is_dataclass(obj):
        return asdict(obj)
    raise TypeError(obj)


def test_default_durations_keep_benchmarks_short() -> None:
    """The default run must stay a minutes-scale operation (plan 18 spirit)."""
    assert max(DEFAULT_DURATIONS) <= 30


# ---------------------------------------------------------------------------
# Device-policy messaging (plan Task 16 acceptance: clear setup/limit messages)
# ---------------------------------------------------------------------------


def test_cuda_request_without_gpu_fails_with_actionable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types

    from worker.errors import ErrorCode
    from worker.models.base import SeparationError
    from worker.models.demucs import resolve_device

    fake_torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: False))
    monkeypatch.setattr("worker.models.demucs._import_torch", lambda: fake_torch)

    with pytest.raises(SeparationError) as excinfo:
        resolve_device("cuda")
    assert excinfo.value.code == ErrorCode.MODEL_LOAD_FAILED
    message = str(excinfo.value)
    assert "STEMIFY_DEVICE=cpu" in message  # names the concrete fix
    assert "CUDA" in message


def test_cpu_request_never_touches_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    from worker.models.demucs import resolve_device

    def explode() -> None:
        raise AssertionError("cpu policy must not probe CUDA")

    fake_torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=explode))
    monkeypatch.setattr("worker.models.demucs._import_torch", lambda: fake_torch)
    assert resolve_device("cpu") == "cpu"


def test_auto_falls_back_to_cpu_without_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    from worker.models.demucs import resolve_device

    fake_torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: False))
    monkeypatch.setattr("worker.models.demucs._import_torch", lambda: fake_torch)
    assert resolve_device("auto") == "cpu"
