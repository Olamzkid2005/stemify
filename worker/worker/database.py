"""SQLite queue access for the local worker (plan Sections 11.2, 12.1, 13.3).

Opens the same database file the web app writes and owns one connection with
WAL mode and a busy timeout, so the web process and the worker can run side by
side. Claiming a job is one short atomic transaction; all state updates are
guarded so no terminal state ever moves backward.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from worker.errors import ErrorCode
from worker.stages import Stage

# Same DDL as apps/web/lib/db/schema.ts (SQLITE_SCHEMA). Kept in sync by
# tests/test_contract_parity.py — update both sides in the same change.
SQLITE_SCHEMA = """\
CREATE TABLE IF NOT EXISTS uploads (
  id TEXT PRIMARY KEY,
  owner_key TEXT NOT NULL,
  filename TEXT NOT NULL,
  object_key TEXT NOT NULL UNIQUE,
  size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
  created_at INTEGER NOT NULL DEFAULT (unixepoch('subsec') * 1000),
  expires_at INTEGER
);

CREATE INDEX IF NOT EXISTS uploads_owner_created_idx ON uploads(owner_key, created_at);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  access_token_hash TEXT,
  owner_key TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK (source_type IN ('upload', 'youtube')),
  source_filename TEXT,
  source_object_key TEXT,
  source_path TEXT,
  source_url TEXT,
  source_duration_seconds REAL,
  source_size_bytes INTEGER,
  source_sha256 TEXT,
  mode TEXT NOT NULL CHECK (mode IN ('vocals_instrumental', 'full_stems')),
  output_format TEXT NOT NULL CHECK (output_format IN ('mp3', 'wav', 'flac', 'ogg', 'm4a')),
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'processing', 'completed', 'failed', 'canceled', 'expired')),
  stage TEXT,
  progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
  cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
  worker_call_id TEXT,
  idempotency_key_hash TEXT,
  error_code TEXT,
  error_message_public TEXT,
  diagnostic_reference TEXT,
  created_at INTEGER NOT NULL DEFAULT (unixepoch('subsec') * 1000),
  started_at INTEGER,
  completed_at INTEGER,
  expires_at INTEGER,
  updated_at INTEGER NOT NULL DEFAULT (unixepoch('subsec') * 1000),
  UNIQUE (owner_key, idempotency_key_hash)
);

CREATE TABLE IF NOT EXISTS job_outputs (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  stem_key TEXT NOT NULL,
  label TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  size_bytes INTEGER,
  duration_seconds REAL,
  sha256 TEXT,
  created_at INTEGER NOT NULL DEFAULT (unixepoch('subsec') * 1000),
  expires_at INTEGER
);

CREATE TABLE IF NOT EXISTS job_events (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  stage TEXT,
  progress INTEGER CHECK (progress BETWEEN 0 AND 100),
  detail TEXT,
  created_at INTEGER NOT NULL DEFAULT (unixepoch('subsec') * 1000)
);

CREATE INDEX IF NOT EXISTS jobs_status_created_idx ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS jobs_expires_at_idx ON jobs(expires_at);
CREATE INDEX IF NOT EXISTS job_outputs_job_id_idx ON job_outputs(job_id);
CREATE INDEX IF NOT EXISTS job_events_job_id_idx ON job_events(job_id);
"""


@dataclass(frozen=True)
class ClaimedJob:
    id: str
    source_type: str
    source_object_key: str | None
    mode: str = "vocals_instrumental"
    output_format: str = "mp3"
    source_filename: str | None = None
    expires_at: int | None = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _diagnostic_reference() -> str:
    return f"diag_{uuid.uuid4().hex[:12]}"


class JobQueue:
    """Polling queue over the shared local SQLite file."""

    def __init__(self, data_dir: str | None = None) -> None:
        directory = Path(
            data_dir or os.environ.get("STEMIFY_DATA_DIR") or Path.cwd() / "data"
        ).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        self.data_dir = directory
        self.database_path = directory / "stemify.sqlite3"
        self.worker_call_id = uuid.uuid4().hex
        # isolation_level=None: explicit transactions only, autocommit otherwise.
        # check_same_thread=False: the connection may be created before the loop
        # thread starts; each instance is still used by one thread at a time.
        self._connection = sqlite3.connect(
            self.database_path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.executescript(SQLITE_SCHEMA)

    def close(self) -> None:
        self._connection.close()

    def claim_next_queued_job(self) -> ClaimedJob | None:
        """Atomically move the oldest queued job to processing (plan Section 11.2)."""
        now = _now_ms()
        cursor = self._connection.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            row = cursor.execute(
                "SELECT id, source_type, source_object_key, mode, output_format, "
                "source_filename, expires_at FROM jobs "
                "WHERE status = 'queued' ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if row is None:
                self._connection.execute("ROLLBACK")
                return None
            cursor.execute(
                "UPDATE jobs SET status = 'processing', stage = 'starting', progress = 5, "
                "started_at = ?, updated_at = ?, worker_call_id = ? "
                "WHERE id = ? AND status = 'queued'",
                (now, now, self.worker_call_id, row[0]),
            )
            if cursor.rowcount != 1:  # pragma: no cover — impossible under BEGIN IMMEDIATE
                self._connection.execute("ROLLBACK")
                return None
            self._connection.execute("COMMIT")
            return ClaimedJob(
                id=row[0],
                source_type=row[1],
                source_object_key=row[2],
                mode=row[3],
                output_format=row[4],
                source_filename=row[5],
                expires_at=row[6],
            )
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        finally:
            cursor.close()

    def update_progress(self, job_id: str, stage: Stage, progress: int) -> bool:
        """Persist progress while processing; False once the job is no longer ours."""
        cursor = self._connection.execute(
            "UPDATE jobs SET stage = ?, progress = ?, updated_at = ? "
            "WHERE id = ? AND status = 'processing'",
            (stage.value, max(0, min(100, int(progress))), _now_ms(), job_id),
        )
        return cursor.rowcount == 1

    def fail_job(self, job_id: str, code: ErrorCode, message_public: str) -> None:
        """Move a queued/processing job to failed; terminal states never change."""
        now = _now_ms()
        self._connection.execute(
            "UPDATE jobs SET status = 'failed', error_code = ?, error_message_public = ?, "
            "diagnostic_reference = ?, completed_at = ?, updated_at = ? "
            "WHERE id = ? AND status IN ('queued', 'processing')",
            (code.value, message_public, _diagnostic_reference(), now, now, job_id),
        )

    def is_cancel_requested(self, job_id: str) -> bool:
        """Cancellation flag read (plan Section 9.5); cheap, one indexed lookup."""
        row = self._connection.execute(
            "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return bool(row and row[0])

    def cancel_queued_job(self, job_id: str) -> bool:
        """Cancel a queued job immediately; False for non-queued jobs."""
        now = _now_ms()
        cursor = self._connection.execute(
            "UPDATE jobs SET status = 'canceled', completed_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'queued'",
            (now, now, job_id),
        )
        return cursor.rowcount == 1

    def record_output(
        self,
        job_id: str,
        stem_key: str,
        label: str,
        relative_path: str,
        mime_type: str,
        size_bytes: int,
        duration_seconds: float,
        sha256: str,
        expires_at: int | None,
    ) -> None:
        """Insert one completed output row (plan Section 11.2)."""
        self._connection.execute(
            "INSERT INTO job_outputs "
            "(id, job_id, stem_key, label, relative_path, mime_type, size_bytes, "
            " duration_seconds, sha256, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"out_{uuid.uuid4().hex[:24]}",
                job_id,
                stem_key,
                label,
                relative_path,
                mime_type,
                size_bytes,
                duration_seconds,
                sha256,
                _now_ms(),
                expires_at,
            ),
        )

    def complete_job(self, job_id: str) -> bool:
        """Mark a processing job completed; False if it is no longer processing."""
        now = _now_ms()
        cursor = self._connection.execute(
            "UPDATE jobs SET status = 'completed', stage = 'completed', progress = 100, "
            "completed_at = ?, updated_at = ? WHERE id = ? AND status = 'processing'",
            (now, now, job_id),
        )
        return cursor.rowcount == 1

    def cancel_processing_job(self, job_id: str) -> bool:
        """Finalize a processing job as canceled (plan Section 9.5: never completed)."""
        now = _now_ms()
        cursor = self._connection.execute(
            "UPDATE jobs SET status = 'canceled', error_code = ?, "
            "error_message_public = ?, completed_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'processing'",
            (ErrorCode.CANCELED.value, "This job was canceled.", now, now, job_id),
        )
        return cursor.rowcount == 1

    def recover_stale_processing_jobs(self) -> int:
        """Fail jobs left in processing by a previous run (plan Section 13.2).

        The local milestone runs one worker per data directory, so any row still
        processing at startup belongs to a dead process.
        """
        now = _now_ms()
        cursor = self._connection.execute(
            "UPDATE jobs SET status = 'failed', error_code = ?, "
            "error_message_public = ?, diagnostic_reference = ?, completed_at = ?, updated_at = ? "
            "WHERE status = 'processing'",
            (
                ErrorCode.UNKNOWN.value,
                "Processing was interrupted when the worker restarted. Try again.",
                _diagnostic_reference(),
                now,
                now,
            ),
        )
        return cursor.rowcount
