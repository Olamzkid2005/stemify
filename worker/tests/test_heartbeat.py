"""Task 13 tests: worker heartbeat liveness row (plan Section 19.2).

Plus the per-worker rows a parallel pool needs (concurrency plan 3.3): the
single heartbeat row answers "is any worker up", `workers` answers "which
worker owns this job, and is it still alive".
"""

from __future__ import annotations

import os
import sqlite3
import threading
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


def _worker_rows(queue: JobQueue) -> list[tuple]:
    connection = sqlite3.connect(queue.database_path)
    try:
        return connection.execute(
            "SELECT worker_call_id, pid, started_at, updated_at FROM workers"
        ).fetchall()
    finally:
        connection.close()


def test_register_worker_publishes_one_identifiable_row(queue: JobQueue) -> None:
    assert _worker_rows(queue) == []
    queue.register_worker()

    rows = _worker_rows(queue)
    assert len(rows) == 1
    worker_call_id, pid, started_at, updated_at = rows[0]
    assert worker_call_id == queue.worker_call_id
    assert pid == os.getpid()
    assert started_at <= updated_at

    # Idempotent: registering twice is one row, not two (a restart reuses it).
    queue.register_worker()
    assert len(_worker_rows(queue)) == 1


def test_heartbeat_refreshes_this_workers_row_only(queue: JobQueue) -> None:
    other = JobQueue(data_dir=str(queue.data_dir))
    queue.register_worker()
    other.register_worker()
    try:
        before = {row[0]: row[3] for row in _worker_rows(queue)}
        time.sleep(0.02)
        queue.write_heartbeat()
        after = {row[0]: row[3] for row in _worker_rows(queue)}

        assert len(after) == 2  # each worker keeps its own row
        assert after[queue.worker_call_id] > before[queue.worker_call_id]
        assert after[other.worker_call_id] == before[other.worker_call_id]
    finally:
        other.close()


def test_heartbeat_alone_registers_a_worker_without_a_prior_register(queue: JobQueue) -> None:
    """A queue used outside run() must never leave a NULL owner behind."""
    queue.write_heartbeat()
    rows = _worker_rows(queue)
    assert [row[0] for row in rows] == [queue.worker_call_id]


def test_unregister_worker_removes_only_its_own_row(queue: JobQueue) -> None:
    other = JobQueue(data_dir=str(queue.data_dir))
    queue.register_worker()
    other.register_worker()
    try:
        queue.unregister_worker()
        assert [row[0] for row in _worker_rows(queue)] == [other.worker_call_id]
    finally:
        other.close()


def _claim_job_as_a_dead_worker(queue: JobQueue) -> str:
    """Leave one job processing, owned by a worker whose heartbeat went stale."""
    from test_queue import insert_queued_job

    insert_queued_job(queue.database_path)
    queue.register_worker()
    job = queue.claim_next_queued_job()
    assert job is not None
    from worker.database import WORKER_STALE_MS

    connection = sqlite3.connect(queue.database_path)
    try:
        connection.execute(
            "UPDATE workers SET updated_at = ? WHERE worker_call_id = ?",
            (queue._worker_started_at - WORKER_STALE_MS - 1000, queue.worker_call_id),
        )
        connection.commit()
    finally:
        connection.close()
    return job.id


def _status(queue: JobQueue, job_id: str) -> str:
    connection = sqlite3.connect(queue.database_path)
    try:
        return connection.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    finally:
        connection.close()


def test_heartbeat_tick_recovers_a_dead_peers_job(queue: JobQueue) -> None:
    """Concurrency plan 3.3: a peer's death is cleaned up on the tick, not a restart."""
    from worker import job_loop

    peer = JobQueue(data_dir=str(queue.data_dir))
    job_id = _claim_job_as_a_dead_worker(peer)
    assert _status(queue, job_id) == "processing"

    original_interval = job_loop.HEARTBEAT_INTERVAL_MS
    job_loop.HEARTBEAT_INTERVAL_MS = 20
    stop = threading.Event()
    ticker = threading.Thread(target=job_loop._heartbeat_loop, args=(queue, stop), daemon=True)
    try:
        ticker.start()
        deadline = time.monotonic() + 5
        while _status(queue, job_id) == "processing" and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        stop.set()
        ticker.join(timeout=1.0)
        job_loop.HEARTBEAT_INTERVAL_MS = original_interval
        peer.close()

    assert _status(queue, job_id) == "failed"
    # The tick also proved this worker alive, and pruned the row it replaced.
    assert [row[0] for row in _worker_rows(queue)] == [queue.worker_call_id]


def test_run_registers_this_worker_then_releases_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run()` publishes liveness before recovering, and leaves nothing behind."""
    from worker import job_loop

    data_dir = tmp_path / "data"
    monkeypatch.setenv("STEMIFY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("STEMIFY_WORKER_POLL_MS", "10")
    monkeypatch.setattr(job_loop, "HEARTBEAT_INTERVAL_MS", 50)

    peer = JobQueue(data_dir=str(data_dir))
    job_id = _claim_job_as_a_dead_worker(peer)

    job_loop.run(stop_after_iterations=1)

    # The dead peer's job was recovered at startup, so it is never re-run...
    assert _status(peer, job_id) == "failed"
    # ...and the worker that just stopped unregistered itself, so a restart
    # never waits out the staleness window to recover its own jobs.
    assert _worker_rows(peer) == []
    peer.close()
