"""Local cleanup (plan Task 13 / Section 19.3).

One idempotent pass over the shared data directory:
  1. Mark expired jobs (terminal states only) as expired.
  2. Delete their result files under results/{job_id}/ and the job_outputs rows.
  3. Delete expired uploads: the stored source object and the row.
  4. Remove source directories with no uploads row and no job reference.
  5. Remove stale temporary directories left by crashed runs.

Model checkpoints under data/models/ are never touched by cleanup. Safe to run
repeatedly: every step tolerates already-deleted state and reports counts.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from worker.database import JobQueue, _now_ms


@dataclass
class CleanupReport:
    expired_jobs_marked: int = 0
    result_directories_deleted: int = 0
    expired_uploads_deleted: int = 0
    orphaned_sources_deleted: int = 0
    stale_temp_dirs_deleted: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"expired jobs marked: {self.expired_jobs_marked}, "
            f"result dirs deleted: {self.result_directories_deleted}, "
            f"expired uploads deleted: {self.expired_uploads_deleted}, "
            f"orphaned sources deleted: {self.orphaned_sources_deleted}, "
            f"stale temp dirs deleted: {self.stale_temp_dirs_deleted}"
            + (f", errors: {len(self.errors)}" if self.errors else "")
        )


def mark_expired_jobs(queue: JobQueue, now: int | None = None) -> int:
    """Expire terminal jobs past their retention window; processing is never touched."""
    now = now if now is not None else _now_ms()
    cursor = queue._connection.execute(
        "UPDATE jobs SET status = 'expired', updated_at = ? "
        "WHERE expires_at IS NOT NULL AND expires_at < ? "
        "AND status IN ('completed', 'failed', 'canceled')",
        (now, now),
    )
    return cursor.rowcount


def delete_expired_results(queue: JobQueue, now: int | None = None) -> int:
    """Delete results/{job_id}/ for expired jobs, then their job_outputs rows."""
    now = now if now is not None else _now_ms()
    rows = queue._connection.execute(
        "SELECT id FROM jobs WHERE expires_at IS NOT NULL AND expires_at < ? AND status = 'expired'",
        (now,),
    ).fetchall()
    deleted = 0
    for (job_id,) in rows:
        results_dir = queue.data_dir / "results" / job_id
        if results_dir.is_dir():
            shutil.rmtree(results_dir, ignore_errors=True)
            deleted += 1
    queue._connection.execute(
        "DELETE FROM job_outputs WHERE job_id IN ("
        "SELECT id FROM jobs WHERE expires_at IS NOT NULL AND expires_at < ? AND status = 'expired')",
        (now,),
    )
    return deleted


def delete_expired_uploads(queue: JobQueue, now: int | None = None) -> int:
    """Delete the stored source object and row for expired uploads."""
    now = now if now is not None else _now_ms()
    rows = queue._connection.execute(
        "SELECT id, object_key FROM uploads WHERE expires_at IS NOT NULL AND expires_at < ?",
        (now,),
    ).fetchall()
    deleted = 0
    for upload_id, object_key in rows:
        target = queue.data_dir / object_key
        if target.is_file():
            target.unlink(missing_ok=True)
        cursor = queue._connection.execute(
            "DELETE FROM uploads WHERE id = ? AND expires_at < ?",
            (upload_id, now),
        )
        deleted += cursor.rowcount
    return deleted


def delete_orphaned_sources(queue: JobQueue) -> int:
    """Remove sources/upl_<32hex>/ directories with no uploads row and no job reference.

    Covers the crash window between the file write and the database INSERT.
    Unknown directory names are never touched. Model checkpoints live under
    data/models/ and are outside this namespace by construction.
    """
    sources_dir = queue.data_dir / "sources"
    if not sources_dir.is_dir():
        return 0

    known_prefixes = {
        row[0]
        for row in queue._connection.execute(
            "SELECT object_key FROM uploads UNION SELECT source_object_key FROM jobs "
            "WHERE source_object_key IS NOT NULL"
        ).fetchall()
        if row[0]
    }

    deleted = 0
    for entry in sources_dir.iterdir():
        if not entry.is_dir():
            continue
        prefix = f"sources/{entry.name}/"
        if any(key.startswith(prefix) for key in known_prefixes):
            continue
        if not re_full_upload_dir.match(entry.name):
            continue  # never delete unrecognized names
        shutil.rmtree(entry, ignore_errors=True)
        deleted += 1
    return deleted


def delete_stale_temp_dirs(queue: JobQueue, max_age_ms: int = 6 * 3_600_000) -> int:
    """Remove stemify-job-* temp directories older than max_age_ms (default 6h)."""
    temp_root = Path(os.environ.get("TMPDIR", os.environ.get("TEMP", "/tmp")))
    deleted = 0
    now = _now_ms()
    for entry in temp_root.glob("stemify-job-*"):
        if not entry.is_dir():
            continue
        try:
            age = now - int(entry.stat().st_mtime * 1000)
        except OSError:
            continue
        if age > max_age_ms:
            shutil.rmtree(entry, ignore_errors=True)
            deleted += 1
    return deleted


import re

re_full_upload_dir = re.compile(r"^upl_[a-f0-9]{32}$")


def run_cleanup(queue: JobQueue, now: int | None = None) -> CleanupReport:
    """One idempotent cleanup pass; per-step failures are collected, not raised."""
    report = CleanupReport()
    steps = (
        lambda: mark_expired_jobs(queue, now),
        lambda: delete_expired_results(queue, now),
        lambda: delete_expired_uploads(queue, now),
        lambda: delete_orphaned_sources(queue),
        lambda: delete_stale_temp_dirs(queue),
    )
    for step in steps:
        try:
            value = step()
        except Exception as error:  # noqa: BLE001 - cleanup reports, never aborts
            report.errors.append(str(error))
            continue
        if step is steps[0]:
            report.expired_jobs_marked = value
        elif step is steps[1]:
            report.result_directories_deleted = value
        elif step is steps[2]:
            report.expired_uploads_deleted = value
        elif step is steps[3]:
            report.orphaned_sources_deleted = value
        else:
            report.stale_temp_dirs_deleted = value
    return report


__all__ = [
    "CleanupReport",
    "delete_expired_results",
    "delete_expired_uploads",
    "delete_orphaned_sources",
    "delete_stale_temp_dirs",
    "mark_expired_jobs",
    "run_cleanup",
]
