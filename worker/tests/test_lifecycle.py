"""Task 15 lifecycle integration tests (plan Sections 17.3, 9.4, 9.7).

Real SQLite, real cleanup, real job-loop transitions; the separation engine and
ffmpeg are stubbed where needed so the suite stays offline and fast. Covers the
Task 15 acceptance items not already exercised elsewhere:

- A worker restart preserves completed metadata and never re-runs terminal jobs.
- A job left `processing` by a dead worker ends as failed UNKNOWN at startup
  (never completed), and a fresh retry job can then complete.
- The full local flow: queued -> claimed -> completed -> published outputs,
  with a second owner's job staying invisible (authorization sanity).
- After the retention window, one cleanup pass deletes results and leaves the
  tree safe to run again (idempotent).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from test_pipeline_integration import (
    install_stub_engine,
    make_tone_mp3,
    read_row,
    seed_job_with_source,
)

from worker.cleanup import run_cleanup
from worker.database import JobQueue
from worker.errors import ErrorCode
from worker.input_audio import _require_ffmpeg_tool

try:
    FFMPEG = _require_ffmpeg_tool("ffmpeg")
except Exception:  # noqa: BLE001
    FFMPEG = None

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available")


def _insert_upload_and_job(queue: JobQueue, job_id: str, owner: str, source: Path) -> None:
    """Seed an upload row + queued job the way the web app would."""
    object_key = f"sources/{job_id}/song.mp3"
    destination = queue.data_dir / object_key
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    connection = sqlite3.connect(queue.database_path)
    try:
        connection.execute(
            "INSERT INTO uploads (id, owner_key, filename, object_key, size_bytes, expires_at)"
            " VALUES (?, ?, 'song.mp3', ?, ?, NULL)",
            (f"upl_{job_id[4:36]}", owner, object_key, destination.stat().st_size),
        )
        connection.execute(
            "INSERT INTO jobs (id, owner_key, source_type, source_object_key, source_filename,"
            " mode, output_format) VALUES (?, ?, 'upload', ?, 'song.mp3',"
            " 'vocals_instrumental', 'mp3')",
            (job_id, owner, object_key),
        )
        connection.commit()
    finally:
        connection.close()


def test_worker_restart_preserves_completed_and_never_reclaims_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan 2.2: a local application restart preserves completed results."""
    from worker.job_loop import process_job

    install_stub_engine(monkeypatch)
    queue, job_id = seed_job_with_source(tmp_path, monkeypatch)
    job = queue.claim_next_queued_job()
    assert job is not None
    process_job(queue, job)

    first = read_row(
        queue.database_path, "SELECT status, progress FROM jobs WHERE id = ?", (job_id,)
    )[0]
    assert first == ("completed", 100)
    outputs_before = read_row(
        queue.database_path, "SELECT COUNT(*) FROM job_outputs WHERE job_id = ?", (job_id,)
    )[0][0]
    queue.close()

    # A brand-new worker instance (simulated restart) opens the same database.
    restarted = JobQueue(data_dir=str(tmp_path / "data"))
    restarted.recover_stale_processing_jobs()

    assert read_row(
        restarted.database_path, "SELECT status FROM jobs WHERE id = ?", (job_id,)
    )[0][0] == "completed"
    assert read_row(
        restarted.database_path, "SELECT COUNT(*) FROM job_outputs WHERE job_id = ?", (job_id,)
    )[0][0] == outputs_before
    assert restarted.claim_next_queued_job() is None  # terminal jobs are never re-claimed
    restarted.close()


def test_interrupted_job_fails_at_restart_and_retry_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan 9.4/13.2: a dead worker's processing job ends failed, never completed."""
    install_stub_engine(monkeypatch)
    queue = JobQueue(data_dir=str(tmp_path / "data"))
    source = tmp_path / "song.mp3"
    make_tone_mp3(source)
    job_id = "job_" + "d" * 32
    _insert_upload_and_job(queue, job_id, "owner_lifecycle", source)

    claimed = queue.claim_next_queued_job()
    assert claimed is not None
    # The worker dies here: no terminal write happens. The next startup
    # recovers the orphaned row.
    recovered = queue.recover_stale_processing_jobs()
    assert recovered == 1
    row = read_row(queue.database_path, "SELECT status, error_code FROM jobs WHERE id = ?", (job_id,))[0]
    assert row == ("failed", ErrorCode.UNKNOWN.value)

    # The owner retries with a fresh job; it completes normally.
    retry_id = "job_" + "e" * 32
    _insert_upload_and_job(queue, retry_id, "owner_lifecycle", source)
    fresh = JobQueue(data_dir=str(tmp_path / "data"))
    job = fresh.claim_next_queued_job()
    assert job is not None and job.id == retry_id

    from worker.job_loop import process_job

    process_job(fresh, job)
    assert read_row(
        fresh.database_path, "SELECT status FROM jobs WHERE id = ?", (retry_id,)
    )[0][0] == "completed"
    fresh.close()
    queue.close()


def test_full_upload_to_results_flow_with_owner_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clean-setup vertical slice: create -> claim -> complete -> published."""
    from worker.job_loop import process_job

    install_stub_engine(monkeypatch)
    queue = JobQueue(data_dir=str(tmp_path / "data"))
    source = tmp_path / "song.mp3"
    make_tone_mp3(source)

    owner_job = "job_" + "1" * 32
    other_job = "job_" + "2" * 32
    _insert_upload_and_job(queue, owner_job, "owner_one", source)
    _insert_upload_and_job(queue, other_job, "owner_two", source)

    job = queue.claim_next_queued_job()
    assert job is not None
    process_job(queue, job)
    assert read_row(queue.database_path, "SELECT status FROM jobs WHERE id = ?", (job.id,))[0][0] == "completed"

    # Published outputs live under results/<job_id>/ and are non-empty.
    outputs = read_row(
        queue.database_path,
        "SELECT stem_key, relative_path FROM job_outputs WHERE job_id = ? ORDER BY stem_key",
        (owner_job,),
    )
    assert [row[0] for row in outputs] == ["archive", "instrumental", "vocals"]
    for _, relative_path in outputs:
        published = queue.data_dir / relative_path
        assert published.is_file() and published.stat().st_size > 0

    # The other owner's job is untouched and still queued.
    assert read_row(
        queue.database_path, "SELECT status FROM jobs WHERE id = ?", (other_job,)
    )[0][0] == "queued"
    queue.close()


def test_cleanup_after_retention_window_and_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan 9.7/19.3: results are deleted after retention; cleanup is rerunnable."""
    from worker.job_loop import process_job

    install_stub_engine(monkeypatch)
    queue, job_id = seed_job_with_source(tmp_path, monkeypatch)
    job = queue.claim_next_queued_job()
    assert job is not None
    process_job(queue, job)

    results_dir = queue.data_dir / "results" / job_id
    assert results_dir.is_dir()

    # Force the retention window to be over, then run one cleanup pass.
    import time

    past = int((time.time() - 3600) * 1000)  # an hour ago
    connection = sqlite3.connect(queue.database_path)
    try:
        connection.execute("UPDATE jobs SET expires_at = ? WHERE id = ?", (past, job_id))
        connection.commit()
    finally:
        connection.close()

    report = run_cleanup(queue)
    assert report.expired_jobs_marked == 1
    assert report.result_directories_deleted >= 1
    assert not results_dir.exists()
    assert not read_row(
        queue.database_path, "SELECT 1 FROM job_outputs WHERE job_id = ?", (job_id,)
    )
    assert read_row(
        queue.database_path, "SELECT status FROM jobs WHERE id = ?", (job_id,)
    )[0][0] == "expired"

    # A second pass changes nothing (idempotent; safe to run repeatedly).
    second = run_cleanup(queue)
    assert second.expired_jobs_marked == 0
    assert second.result_directories_deleted == 0
    queue.close()
