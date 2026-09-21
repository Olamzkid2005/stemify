"""Tests for the local worker queue (atomic claim, guarded failure, stale recovery).

Uses a temporary data directory and real SQLite files; no ffmpeg needed, so the
suite runs anywhere.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from worker.database import JobQueue
from worker.errors import ErrorCode
from worker.stages import Stage


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


def test_progress_detail_is_persisted_as_a_safe_event(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    insert_queued_job(queue.database_path)
    job = queue.claim_next_queued_job()
    assert job is not None

    assert queue.update_progress(
        job.id,
        Stage.DOWNLOADING,
        12,
        detail="Downloading audio as MP3",
    )

    row = read_job_row(
        queue.database_path,
        job.id,
        "stage, progress",
    )
    assert row == ("downloading", 12)
    event = read_job_row(
        queue.database_path,
        job.id,
        "(SELECT detail FROM job_events WHERE job_id = jobs.id ORDER BY created_at DESC LIMIT 1)",
    )
    assert event == ("Downloading audio as MP3",)
    queue.close()


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


def test_recovery_leaves_a_live_peers_processing_job_alone(tmp_path: Path) -> None:
    """Concurrency plan 3.3: ownership, not startup, decides what is orphaned.

    Without this, a second worker starting up fails the job the first one is
    running — which is what makes a parallel pool impossible today.
    """
    boss = make_queue(tmp_path)
    insert_queued_job(boss.database_path)
    peer = JobQueue(data_dir=str(tmp_path))
    peer.register_worker()
    claimed = peer.claim_next_queued_job()
    assert claimed is not None and claimed.id == "job_" + "a" * 32
    peer.write_heartbeat()

    # A third process starts up and runs recovery while the peer is working.
    starting = JobQueue(data_dir=str(tmp_path))
    try:
        assert starting.recover_stale_processing_jobs() == 0
        assert read_job_row(boss.database_path, claimed.id, "status")[0] == "processing"
        # And the peer's registration survived: recovery prunes only stale rows.
        assert read_job_row(
            boss.database_path, claimed.id, "worker_call_id"
        )[0] == peer.worker_call_id
    finally:
        starting.close()
        peer.close()
        boss.close()


def test_recovery_fails_a_job_whose_owner_went_stale(tmp_path: Path) -> None:
    """A crashed worker's job is failed within the staleness window."""
    from worker.database import WORKER_STALE_MS

    boss = make_queue(tmp_path)
    insert_queued_job(boss.database_path)
    crashed = JobQueue(data_dir=str(tmp_path))
    crashed.register_worker()
    claimed = crashed.claim_next_queued_job()
    assert claimed is not None

    # The owner died mid-job; its last heartbeat is now past the window.
    stale = crashed._worker_started_at - WORKER_STALE_MS - 1000
    connection = sqlite3.connect(crashed.database_path)
    try:
        connection.execute(
            "UPDATE workers SET updated_at = ? WHERE worker_call_id = ?",
            (stale, crashed.worker_call_id),
        )
        connection.commit()
    finally:
        connection.close()

    starting = JobQueue(data_dir=str(tmp_path))
    try:
        assert starting.recover_stale_processing_jobs() == 1
        assert read_job_row(
            boss.database_path, claimed.id, "status, error_code"
        ) == ("failed", ErrorCode.UNKNOWN.value)
        # The dead worker's row is pruned, so the table does not grow per crash.
        connection = sqlite3.connect(boss.database_path)
        try:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM workers WHERE worker_call_id = ?",
                (crashed.worker_call_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        assert remaining == 0
    finally:
        starting.close()
        crashed.close()
        boss.close()


def test_recovery_fails_a_legacy_row_with_no_owner(tmp_path: Path) -> None:
    """Rows from before worker_call_id was recorded still recover."""
    queue = make_queue(tmp_path)
    insert_queued_job(queue.database_path)
    claimed = queue.claim_next_queued_job()
    assert claimed is not None

    connection = sqlite3.connect(queue.database_path)
    try:
        connection.execute(
            "UPDATE jobs SET worker_call_id = NULL WHERE id = ?", (claimed.id,)
        )
        connection.commit()
    finally:
        connection.close()

    assert queue.recover_stale_processing_jobs() == 1
    assert read_job_row(
        queue.database_path, claimed.id, "status, error_code"
    ) == ("failed", ErrorCode.UNKNOWN.value)
    queue.close()


def test_progress_writes_are_best_effort_when_the_database_is_locked(tmp_path: Path) -> None:
    """Concurrency plan 2.4: a locked database must not fail the job it reports on.

    State transitions stay strict; only progress may be dropped.
    """
    queue = make_queue(tmp_path)
    insert_queued_job(queue.database_path)
    job = queue.claim_next_queued_job()
    assert job is not None
    # Give up quickly instead of waiting out the real 5s busy timeout.
    queue._connection.execute("PRAGMA busy_timeout = 10")

    blocker = sqlite3.connect(queue.database_path, isolation_level=None, timeout=5.0)
    try:
        blocker.execute("BEGIN IMMEDIATE")  # holds the write lock
        assert queue.update_progress(job.id, Stage.SEPARATING, 42, detail="tick") is False
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    # The job is untouched and the next progress write lands normally.
    assert read_job_row(queue.database_path, job.id, "status")[0] == "processing"
    assert queue.update_progress(job.id, Stage.SEPARATING, 43) is True
    queue.close()


def test_claim_reports_the_lock_that_blocked_it(tmp_path: Path) -> None:
    """A failed BEGIN must not be masked by the ROLLBACK that follows it.

    With another connection holding the write lock, BEGIN IMMEDIATE times out
    and there is no transaction to unwind. Running ROLLBACK regardless raised
    "cannot rollback - no transaction is active", so the log blamed a rollback
    failure instead of the locked database that caused it.
    """
    queue = make_queue(tmp_path)
    insert_queued_job(queue.database_path)
    # Give up quickly instead of waiting out the real 5s busy timeout.
    queue._connection.execute("PRAGMA busy_timeout = 10")

    blocker = sqlite3.connect(queue.database_path, isolation_level=None, timeout=5.0)
    try:
        blocker.execute("BEGIN IMMEDIATE")  # holds the write lock
        with pytest.raises(sqlite3.OperationalError) as caught:
            queue.claim_next_queued_job()
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert "locked" in str(caught.value)
    # The queue is untouched by the failed attempt: the job is still claimable.
    assert queue.claim_next_queued_job() is not None
    queue.close()


def test_discard_unpublished_results_keeps_published_outputs(tmp_path: Path) -> None:
    """Failed jobs must not leave artifacts retention will never collect.

    A Spotify fetch writes its album cover into results/{job_id}/ before
    separation can run, and only a completed job ever receives an expires_at,
    so a failed job's cover would otherwise stay on disk for good.
    """
    queue = make_queue(tmp_path)
    job_id = "job_" + "b" * 32
    insert_queued_job(queue.database_path, job_id)
    results = queue.data_dir / "results" / job_id
    results.mkdir(parents=True)
    (results / "artwork.jpg").write_bytes(b"\xff\xd8\xffjpeg")

    assert queue.discard_unpublished_results(job_id) is True
    assert not results.exists()

    # Published outputs are a different case: they belong to a job the user may
    # still be able to download from, so they stay.
    results.mkdir(parents=True)
    (results / "vocals.mp3").write_bytes(b"audio")
    queue.record_output(
        job_id=job_id,
        stem_key="vocals",
        label="Extracted Vocals",
        relative_path=f"results/{job_id}/vocals.mp3",
        mime_type="audio/mpeg",
        size_bytes=5,
        duration_seconds=1.0,
        sha256="0" * 64,
        expires_at=None,
    )
    assert queue.discard_unpublished_results(job_id) is False
    assert (results / "vocals.mp3").is_file()
    queue.close()


def test_pipeline_failure_lands_in_failed_status(tmp_path: Path) -> None:
    """End-to-end: a claimed upload whose source file is missing fails safely."""
    from worker.job_loop import process_job

    queue = make_queue(tmp_path)
    insert_queued_job(queue.database_path)
    job = queue.claim_next_queued_job()
    assert job is not None

    # A link job writes its cover into results/ during the download, so the
    # directory can exist before anything fails.
    artwork_dir = queue.data_dir / "results" / job.id
    artwork_dir.mkdir(parents=True)
    (artwork_dir / "artwork.jpg").write_bytes(b"\xff\xd8\xff")

    # source_object_key points at a file that was never written.
    process_job(queue, job)

    row = read_job_row(queue.database_path, job.id, "status, error_code")
    assert row[0] == "failed"
    assert row[1] in {ErrorCode.INVALID_AUDIO.value, ErrorCode.UNKNOWN.value}
    assert not queue.claim_next_queued_job(), "failed job must not be re-claimed"
    assert not artwork_dir.exists(), "a failed job must not leave artifacts behind"
    queue.close()
