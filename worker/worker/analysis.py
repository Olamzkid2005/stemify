"""Track analysis (roadmap Phase C): BPM and musical key after separation.

Runs on the separated stems: tempo estimation is measurably more accurate on
the isolated drums stem, and key estimation uses the full mixture (all tonal
instruments present).

Dependency policy: librosa is an OPTIONAL dependency (own group in
requirements.txt, mirroring yt-dlp/psutil). Every failure path degrades
gracefully — analysis never fails a completed separation; callers receive None
fields plus a `degraded` reason instead of an exception.

Swappability (decision D3): DeepRhythm was the preferred tempo estimator but is
AGPL-3.0, so the default is librosa's multi-feature tempo estimate. The public
functions here are estimator-agnostic; swapping in another engine must not
require changes outside this module.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Krumhansl-Schmuckler key profiles (major, minor), from the classic 1990
# "Cognitive Foundations of Musical Pitch" measurements.
_KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
_PITCH_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# Camelot wheel: C=8B, ascending fifths. Index = pitch class of the MAJOR key
# tonic (8B=C major ... 1B=B major). Minor keys take their RELATIVE major's
# number with letter A (8A=A minor, 11A=F# minor): index = minor tonic, value
# = major table at (tonic + 3) semitones.
_CAMELOT_MAJOR = ["8B", "3B", "10B", "5B", "12B", "7B", "2B", "9B", "4B", "11B", "6B", "1B"]
_CAMELOT_MINOR = ["5A", "12A", "7A", "2A", "9A", "4A", "11A", "6A", "1A", "8A", "3A", "10A"]


@dataclass(frozen=True)
class TrackAnalysis:
    """Analysis values safe to embed in the output manifest."""

    bpm: float
    key: str
    camelot: str


@dataclass(frozen=True)
class AnalysisResult:
    analysis: TrackAnalysis | None
    # Set when analysis could not run: "unavailable" (librosa missing) or the
    # exception class name. Never raised — recorded for diagnostics.
    degraded: str | None = None


def estimate_tempo(stereo_waveform: np.ndarray, sample_rate: int) -> tuple[float, float] | None:
    """Estimate BPM from a (channels, samples) float32 waveform.

    Returns (bpm, confidence) or None when the signal is unusable. Confidence
    here is librosa's ratio between the two strongest tempo candidates — a
    heuristic, good enough to flag ambiguous material.
    """
    librosa = _import_librosa()
    if librosa is None:
        return None
    mono = _mono(stereo_waveform)
    if mono.size < sample_rate:  # under a second of audio: tempo is meaningless
        return None
    onset_env = librosa.onset.onset_strength(y=mono, sr=sample_rate)
    tempo = librosa.feature.tempo(onset_envelope=onset_env, sr=sample_rate, aggregate=None)
    if tempo.size == 0 or not np.isfinite(tempo).all():
        return None
    primary = float(tempo[0])
    secondary = float(tempo[1]) if tempo.size > 1 else primary
    confidence = min(primary, secondary) / max(primary, secondary) if max(primary, secondary) > 0 else 0.0
    return _clamp_tempo(primary), float(min(1.0, max(0.0, confidence)))


def detect_key(stereo_waveform: np.ndarray, sample_rate: int) -> tuple[str, str] | None:
    """Detect the musical key via chroma + Krumhansl-Schmuckler correlation.

    Returns (key, camelot), e.g. ("F# minor", "11A"), or None when the signal
    is unusable. Uses the full mixture: all tonal content present.
    """
    librosa = _import_librosa()
    if librosa is None:
        return None
    mono = _mono(stereo_waveform)
    if mono.size < sample_rate // 2:
        return None
    chroma = librosa.feature.chroma_cqt(y=mono, sr=sample_rate)
    profile = chroma.mean(axis=1)
    if not np.isfinite(profile).all() or float(np.abs(profile).max()) <= 0:
        return None
    profile = profile / float(np.abs(profile).max())

    best: tuple[float, int, str] | None = None
    for rotation in range(12):
        rotated = np.roll(profile, -rotation)
        for quality, ks_profile in (("major", _KS_MAJOR), ("minor", _KS_MINOR)):
            score = float(np.corrcoef(rotated, ks_profile)[0, 1])
            if np.isnan(score):
                continue
            if best is None or score > best[0]:
                best = (score, rotation, quality)
    if best is None:
        return None
    _, rotation, quality = best
    tonic = _PITCH_CLASSES[rotation]
    camelot = (_CAMELOT_MAJOR if quality == "major" else _CAMELOT_MINOR)[rotation]
    return f"{tonic} {quality}", camelot


def analyze_track(
    drums_stem: np.ndarray | None,
    mixture: np.ndarray,
    sample_rate: int,
) -> AnalysisResult:
    """Analyze one separated track. Never raises: failures degrade to None."""
    try:
        librosa = _import_librosa()
        if librosa is None:
            return AnalysisResult(analysis=None, degraded="unavailable")

        tempo_source = drums_stem if drums_stem is not None else mixture
        tempo_result = estimate_tempo(tempo_source, sample_rate)
        key_result = detect_key(mixture, sample_rate)
        if tempo_result is None or key_result is None:
            return AnalysisResult(analysis=None, degraded="insufficient_signal")
        (bpm, _confidence), (key, camelot) = tempo_result, key_result
        return AnalysisResult(
            analysis=TrackAnalysis(bpm=round(bpm, 1), key=key, camelot=camelot)
        )
    except Exception as error:  # noqa: BLE001 - analysis must never fail a job
        return AnalysisResult(analysis=None, degraded=type(error).__name__)


def _clamp_tempo(bpm: float) -> float:
    """Fold extreme tempo estimates into the 70-180 window DJs expect.

    Halves above 180 and doubles below 70 once; boundary values pass through
    unchanged. A 190 BPM drum-and-bass read becomes 95; a 60 BPM ballad read
    becomes 120 only when strictly below the floor.
    """
    value = float(bpm)
    while value > 180.0:
        value /= 2.0
    while value < 70.0:
        value *= 2.0
    return value


def _mono(stereo_waveform: np.ndarray) -> np.ndarray:
    if stereo_waveform.ndim == 1:
        return stereo_waveform.astype(np.float32, copy=False)
    return stereo_waveform.mean(axis=0).astype(np.float32, copy=False)


def _import_librosa() -> object | None:
    try:
        import librosa
    except ImportError:
        return None
    return librosa


__all__ = ["AnalysisResult", "TrackAnalysis", "analyze_track", "detect_key", "estimate_tempo"]
