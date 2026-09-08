"""Task 13 tests: local cleanup and retention (plan Section 19.3).

All steps must be idempotent, never touch data/models/, and never expire
processing jobs (stale recovery owns those).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from worker.cleanup import (
    delete_expired_results,
    delete_expired_uploads,
    delete_orphaned_sources,
    mark_expired_jobs,
    run_cleanup,
)
from worker.database import JobQueue

HOUR_MS = 3_600_000


@pytest.fixture()
def queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> JobQueue:
    monkeypatch.setenv("STEMIFY_DATA_DIR", str(tmp_path / "data"))
    return JobQueue(data_dir=str(tmp_path / "data"))


def insert_job(queue: JobQueue, job_id: str, status: str, expires_at: int | None) -> None:
    queue._connection.execute(
        "INSERT INTO jobs (id, owner_key, source_type, mode, output_format, status, expires_at) "
        "VALUES (?, 'cleanup-owner', 'upload', 'vocals_instrumental', 'mp3', ?, ?)",
        (job_id, status, expires_at),
    )


def insert_upload(queue: JobQueue, upload_id: str, expires_at: int | None, with_file: bool) -> str:
    object_key = f"sources/{upload_id}/song.mp3"
    if with_file:
        target = queue.data_dir / object_key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 8)
    queue._connection.execute(
        "INSERT INTO uploads (id, owner_key, filename, object_key, size_bytes, expires_at) "
        "VALUES (?, 'cleanup-owner', 'song.mp3', ?, 8, ?)",
        (upload_id, object_key, expires_at),
    )
    return object_key


def test_mark_expired_jobs_only_touches_expired_terminal_jobs(queue: JobQueue) -> None:
    now = int(time.time() * 1000)
    insert_job(queue, "job_past_completed", "completed", now - HOUR_MS)
    insert_job(queue, "job_future_completed", "completed", now + HOUR_MS)
    insert_job(queue, "job_no_expiry", "completed", None)
    insert_job(queue, "job_past_processing", "processing", now - HOUR_MS)

    assert mark_expired_jobs(queue, now) == 1
    statuses = dict(
        queue._connection.execute("SELECT id, status FROM jobs").fetchall()
    )
    assert statuses["job_past_completed"] == "expired"
    assert statuses["job_future_completed"] == "completed"
    assert statuses["job_no_expiry"] == "completed"
    # Stale-job recovery owns processing rows; cleanup never expires them.
    assert statuses["job_past_processing"] == "processing"


def test_delete_expired_results_is_idempotent(queue: JobQueue) -> None:
    now = int(time.time() * 1000)
    job_id = "job_resultsgone"
    insert_job(queue, job_id, "expired", now - HOUR_MS)
    results_dir = queue.data_dir / "results" / job_id
    results_dir.mkdir(parents=True)
    (results_dir / "vocals.mp3").write_bytes(b"x" * 16)
    queue._connection.execute(
        "INSERT INTO job_outputs (id, job_id, stem_key, label, relative_path, mime_type) "
        "VALUES ('out_1', ?, 'vocals', 'Vocals', 'results/x/vocals.mp3', 'audio/mpeg')",
        (job_id,),
    )

    assert delete_expired_results(queue, now) == 1
    assert not results_dir.exists()
    assert queue._connection.execute(
        "SELECT COUNT(*) FROM job_outputs WHERE job_id = ?", (job_id,)
    ).fetchone()[0] == 0

    # Second run: nothing left, still no error.
    assert delete_expired_results(queue, now) == 0


def test_delete_expired_uploads_removes_file_and_row(queue: JobQueue) -> None:
    now = int(time.time() * 1000)
    upload_id = "upl_" + "a" * 32
    object_key = insert_upload(queue, upload_id, now - HOUR_MS, with_file=True)

    assert delete_expired_uploads(queue, now) == 1
    assert not (queue.data_dir / object_key).exists()
    assert queue._connection.execute(
        "SELECT COUNT(*) FROM uploads WHERE id = ?", (upload_id,)
    ).fetchone()[0] == 0
    assert delete_expired_uploads(queue, now) == 0


def test_delete_orphaned_sources_spares_referenced_and_unknown(queue: JobQueue) -> None:
    orphan = queue.data_dir / "sources" / ("upl_" + "b" * 32)
    orphan.mkdir(parents=True)
    (orphan / "song.mp3").write_bytes(b"x")
    referenced = queue.data_dir / "sources" / ("upl_" + "c" * 32)
    referenced.mkdir(parents=True)
    (referenced / "song.mp3").write_bytes(b"x")
    queue._connection.execute(
        "INSERT INTO uploads (id, owner_key, filename, object_key, size_bytes, expires_at) "
        "VALUES (?, 'cleanup-owner', 'song.mp3', ?, 8, ?)",
        ("upl_" + "c" * 32, f"sources/{referenced.name}/song.mp3", None),
    )
    unknown = queue.data_dir / "sources" / "not-an-upload-id"
    unknown.mkdir(parents=True)

    assert delete_orphaned_sources(queue) == 1
    assert not orphan.exists()
    assert referenced.exists()
    assert unknown.exists()


def test_cleanup_never_touches_model_checkpoints(queue: JobQueue) -> None:
    now = int(time.time() * 1000)
    checkpoint = queue.data_dir / "models" / "hub" / "checkpoints" / "955717e8-8726e21a.th"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"weights")

    insert_job(queue, "job_expired", "completed", now - HOUR_MS)
    run_cleanup(queue, now)

    assert checkpoint.is_file()


def test_run_cleanup_is_idempotent_and_reports(queue: JobQueue) -> None:
    now = int(time.time() * 1000)
    insert_job(queue, "job_a", "completed", now - HOUR_MS)
    insert_upload(queue, "upl_" + "d" * 32, now - HOUR_MS, with_file=True)

    first = run_cleanup(queue, now)
    assert first.expired_jobs_marked == 1
    assert first.expired_uploads_deleted == 1
    assert first.errors == []

    second = run_cleanup(queue, now)
    assert second.expired_jobs_marked == 0
    assert second.expired_uploads_deleted == 0


def test_complete_job_stamps_retention_window(
    queue: JobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOB_RETENTION_HOURS", "2")
    now = int(time.time() * 1000)
    insert_job(queue, "job_retained", "processing", None)
    queue._connection.execute(
        "INSERT INTO job_outputs (id, job_id, stem_key, label, relative_path, mime_type) "
        "VALUES ('out_r', 'job_retained', 'vocals', 'Vocals', 'results/x/v.mp3', 'audio/mpeg')"
    )

    assert queue.complete_job("job_retained") is True
    row = queue._connection.execute(
        "SELECT status, expires_at FROM jobs WHERE id = 'job_retained'"
    ).fetchone()
    assert row[0] == "completed"
    # expires_at ~= now + 2h (allow generous slack for test execution speed).
    assert now + 1.5 * HOUR_MS <= row[1] <= now + 3 * HOUR_MS
    output_expiry = queue._connection.execute(
        "SELECT expires_at FROM job_outputs WHERE id = 'out_r'"
    ).fetchone()[0]
    assert output_expiry == row[1]
