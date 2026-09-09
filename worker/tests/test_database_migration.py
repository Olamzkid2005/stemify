"""Regression tests for the jobs-table rebuild migration (roadmap Phase B).

The migration rewrites a legacy ``jobs`` table (2-value mode CHECK) into the
current one. History: with ``PRAGMA foreign_keys = ON`` during the rebuild,
``ALTER TABLE jobs RENAME TO jobs_old`` silently rewrote job_outputs'
``REFERENCES jobs`` clause to point at jobs_old, and the subsequent DROP left
it dangling — every later insert into job_outputs then failed with
"no such table: main.jobs_old". These tests pin the fixed behavior: the
rebuild runs with FK enforcement off, foreign keys are re-enabled afterwards,
and job_outputs keeps pointing at the rebuilt jobs table.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from worker.database import JOBS_TABLE_DDL, JobQueue

# The pre-drum_breakdown jobs DDL (2-value mode CHECK), as shipped before the
# Phase B schema change: identical to the current DDL except the mode CHECK.
# Derived from the live constant so the fixture can never drift from what the
# migration's INSERT ... SELECT expects.
LEGACY_JOBS_DDL = JOBS_TABLE_DDL.replace(
    "mode IN ('vocals_instrumental', 'full_stems', 'drum_breakdown')",
    "mode IN ('vocals_instrumental', 'full_stems')",
).replace(
    "CREATE TABLE IF NOT EXISTS jobs",
    "CREATE TABLE jobs",
)

LEGACY_JOB_OUTPUTS_DDL = """
CREATE TABLE job_outputs (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  stem_key TEXT NOT NULL,
  label TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  size_bytes INTEGER,
  duration_seconds REAL,
  sha256 TEXT,
  created_at INTEGER NOT NULL DEFAULT 0,
  expires_at INTEGER
);
"""


def _make_legacy_db(data_dir: Path, with_row: bool = True) -> Path:
    """Create a database with the legacy schema, as an older release would."""
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "stemify.sqlite3"
    con = sqlite3.connect(db_path)
    con.executescript("PRAGMA foreign_keys = ON;")
    con.executescript(LEGACY_JOBS_DDL + LEGACY_JOB_OUTPUTS_DDL)
    if with_row:
        con.execute(
            "INSERT INTO jobs (id, owner_key, source_type, mode, output_format, status)"
            " VALUES ('job_legacy', 'owner', 'upload', 'vocals_instrumental', 'mp3', 'completed')"
        )
        con.execute(
            "INSERT INTO job_outputs (id, job_id, stem_key, label, relative_path, mime_type)"
            " VALUES ('out_legacy', 'job_legacy', 'vocals', 'Vocals', 'r/v.mp3', 'audio/mpeg')"
        )
    con.commit()
    con.close()
    return db_path


def test_rebuild_preserves_job_outputs_foreign_key(tmp_path: Path) -> None:
    """The core regression: job_outputs must reference jobs, never jobs_old."""
    _make_legacy_db(tmp_path)
    queue = JobQueue(data_dir=str(tmp_path))
    try:
        row = queue._connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'job_outputs'"
        ).fetchone()
        assert "jobs_old" not in (row[0] or "")
        assert "REFERENCES jobs" in row[0]
        assert queue._connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        queue.close()


def test_rebuild_preserves_existing_rows(tmp_path: Path) -> None:
    _make_legacy_db(tmp_path)
    queue = JobQueue(data_dir=str(tmp_path))
    try:
        job = queue._connection.execute(
            "SELECT id, mode, status FROM jobs WHERE id = 'job_legacy'"
        ).fetchone()
        assert job == ("job_legacy", "vocals_instrumental", "completed")
        out = queue._connection.execute(
            "SELECT job_id, stem_key FROM job_outputs WHERE id = 'out_legacy'"
        ).fetchone()
        assert out == ("job_legacy", "vocals")
    finally:
        queue.close()


def test_rebuilt_table_accepts_drum_breakdown_and_inserts(tmp_path: Path) -> None:
    """The whole point of the migration: the new mode value must be accepted —
    including inserts into job_outputs against the rebuilt table."""
    _make_legacy_db(tmp_path)
    queue = JobQueue(data_dir=str(tmp_path))
    try:
        queue._connection.execute(
            "INSERT INTO jobs (id, owner_key, source_type, mode, output_format, status)"
            " VALUES ('job_new', 'owner', 'upload', 'drum_breakdown', 'mp3', 'completed')"
        )
        # This is the exact statement that failed with
        # "no such table: main.jobs_old" before the fix.
        queue._connection.execute(
            "INSERT INTO job_outputs (id, job_id, stem_key, label, relative_path, mime_type)"
            " VALUES ('out_new', 'job_new', 'drums', 'Drums', 'r/d.mp3', 'audio/mpeg')"
        )
    finally:
        queue.close()


def test_foreign_keys_are_enforced_after_rebuild(tmp_path: Path) -> None:
    """Re-enabling FK enforcement after the rebuild is part of the contract."""
    _make_legacy_db(tmp_path)
    queue = JobQueue(data_dir=str(tmp_path))
    try:
        assert queue._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            queue._connection.execute(
                "INSERT INTO job_outputs (id, job_id, stem_key, label, relative_path, mime_type)"
                " VALUES ('out_orphan', 'job_missing', 'x', 'X', 'r/x.mp3', 'audio/mpeg')"
            )
    finally:
        queue.close()


def test_fresh_database_needs_no_rebuild(tmp_path: Path) -> None:
    """A brand-new database is created with the current schema, no rebuild."""
    queue = JobQueue(data_dir=str(tmp_path))
    try:
        row = queue._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
        ).fetchone()
        assert "drum_breakdown" in row[0]
        # And inserts work immediately (the FK corruption symptom).
        queue._connection.execute(
            "INSERT INTO jobs (id, owner_key, source_type, mode, output_format, status)"
            " VALUES ('job_fresh', 'owner', 'upload', 'drum_breakdown', 'mp3', 'queued')"
        )
        queue._connection.execute(
            "INSERT INTO job_outputs (id, job_id, stem_key, label, relative_path, mime_type)"
            " VALUES ('out_fresh', 'job_fresh', 'vocals', 'Vocals', 'r/v.mp3', 'audio/mpeg')"
        )
    finally:
        queue.close()
