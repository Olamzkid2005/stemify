"""Thread budget tests (concurrency plan 3.4, milestone C2).

`STEMIFY_WORKER_THREADS` is exported by the launcher as `cores // pool`, and the
worker turns it into the numeric stack's thread limits before torch is imported.
These cover the resolution rules; that the limit actually reaches torch is a
manual check (documented in docs/BENCHMARKS.md) because a test process has
usually imported torch already.
"""

from __future__ import annotations

import os

import pytest

from worker.job_loop import _THREAD_ENV_VARS, _apply_thread_budget, _thread_budget


@pytest.fixture()
def clean_thread_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No budget and no inherited thread limits, whatever the machine has set."""
    monkeypatch.delenv("STEMIFY_WORKER_THREADS", raising=False)
    for variable in _THREAD_ENV_VARS:
        monkeypatch.delenv(variable, raising=False)


def test_no_budget_env_means_defaults_are_left_alone(clean_thread_env: None) -> None:
    assert _thread_budget() is None
    assert _apply_thread_budget() is None
    for variable in _THREAD_ENV_VARS:
        assert os.environ.get(variable) is None


@pytest.mark.parametrize("value", ["", "0", "+0", "-1", "two", "2.5", "1e3"])
def test_invalid_budgets_are_ignored_rather_than_forced_to_one(
    clean_thread_env: None, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A typo must not silently pin the worker to a single thread."""
    monkeypatch.setenv("STEMIFY_WORKER_THREADS", value)
    assert _thread_budget() is None
    assert _apply_thread_budget() is None
    for variable in _THREAD_ENV_VARS:
        assert os.environ.get(variable) is None


def test_valid_budget_is_applied_to_every_thread_limit(
    clean_thread_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STEMIFY_WORKER_THREADS", "2")
    assert _thread_budget() == 2
    assert _apply_thread_budget() == 2
    for variable in _THREAD_ENV_VARS:
        assert os.environ[variable] == "2"


def test_one_thread_is_a_valid_budget(clean_thread_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pool size >= cores legitimately asks for a single-threaded worker."""
    monkeypatch.setenv("STEMIFY_WORKER_THREADS", "1")
    assert _apply_thread_budget() == 1
    assert os.environ["OMP_NUM_THREADS"] == "1"


def test_an_explicit_library_limit_wins(
    clean_thread_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator who set OMP_NUM_THREADS directly keeps it."""
    monkeypatch.setenv("STEMIFY_WORKER_THREADS", "4")
    monkeypatch.setenv("OMP_NUM_THREADS", "3")
    assert _apply_thread_budget() == 4
    assert os.environ["OMP_NUM_THREADS"] == "3"
    assert os.environ["MKL_NUM_THREADS"] == "4"
