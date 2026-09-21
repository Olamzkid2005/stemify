"""Product-limit tests (plan Section 9).

The size and duration caps are documented in `.env.example` and exported by
start.sh to both processes. They were read by nothing (the duration cap) or with
a hard-coded literal in the callers (the size cap), which is indistinguishable
from having no limit. These pin the reading, the fallbacks and the per-mode
split; no ffmpeg is needed here — the validation ladder itself is covered in
test_input_audio.py.
"""

from __future__ import annotations

import pytest

import worker.input_audio as mod

ENV_NAMES = ("MAX_UPLOAD_BYTES", "MAX_DURATION_SECONDS", "MAX_FULL_STEMS_DURATION_SECONDS")
DEFAULTS = (100 * 1024 * 1024, 480.0, 360.0)


@pytest.fixture(autouse=True)
def _clean_limit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset the caps so a developer's own shell cannot change the outcome."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_the_documented_limits() -> None:
    assert mod._limits_from_env() == DEFAULTS


def test_limits_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "52428800")
    monkeypatch.setenv("MAX_DURATION_SECONDS", "600")
    monkeypatch.setenv("MAX_FULL_STEMS_DURATION_SECONDS", "120")
    assert mod._limits_from_env() == (52_428_800, 600.0, 120.0)


@pytest.mark.parametrize("value", ["", "   ", "abc", "0", "-1", "inf", "nan", "1e999"])
def test_unusable_values_keep_the_default(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """A typo must not turn a cap off, so every unusable value falls back."""
    for name in ENV_NAMES:
        monkeypatch.setenv(name, value)
    assert mod._limits_from_env() == DEFAULTS


def test_module_constants_are_populated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The constants are assignments of `_limits_from_env()`, so they are real."""
    assert (mod.MAX_FILE_BYTES, mod.MAX_DURATION_SECONDS, mod.FULL_STEMS_MAX_DURATION_SECONDS) == (
        mod.DEFAULT_MAX_FILE_BYTES,
        mod.DEFAULT_MAX_DURATION_SECONDS,
        mod.DEFAULT_FULL_STEMS_MAX_DURATION_SECONDS,
    )


def test_three_stem_split_is_capped_below_the_two_stem_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod, "MAX_DURATION_SECONDS", 480.0)
    monkeypatch.setattr(mod, "FULL_STEMS_MAX_DURATION_SECONDS", 360.0)
    assert mod.max_duration_for_mode("full_stems") == 360.0
    assert mod.max_duration_for_mode("vocals_instrumental") == 480.0
    # Refine consumes a worker-produced stem, never longer than the upload that
    # already passed this ladder, so it takes the general cap.
    assert mod.max_duration_for_mode("drum_breakdown") == 480.0
    assert mod.DEFAULT_FULL_STEMS_MAX_DURATION_SECONDS < mod.DEFAULT_MAX_DURATION_SECONDS
