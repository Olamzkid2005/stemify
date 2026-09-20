"""Real-engine regression tests for per-chunk separation progress (no stubs).

`worker.models.demucs._apply_model_with_progress` replicates demucs 4.0.1's
apply_model so every finished chunk can report progress; the job bar used to sit
at one value for the whole separation because the adapter callback was dropped.
Three properties matter, and only the real engine can settle them:

1. The replication must be *bit-identical* to apply_model — otherwise the moving
   bar would have been bought with subtly different audio. The drift guard in
   _apply_path_matches_installed only checks that the installed demucs source
   still looks like what we replicate; it cannot prove the math matches.
2. Progress must advance per chunk and per shift pass, ending at exactly 1.0.
3. The chunked path must handle a short tail chunk (the final chunk is narrower
   than the segment, which is where window slicing is easiest to get wrong).

Settings match production in the first test: the profile's chunk_length_seconds
is None, so the chunk length comes from the model — and the htdemucs checkpoint
loads as a BagOfModels, which carries no `.segment` of its own. Reading
`model.segment` directly failed every real job; the fast fake-model coverage for
that lives in tests/test_models.py.

Skips cleanly when the model stack is unavailable. Slow by design (~1-2 minutes
of real inference), so deselect with -k when iterating: these are the guard, not
the inner loop.
"""

from __future__ import annotations

import random
from typing import Any

import numpy as np
import pytest

try:
    import demucs.apply as demucs_apply
    import torch as th

    ENGINE_AVAILABLE = True
except Exception:  # noqa: BLE001 - any import failure means: skip
    ENGINE_AVAILABLE = False

from worker.models.demucs import (
    _LOADED_MODELS,
    _apply_model_with_progress,
    _apply_path_matches_installed,
    _chunk_offsets,
    get_profile,
    load_model,
)
from worker.models.profiles import DEFAULT_PROFILE_ID, resolve_quality

pytestmark = pytest.mark.skipif(
    not ENGINE_AVAILABLE, reason="real separation engine (torch/demucs) not available"
)

# Same checkpoint the app downloads; reused from the repo when present so these
# tests stay offline-friendly instead of pulling 80MB per run.
APP_CHECKPOINT = "955717e8-8726e21a.th"
CHECKPOINT_MIN_BYTES = 84_141_911

# Deliberately short: every second of audio is processed once per shift pass for
# both the reference and the replication. 4 seconds at the model's own 7.8s
# chunk length is one chunk per pass — enough to prove pass scaling and
# unchanged audio without paying for a whole song.
CLIP_SECONDS = 4
# 6 seconds at the model's 7.8s chunk length with overlap 0.4 yields exactly
# two chunks, the second only ~58k of 344k samples long: a real, very short tail
# chunk, which is where the window slicing is easiest to get wrong.
MULTI_CHUNK_SECONDS = 6.0
SEED = 20240920


# Environment keys this module needs to control. Saved and restored by hand
# because the fixture is module scoped (monkeypatch is function scoped), which
# also lets both tests share one ~80MB model load.
_ENV_KEYS = ("STEMIFY_DATA_DIR", "TORCH_HOME", "STEMIFY_MODEL_PROFILE", "STEMIFY_QUALITY")


@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Load the real model once, reusing the repo checkpoint when available."""
    import os
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    checkpoint = repo_root / "data" / "models" / "hub" / "checkpoints" / APP_CHECKPOINT
    if checkpoint.is_file() and checkpoint.stat().st_size >= CHECKPOINT_MIN_BYTES:
        data_dir = repo_root / "data"  # read-only reuse of the app's checkpoint
    else:
        data_dir = tmp_path_factory.mktemp("engine_progress_models")

    saved = {key: os.environ.get(key) for key in _ENV_KEYS}
    os.environ["STEMIFY_DATA_DIR"] = str(data_dir)
    for key in ("TORCH_HOME", "STEMIFY_MODEL_PROFILE", "STEMIFY_QUALITY"):
        os.environ.pop(key, None)

    # Start (and finish) with an empty cache so this module neither inherits nor
    # leaks a loaded model into other tests (test_dod_e2e asserts on cache size).
    _LOADED_MODELS.clear()
    try:
        profile = get_profile(DEFAULT_PROFILE_ID)
        model, device = load_model(profile)
        quality_name, overlap, shifts = resolve_quality(None)
        yield {
            "profile": profile,
            "model": model,
            "device": device,
            "quality": quality_name,
            "overlap": overlap,
            "shifts": shifts,
        }
    finally:
        _LOADED_MODELS.clear()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _clip(seconds: float) -> np.ndarray:
    """Deterministic stereo noise: stable across runs, unlike a pure tone."""
    rng = np.random.default_rng(SEED)
    return rng.uniform(-0.3, 0.3, size=(2, int(44100 * seconds))).astype(np.float32)


def _assert_replication_matches(
    engine: Any, waveform: np.ndarray, *, segment: float | None, shifts: int | None = None
) -> list[float]:
    """Run apply_model and the replication on the same input; return progress.

    Both runs get the same random seed so demucs' random shift offsets match —
    without that the outputs differ for a reason that has nothing to do with the
    replication.
    """
    shift_count = engine["shifts"] if shifts is None else shifts
    tensor = th.from_numpy(waveform).to(engine["device"])[None]
    seen: list[float] = []

    def reference() -> Any:
        random.seed(SEED)
        with th.no_grad():
            return demucs_apply.apply_model(
                engine["model"],
                tensor,
                device=engine["device"],
                shifts=shift_count,
                split=True,
                overlap=engine["overlap"],
                segment=segment,
                progress=False,
            )[0]

    def replicated() -> Any:
        random.seed(SEED)
        with th.no_grad():
            return _apply_model_with_progress(
                engine["model"],
                th,
                tensor,
                device=engine["device"],
                shifts=shift_count,
                overlap=engine["overlap"],
                segment=segment,
                progress_callback=seen.append,
            )[0]

    expected = reference()
    actual = replicated()
    # Exact equality, not a tolerance: a tolerance would hide the drift this
    # test exists to catch.
    assert th.equal(expected, actual), "replication changed the separated audio"
    return seen


def _assert_progress_advances(seen: list[float]) -> None:
    assert seen, "the replication reported no progress at all"
    assert seen == sorted(seen), f"progress went backwards: {seen}"
    assert len(set(seen)) == len(seen), f"progress repeated a value: {seen}"
    assert seen[-1] == 1.0, f"progress must finish at exactly 1.0: {seen}"


def test_replication_matches_production_settings_and_scales_per_pass(engine: Any) -> None:
    """Production path: segment from the model (BagOfModels), default shifts."""
    # Guards against a vacuously passing test: if the replication were disabled
    # (drift fallback), both calls would run apply_model and compare equal for
    # the wrong reason.
    assert _apply_path_matches_installed() is True, (
        "demucs' chunk math no longer matches the replication; the progress path "
        "is falling back (safe, but the bar will look frozen again)"
    )
    assert engine["shifts"] > 1, "the default preset averages shift passes"

    seen = _assert_replication_matches(engine, _clip(CLIP_SECONDS), segment=None)

    _assert_progress_advances(seen)
    # One report per finished chunk per pass; a pass boundary must not finish
    # early, so the reports only reach 1.0 on the last pass.
    assert len(seen) >= engine["shifts"], f"expected a report per pass, got {seen}"


def test_replication_matches_with_multiple_chunks_and_a_short_tail(engine: Any) -> None:
    """The split pass must blend chunks — including a very short tail — exactly."""
    profile = engine["profile"]
    waveform = _clip(MULTI_CHUNK_SECONDS)
    segment_length = int(profile.sample_rate * profile.chunk_length_seconds) if profile.chunk_length_seconds else int(profile.sample_rate * 7.8)
    offsets = _chunk_offsets(waveform.shape[-1], segment_length, engine["overlap"])
    assert len(offsets) == 2, f"expected two chunks for this fixture, got {offsets}"
    # Pin the intent: the final chunk must be shorter than a full segment,
    # otherwise this test silently stops covering the tail case.
    assert waveform.shape[-1] - offsets[-1] < segment_length

    # shifts=0 exercises the plain split pass: one report per chunk, so the
    # count is exact and the bar plainly moves chunk by chunk.
    seen = _assert_replication_matches(engine, waveform, segment=None, shifts=0)

    _assert_progress_advances(seen)
    assert len(seen) == len(offsets)
    assert seen == [pytest.approx((done + 1) / len(offsets)) for done in range(len(offsets))]
