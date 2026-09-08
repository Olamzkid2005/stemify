"""Task 13 tests: worker heartbeat liveness row (plan Section 19.2)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from worker.database import JobQueue


@pytest.fixture()
def queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> JobQueue:
    monkeypatch.setenv("STEMIFY_DATA_DIR", str(tmp_path / "data"))
    return JobQueue(data_dir=str(tmp_path / "data"))


def test_heartbeat_row_is_absent_before_first_write(queue: JobQueue) -> None:
    assert queue.heartbeat_age_ms() is None


def test_write_heartbeat_upserts_single_row(queue: JobQueue) -> None:
    queue.write_heartbeat()
    first_age = queue.heartbeat_age_ms()
    assert first_age is not None and first_age < 1000

    time.sleep(0.01)
    queue.write_heartbeat()
    rows = queue._connection.execute(
        "SELECT COUNT(*) FROM worker_heartbeat"
    ).fetchone()[0]
    assert rows == 1  # upsert, never a second row
    assert queue.heartbeat_age_ms() >= 0


def test_heartbeat_age_grows_with_clock(queue: JobQueue) -> None:
    queue.write_heartbeat()
    time.sleep(0.02)
    age = queue.heartbeat_age_ms()
    assert age is not None and age >= 15
