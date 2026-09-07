"""Tests for the local worker queue (atomic claim, guarded failure, stale recovery).

Uses a temporary data directory and real SQLite files; no ffmpeg needed, so the
suite runs anywhere.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from worker.database import JobQueue
from worker.errors import ErrorCode


def make_queue(tmp_path: Path) -> JobQueue:
    return JobQueue(data_dir=str(tmp_path))


def insert_queued_job(db_path: Path, job_id: str = "job_" + "a" * 32) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT INTO jobs (id, owner_key, source_type, source_object_key, mode, output_format) "
            "VALUES (?, 'owner_test', 'upload', 'sources/upl_test/song.mp3', "
            "'vocals_instrumental', 'mp3')",
            (job_id,),
        )
        connection.commit()
    finally:
        connection.close()


def read_job_row(db_path: Path, job_id: str, columns: str) -> tuple:
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            f"SELECT {columns} FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    finally:
        connection.close()


def test_claim_moves_job_to_processing_exactly_once(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    insert_queued_job(queue.database_path)

    job = queue.claim_next_queued_job()
    assert job is not None
    assert job.source_type == "upload"
    assert job.source_object_key == "sources/upl_test/song.mp3"

    # Second claim on the same database must find nothing: the row is processing.
    other = JobQueue(data_dir=str(tmp_path))
    try:
        assert other.claim_next_queued_job() is None
    finally:
        other.close()

    row = read_job_row(
        queue.database_path, job.id, "status, stage, progress, worker_call_id"
    )
    assert row == ("processing", "starting", 5, queue.worker_call_id)
    queue.close()


def test_concurrent_claims_cannot_grab_the_same_job(tmp_path: Path) -> None:
    queue_a = make_queue(tmp_path)
    queue_b = make_queue(tmp_path)
    insert_queued_job(queue_a.database_path)

    results: list[JobQueue.claim_next_queued_job | None] = []

    def claim(queue: JobQueue) -> None:
        results.append(queue.claim_next_queued_job())

    threads = [threading.Thread(target=claim, args=(q,)) for q in (queue_a, queue_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    claimed = [r for r in results if r is not None]
    assert len(claimed) == 1, "two loops must never process one job twice"
    queue_a.close()
    queue_b.close()


def test_fail_job_sets_terminal_state_with_public_message(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    insert_queued_job(queue.database_path)
    job = queue.claim_next_queued_job()
    assert job is not None

    queue.fail_job(job.id, ErrorCode.INVALID_AUDIO, "This file does not look like playable audio.")

    row = read_job_row(
        queue.database_path,
        job.id,
        "status, error_code, error_message_public, diagnostic_reference, completed_at",
    )
    assert row[0] == "failed"
    assert row[1] == ErrorCode.INVALID_AUDIO.value
    assert row[2].startswith("This file")
    assert row[3] and row[3].startswith("diag_")
    assert row[4] is not None
    queue.close()


def test_fail_job_never_moves_a_terminal_state_backwards(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    insert_queued_job(queue.database_path)
    job = queue.claim_next_queued_job()
    assert job is not None
    queue.fail_job(job.id, ErrorCode.UNKNOWN, "first failure")

    # A late/second failure attempt for the same job must be a no-op.
    queue.fail_job(job.id, ErrorCode.INVALID_AUDIO, "second failure")

    row = read_job_row(queue.database_path, job.id, "error_code, error_message_public")
    assert (row[0], row[1]) == (ErrorCode.UNKNOWN.value, "first failure")
    queue.close()


def test_recover_stale_processing_jobs_fails_leftovers_at_startup(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    insert_queued_job(queue.database_path)

    # Simulate a crashed previous run: flip the row to processing behind the queue's back.
    crash_connection = sqlite3.connect(queue.database_path)
    try:
        crash_connection.execute(
            "UPDATE jobs SET status = 'processing' WHERE id = ?",
            ("job_" + "a" * 32,),
        )
        crash_connection.commit()
    finally:
        crash_connection.close()

    recovered = queue.recover_stale_processing_jobs()
    assert recovered == 1
    row = read_job_row(queue.database_path, "job_" + "a" * 32, "status, error_code")
    assert row == ("failed", ErrorCode.UNKNOWN.value)
    queue.close()


def test_pipeline_failure_lands_in_failed_status(tmp_path: Path) -> None:
    """End-to-end: a claimed upload whose source file is missing fails safely."""
    from worker.job_loop import process_job

    queue = make_queue(tmp_path)
    insert_queued_job(queue.database_path)
    job = queue.claim_next_queued_job()
    assert job is not None

    # source_object_key points at a file that was never written.
    process_job(queue, job)

    row = read_job_row(queue.database_path, job.id, "status, error_code")
    assert row[0] == "failed"
    assert row[1] in {ErrorCode.INVALID_AUDIO.value, ErrorCode.UNKNOWN.value}
    assert not queue.claim_next_queued_job(), "failed job must not be re-claimed"
    queue.close()
