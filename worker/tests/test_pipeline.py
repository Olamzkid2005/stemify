"""Worker tests for the shared stage definitions and pipeline progress.

Progress is user-visible: the separation stage used to emit only its 30 and 75
boundaries because the model adapter's callback was dropped, so the bar sat at
30 for the whole inference. These tests pin the mapping and the pass-through.
"""

from pathlib import Path
from typing import Any

import pytest

from worker.errors import ErrorCode
from worker.pipeline import (
    PIPELINE_STAGES,
    SEPARATION_END_PCT,
    SEPARATION_START_PCT,
    _encode_progress,
    _separation_progress,
    run_separation_stage,
)
from worker.stages import TERMINAL_STATUSES, Stage, Status


def test_pipeline_stages_are_defined_in_order() -> None:
    assert PIPELINE_STAGES == (
        Stage.STARTING,
        Stage.DOWNLOADING,
        Stage.VALIDATING,
        Stage.PREPARING_AUDIO,
        Stage.SEPARATING,
        Stage.ENCODING,
        Stage.PACKAGING,
        Stage.CLEANUP,
    )


def test_only_terminal_statuses_are_terminal() -> None:
    assert TERMINAL_STATUSES == frozenset(
        {Status.COMPLETED, Status.FAILED, Status.CANCELED, Status.EXPIRED}
    )
    assert Status.QUEUED not in TERMINAL_STATUSES
    assert ErrorCode.UNKNOWN  # error codes are importable and non-empty


def test_separation_progress_spans_its_window_without_touching_boundaries() -> None:
    """The model's 0-1 fraction maps strictly inside the 30-75 stage window."""
    assert _separation_progress(0.0) == SEPARATION_START_PCT + 1
    assert _separation_progress(1.0) == SEPARATION_END_PCT - 1
    # Out-of-range input is clamped rather than escaping the window.
    assert _separation_progress(-5.0) == SEPARATION_START_PCT + 1
    assert _separation_progress(2.0) == SEPARATION_END_PCT - 1
    # Monotonic across the range, so the bar never moves backwards.
    values = [_separation_progress(index / 100) for index in range(101)]
    assert values == sorted(values)
    assert len(set(values)) > 20  # visibly moving, not a couple of steps


def test_encode_progress_spans_75_to_90() -> None:
    assert _encode_progress(0, 3) == 75
    assert _encode_progress(1, 3) == 80
    assert _encode_progress(3, 3) == 90
    assert _encode_progress(5, 3) == 90  # clamped
    assert _encode_progress(1, 0) == 90  # no division by zero


def _fake_separate(
    fractions: list[float], calls: dict[str, Any] | None = None
) -> Any:
    """A stand-in adapter: records the callback, then reports set fractions."""

    def separate(
        waveform: Any,
        profile: Any = None,
        mode: str = "vocals_instrumental",
        progress_callback: Any = None,
        cancellation_checker: Any = None,
        quality: str | None = None,
        stem_selection: Any = None,
    ) -> dict[str, Any]:
        if calls is not None:
            calls["progress_callback"] = progress_callback
        for fraction in fractions:
            progress_callback(fraction)
        return {"vocals": object()}, None

    return separate


def _install_stage_doubles(
    monkeypatch: pytest.MonkeyPatch,
    fractions: list[float],
) -> dict[str, Any]:
    """Patch decoding and both adapters; capture what the pipeline emits."""
    import worker.pipeline as pipeline_module
    from worker import input_audio
    from worker.models import drumsep

    monkeypatch.setattr(input_audio, "decode_to_waveform", lambda path: (object(), None))

    calls: dict[str, Any] = {}
    fake = _fake_separate(fractions, calls)
    monkeypatch.setattr(pipeline_module, "separate", fake)
    monkeypatch.setattr(drumsep, "separate", fake)
    return calls


@pytest.mark.parametrize("mode", ["vocals_instrumental", "full_stems", "drum_breakdown"])
def test_separation_stage_emits_moving_progress_in_every_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    seen: list[tuple[Stage, int]] = []
    calls = _install_stage_doubles(monkeypatch, [0.0, 0.25, 0.5, 0.75, 1.0])

    run_separation_stage(
        tmp_path / "canonical.wav",
        tmp_path,
        mode,
        progress_callback=lambda stage, progress: seen.append((stage, progress)),
    )

    # The adapter must receive a callback at all (the original bug passed None,
    # so nothing moved between 30 and 75).
    assert calls["progress_callback"] is not None

    assert {stage for stage, _ in seen} == {Stage.SEPARATING}
    percents = [progress for _, progress in seen]
    assert percents[0] == SEPARATION_START_PCT
    assert percents[-1] == SEPARATION_END_PCT
    assert percents == sorted(percents)
    # Every chunk report lands inside the window, each on its own percent here.
    interior = percents[1:-1]
    assert all(SEPARATION_START_PCT < value < SEPARATION_END_PCT for value in interior)
    assert len(set(interior)) == len(interior) == 5


def test_separation_stage_drops_repeated_percentages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chunks that round to the same percent must not each write a DB row."""
    fractions = [0.0, 0.01, 0.5, 0.51, 1.0]
    _install_stage_doubles(monkeypatch, fractions)
    seen: list[int] = []

    run_separation_stage(
        tmp_path / "canonical.wav",
        tmp_path,
        "vocals_instrumental",
        progress_callback=lambda stage, progress: seen.append(progress),
    )

    # 0.0/0.01 both map to 31 and 0.5/0.51 both map to 52, so each is emitted
    # once; the 30 and 75 boundaries always fire.
    assert [round(_separation_progress(f)) for f in fractions] == [31, 31, 52, 52, 74]
    assert seen == [SEPARATION_START_PCT, 31, 52, 74, SEPARATION_END_PCT]
