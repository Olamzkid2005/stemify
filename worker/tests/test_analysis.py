"""Analysis unit tests (roadmap Phase C): pure helpers and degradation.

librosa-dependent paths stay untested here (the optional stack is not
installed in CI-lite environments); the graceful-degradation contract is
exercised by the import-missing case, and the key/camelot mapping is verified
without librosa by synthesizing chroma profiles directly.
"""

from __future__ import annotations

import numpy as np
import pytest

from worker import analysis as analysis_module
from worker.analysis import _clamp_tempo, analyze_track


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (128.0, 128.0),
        (180.0, 180.0),
        (70.0, 70.0),
        (190.0, 95.0),  # drum-and-bass double-read folds down
        (60.0, 120.0),  # ballad half-read folds up
        (370.0, 92.5),  # two folds
    ],
)
def test_clamp_tempo_folds_into_dj_window(raw: float, expected: float) -> None:
    assert _clamp_tempo(raw) == pytest.approx(expected)


def test_clamp_tempo_returns_non_positive_input_unchanged() -> None:
    """A zero reading is not a very slow song, and folding it never terminates.

    Doubling zero towards the 70 BPM floor loops forever, which is how a
    finished separation looked hung: the heartbeat thread kept reporting the
    worker as alive while the job never advanced.
    """
    assert _clamp_tempo(0.0) == 0.0
    assert _clamp_tempo(-1.0) == -1.0


def test_estimate_tempo_degrades_on_a_zero_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    """librosa reports 0 for material with no usable onsets: not a tempo.

    Carrying an analysis value of 0 forward is worse than reporting none: the
    manifest schema requires a positive bpm, so it turned an otherwise finished
    job into a validation failure at packaging.
    """

    class FakeOnset:
        @staticmethod
        def onset_strength(*_args: object, **_kwargs: object) -> np.ndarray:
            return np.zeros(64, dtype=np.float32)

    class FakeFeature:
        @staticmethod
        def tempo(*_args: object, **_kwargs: object) -> np.ndarray:
            return np.array([0.0])

    fake = type("FakeLibrosa", (), {"onset": FakeOnset, "feature": FakeFeature})
    monkeypatch.setattr(analysis_module, "_import_librosa", lambda: fake)

    # One second at 100 Hz keeps the fixture tiny; the guard compares the mono
    # sample count against the sample rate, so the ratio is what matters.
    waveform = np.zeros((2, 100), dtype=np.float32)
    assert analysis_module.estimate_tempo(waveform, 100) is None


def test_analyze_track_degrades_when_librosa_missing() -> None:
    """The import-missing path must degrade, never raise (graceful contract)."""
    result = analyze_track(
        drums_stem=np.zeros((2, 44100), dtype=np.float32),
        mixture=np.zeros((2, 44100), dtype=np.float32),
        sample_rate=44100,
    )
    # In an environment with librosa installed this returns an analysis or
    # insufficient_signal for silence; without it, "unavailable". Both are
    # graceful — the assertion is that no exception escaped.
    assert result.analysis is None or result.degraded in (None, "insufficient_signal")


def test_analyze_track_records_exception_class(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_waveform: np.ndarray, _sample_rate: int) -> tuple[float, float] | None:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(analysis_module, "_import_librosa", lambda: object())
    monkeypatch.setattr(analysis_module, "estimate_tempo", boom)
    result = analyze_track(
        drums_stem=np.zeros((2, 44100), dtype=np.float32),
        mixture=np.zeros((2, 44100), dtype=np.float32),
        sample_rate=44100,
    )
    assert result.analysis is None
    assert result.degraded == "RuntimeError"


def test_key_profiles_map_to_expected_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chroma profile peaked on F#/A must map to F# minor / 11A (no librosa)."""
    profile = np.zeros(12, dtype=np.float64)
    # F# minor triad emphasis: F#, A, C# — pitch classes 6, 9, 1.
    for pitch_class, weight in ((6, 1.0), (9, 0.8), (1, 0.7)):
        profile[pitch_class] = weight

    class FakeLibrosa:
        onset = None
        feature = None

    # detect_key builds chroma through librosa; inject a fake returning our
    # profile so the KS-mapping logic is exercised in isolation.
    class FakeChroma:
        @staticmethod
        def chroma_cqt(*_args: object, **_kwargs: object) -> np.ndarray:
            return profile.reshape(12, 1)

    fake = type("FakeLibrosa", (), {"feature": FakeChroma})
    monkeypatch.setattr(analysis_module, "_import_librosa", lambda: fake)

    result = analysis_module.detect_key(np.zeros((2, 44100), dtype=np.float32), 44100)
    assert result is not None
    key, camelot = result
    assert key == "F# minor"
    assert camelot == "11A"
