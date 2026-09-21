"""SQLite queue access for the local worker (plan Sections 11.2, 12.1, 13.3).

Opens the same database file the web app writes and owns one connection with
WAL mode and a busy timeout, so the web process and the worker can run side by
side. Claiming a job is one short atomic transaction; all state updates are
guarded so no terminal state ever moves backward.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from worker.errors import ErrorCode
from worker.stages import Stage

# Same DDL as apps/web/lib/db/schema.ts (SQLITE_SCHEMA). Kept in sync by
# review — update both sides in the same change. Note: SQLite cannot alter a
# CHECK constraint and constraints re-evaluate on every UPDATE, so both
# clients rebuild the jobs table when it still carries an older enum
# constraint — the 2-value mode CHECK, or the source_type CHECK that predates
# Spotify input (_migrate_jobs_constraints below / apps/web/lib/db/client.ts).
JOBS_TABLE_DDL = """\
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  access_token_hash TEXT,
  owner_key TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK (source_type IN ('upload', 'youtube', 'spotify')),
  source_filename TEXT,
  -- Album the resolved source belongs to (link sources only); shown on the job
  -- page. Nullable and app-side, like source_filename: a lookup failure just
  -- means nothing to display.
  source_album TEXT,
  source_object_key TEXT,
  source_path TEXT,
  source_url TEXT,
  source_duration_seconds REAL,
  source_size_bytes INTEGER,
  source_sha256 TEXT,
  mode TEXT NOT NULL CHECK (mode IN ('vocals_instrumental', 'full_stems', 'drum_breakdown', 'custom')),
  output_format TEXT NOT NULL CHECK (output_format IN ('mp3', 'wav', 'flac', 'ogg', 'm4a')),
  -- Per-job quality preset (STEMIFY_QUALITY values); NULL = worker default.
  -- Nullable TEXT on purpose: validation is app-side, so a future preset
  -- never needs another table rebuild (SQLite cannot alter CHECKs).
  quality TEXT,
  -- Stem list for mode='custom' (docs/STEM_SELECTION_PLAN.md): a JSON array of
  -- 1-4 keys from vocals/drums/bass/instrumental, validated app-side and
  -- re-validated at claim. NULL for every other mode.
  stem_selection TEXT,
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
"""

SQLITE_SCHEMA = JOBS_TABLE_DDL + """\
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

CREATE TABLE IF NOT EXISTS worker_heartbeat (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  updated_at INTEGER NOT NULL
);

-- One row per live worker process (concurrency plan 3.3). `worker_heartbeat`
-- stays the single "is any worker up" signal the UI reads; this table answers
-- the different question recovery needs: which worker owns a processing job,
-- and is that worker still alive? Without it, a second worker starting up
-- would fail the job the first one is in the middle of.
CREATE TABLE IF NOT EXISTS workers (
  worker_call_id TEXT PRIMARY KEY,
  pid INTEGER,
  started_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class ClaimedJob:
    id: str
    source_type: str
    source_object_key: str | None
    mode: str = "vocals_instrumental"
    output_format: str = "mp3"
    quality: str | None = None
    stem_selection: tuple[str, ...] | None = None
    source_filename: str | None = None
    expires_at: int | None = None
    source_url: str | None = None


def _now_ms() -> int:
    return int(time.time() * 1000)


# mode='custom' carries this exact allowlist (docs/STEM_SELECTION_PLAN.md).
STEM_SELECTION_KEYS = ("vocals", "drums", "bass", "instrumental")


def parse_stem_selection(raw: str | None) -> tuple[str, ...] | None:
    """Decode the jobs.stem_selection JSON column, or None when absent.

    Claim-time re-validation, not trust: the column was written by another
    process (the web app), so a malformed or unknown value reads as None —
    the job then fails validation in job_loop with a clear code instead of a
    JSON error deep in the pipeline. A selection on a non-custom mode is
    ignored the same way: the mode decides.
    """
    if raw is None or raw == "":
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(item, str) for item in value):
        return None
    if len(set(value)) != len(value):
        return None
    if not set(value) <= set(STEM_SELECTION_KEYS):
        return None
    return tuple(value)


# How long a worker's row may go without a heartbeat before recovery treats it
# as dead (concurrency plan 3.3): 30s is six 5s heartbeat periods, long enough
# that a stalled disk write cannot orphan the job of a worker that is running.
WORKER_STALE_MS = 30_000


def _retention_ms() -> int:
    """Retention window in ms (plan Section 15: JOB_RETENTION_HOURS, default 24)."""
    raw = os.environ.get("JOB_RETENTION_HOURS", "24")
    try:
        hours = float(raw)
    except ValueError:
        hours = 24.0
    return int(max(0.0, hours) * 3_600_000)


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
        self._worker_pid = os.getpid()
        self._worker_started_at = _now_ms()
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
        # Migration must precede the schema script: the CREATE INDEX statements
        # would otherwise attach to the old jobs table and be dropped with it
        # during the rebuild.
        self._migrate_jobs_constraints()
        self._connection.executescript(SQLITE_SCHEMA)
        self._migrate_additive_columns()

    def close(self) -> None:
        self._connection.close()

    def _migrate_jobs_constraints(self) -> None:
        """Rebuild the jobs table when an enum CHECK predates a current value.

        SQLite cannot alter a CHECK constraint, and CHECKs re-evaluate on every
        UPDATE — so a database created before drum_breakdown (roadmap Phase B)
        or before Spotify input would reject even unrelated updates to any jobs
        row. The rebuild is a no-op once the table accepts every current value.
        """
        row = self._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
        ).fetchone()
        if row is None:
            return
        table_sql = row[0] or ""
        # Both CHECKs live on this one table, so one rebuild covers either gap.
        # The quoted token cannot match a comment.
        if "drum_breakdown" in table_sql and "'spotify'" in table_sql and "'custom'" in table_sql:
            return
        # Project exactly the columns the old table has. Naming them
        # explicitly is what stops a rebuild triggered by one constraint from
        # dropping the values of a column an earlier migration added —
        # jobs.quality is the live example, and it must survive this rebuild.
        columns = ", ".join(
            column[1]
            for column in self._connection.execute("PRAGMA table_info(jobs)").fetchall()
        )
        # Rebuilding a table that other tables reference via foreign keys:
        # modern SQLite rewrites the REFERENCES clauses in job_outputs to
        # point at jobs_old on ANY ALTER TABLE ... RENAME (even with FK
        # enforcement off — verified empirically on sqlite 3.50), and the
        # subsequent DROP leaves them dangling ("no such table:
        # main.jobs_old" on every later insert). The documented opt-out is
        # legacy_alter_table during the rename; FK enforcement also goes off
        # for the rebuild, per the sqlite.org altertable procedure. Both are
        # restored afterwards, and PRAGMA foreign_key_check verifies the
        # rebuilt schema.
        self._connection.execute("PRAGMA foreign_keys = OFF")
        self._connection.execute("PRAGMA legacy_alter_table = ON")
        try:
            self._connection.executescript(
                """
                BEGIN IMMEDIATE;
                ALTER TABLE jobs RENAME TO jobs_old;
                """
                + JOBS_TABLE_DDL
                + f"""
                INSERT INTO jobs ({columns})
                SELECT {columns}
                FROM jobs_old;
                DROP TABLE jobs_old;
                COMMIT;
                """
            )
        finally:
            self._connection.execute("PRAGMA legacy_alter_table = OFF")
            self._connection.execute("PRAGMA foreign_keys = ON")
        violations = self._connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"foreign key violations after jobs rebuild: {violations!r}")

    def _migrate_additive_columns(self) -> None:
        """Add jobs columns that postdate the table's first release.

        Additive ALTERs (no rebuild): every one of these is nullable and
        validated app-side, so an older row simply reads NULL — no per-job
        quality (worker default), no source album to show. Kept in one place so
        a new column is one entry rather than another migration method.
        """
        columns = {
            row[1] for row in self._connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "quality" not in columns:
            self._connection.execute("ALTER TABLE jobs ADD COLUMN quality TEXT")
        if "source_album" not in columns:
            self._connection.execute("ALTER TABLE jobs ADD COLUMN source_album TEXT")
        if "stem_selection" not in columns:
            self._connection.execute("ALTER TABLE jobs ADD COLUMN stem_selection TEXT")

    def claim_next_queued_job(self) -> ClaimedJob | None:
        """Atomically move the oldest queued job to processing (plan Section 11.2)."""
        now = _now_ms()
        cursor = self._connection.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            row = cursor.execute(
                "SELECT id, source_type, source_object_key, mode, output_format, "
                "source_filename, expires_at, source_url, quality, stem_selection FROM jobs "
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
                source_url=row[7],
                quality=row[8],
                stem_selection=parse_stem_selection(row[9]),
            )
        except BaseException:
            # Only unwind a transaction that actually opened: when BEGIN IMMEDIATE
            # itself failed (a locked database is the realistic case, after the
            # busy timeout), there is nothing to roll back and ROLLBACK would
            # raise "cannot rollback - no transaction is active", replacing the
            # caller's clear error with an unrelated one.
            with contextlib.suppress(sqlite3.Error):
                self._connection.execute("ROLLBACK")
            raise
        finally:
            cursor.close()

    def update_progress(
        self,
        job_id: str,
        stage: Stage,
        progress: int,
        detail: str | None = None,
    ) -> bool:
        """Persist current stage/progress and a safe user-facing detail.

        Best-effort by contract (concurrency plan 2.4): progress is cosmetic, so
        with several workers writing the same file a `SQLITE_BUSY` on a progress
        row must never fail the job it is reporting on. Job state transitions
        (claim, fail, complete) stay strict — only this one is swallowed.
        """
        bounded = max(0, min(100, int(progress)))
        try:
            cursor = self._connection.execute(
                "UPDATE jobs SET stage = ?, progress = ?, updated_at = ? "
                "WHERE id = ? AND status = 'processing'",
                (stage.value, bounded, _now_ms(), job_id),
            )
            updated = cursor.rowcount == 1
        except Exception:  # noqa: BLE001 - cosmetic; the next update retries
            return False
        if updated and detail:
            self.record_event(job_id, "progress", detail, stage=stage, progress=bounded)
        return updated

    def record_source_filename(self, job_id: str, filename: str) -> None:
        """Store a resolved display name for the source (roadmap A2).

        YouTube jobs arrive with source_filename NULL; the worker fills it with
        the sanitized video title so downloads and ZIPs are named after the
        song. Only running jobs are updated, and failures are swallowed: the
        naming is cosmetic, the job must never fail because of it.
        """
        try:
            self._connection.execute(
                "UPDATE jobs SET source_filename = ?, updated_at = ? "
                "WHERE id = ? AND status = 'processing'",
                (filename, _now_ms(), job_id),
            )
        except Exception:  # noqa: BLE001, S110 - naming is cosmetic, never fatal
            pass

    def record_source_album(self, job_id: str, album: str) -> None:
        """Store the album the source belongs to, for the job view.

        Same contract as `record_source_filename`: only running jobs are
        updated and failures are swallowed, because this is decoration — a job
        must never fail over a missing album name.
        """
        try:
            self._connection.execute(
                "UPDATE jobs SET source_album = ?, updated_at = ? "
                "WHERE id = ? AND status = 'processing'",
                (album, _now_ms(), job_id),
            )
        except Exception:  # noqa: BLE001, S110 - decoration, never fatal
            pass

    def record_event(
        self,
        job_id: str,
        event_type: str,
        detail: str,
        *,
        stage: Stage | None = None,
        progress: int | None = None,
    ) -> None:
        """Append a sanitized local event; diagnostics are best-effort only."""
        try:
            self._connection.execute(
                "INSERT INTO job_events "
                "(id, job_id, event_type, stage, progress, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f"evt_{_diagnostic_reference()}",
                    job_id,
                    event_type,
                    stage.value if stage else None,
                    progress,
                    detail,
                    _now_ms(),
                ),
            )
        except Exception:  # noqa: BLE001, S110 - events are diagnostic, never fatal
            pass

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
        """Mark a processing job completed and stamp the retention window (plan Task 13).

        expires_at = completed_at + JOB_RETENTION_HOURS (default 24h) on both the
        job and its outputs; the local cleanup pass deletes those files later.
        """
        now = _now_ms()
        expires_at = now + _retention_ms()
        cursor = self._connection.execute(
            "UPDATE jobs SET status = 'completed', stage = 'completed', progress = 100, "
            "completed_at = ?, updated_at = ?, expires_at = ? WHERE id = ? AND status = 'processing'",
            (now, now, expires_at, job_id),
        )
        if cursor.rowcount == 1:
            self._connection.execute(
                "UPDATE job_outputs SET expires_at = ? WHERE job_id = ? AND expires_at IS NULL",
                (expires_at, job_id),
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

    def discard_unpublished_results(self, job_id: str) -> bool:
        """Delete a job's results directory when nothing was published to it.

        Failing a job is not always the end of its artifacts: a Spotify fetch
        writes the album cover straight into `results/{job_id}/` during the
        download, and retention only deletes results for *expired* jobs — while
        only a completed job ever receives an `expires_at`. Without this, a
        failed link job leaves that cover on disk for good.

        Published outputs are deliberately kept: they belong to a job the user
        may still be able to download from. Returns True when a directory was
        removed.
        """
        try:
            published = self._connection.execute(
                "SELECT 1 FROM job_outputs WHERE job_id = ? LIMIT 1", (job_id,)
            ).fetchone()
            if published is not None:
                return False
            directory = self.data_dir / "results" / job_id
            if not directory.is_dir():
                return False
            shutil.rmtree(directory, ignore_errors=True)
            return True
        except Exception:  # noqa: BLE001 - tidying a failed job is never fatal
            return False

    def write_heartbeat(self) -> None:
        """Upsert the single worker-liveness row (plan Section 19.2).

        The web app compares updated_at against its own clock to show a
        non-sensitive worker-unavailable state when the worker is not running.

        The same tick refreshes this worker's own `workers` row (concurrency
        plan 3.3): one statement per concern, so a heartbeat can never advance
        liveness without also proving *which* worker is alive — the cheap
        INSERT form keeps a lone heartbeat (a queue used outside `run()`)
        self-registering rather than leaving a NULL owner behind.
        """
        now = _now_ms()
        self._connection.execute(
            "INSERT INTO worker_heartbeat (id, updated_at) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at",
            (now,),
        )
        self._connection.execute(
            "INSERT INTO workers (worker_call_id, pid, started_at, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(worker_call_id) DO UPDATE SET updated_at = excluded.updated_at",
            (self.worker_call_id, self._worker_pid, self._worker_started_at, now),
        )

    def register_worker(self) -> None:
        """Publish this process as a live worker before it claims anything.

        `started_at`/`pid` make a stuck worker identifiable in the local
        database; recovery only reads `updated_at`. Idempotent, so a restart
        that reuses the id cannot fail on an existing row.
        """
        now = _now_ms()
        self._connection.execute(
            "INSERT INTO workers (worker_call_id, pid, started_at, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(worker_call_id) DO UPDATE SET "
            "pid = excluded.pid, updated_at = excluded.updated_at",
            (self.worker_call_id, self._worker_pid, self._worker_started_at, now),
        )

    def unregister_worker(self) -> None:
        """Drop this worker's row on clean shutdown (concurrency plan 3.3).

        Best-effort: a failed delete is not a reason to refuse to exit, and
        recovery expires a stale row on its own within the staleness window.
        """
        try:
            self._connection.execute(
                "DELETE FROM workers WHERE worker_call_id = ?", (self.worker_call_id,)
            )
        except Exception:  # noqa: BLE001, S110 - shutdown must not be blocked by bookkeeping
            pass

    def heartbeat_age_ms(self) -> int | None:
        """Milliseconds since the last heartbeat, or None if never written."""
        row = self._connection.execute(
            "SELECT updated_at FROM worker_heartbeat WHERE id = 1"
        ).fetchone()
        return None if row is None else max(0, _now_ms() - row[0])

    def recover_stale_processing_jobs(self) -> int:
        """Fail jobs whose owning worker is gone (concurrency plan 3.3).

        Ownership, not startup, decides: a `processing` row is orphaned only
        when it has no owner at all (a legacy row from before
        `worker_call_id` was recorded) or its owner has no live row in
        `workers` — so a second worker starting up never fails the job a live
        peer is in the middle of. Idempotent, so running it from every
        worker's heartbeat tick is harmless.
        """
        now = _now_ms()
        cutoff = now - WORKER_STALE_MS
        cursor = self._connection.execute(
            "UPDATE jobs SET status = 'failed', error_code = ?, "
            "error_message_public = ?, diagnostic_reference = ?, completed_at = ?, updated_at = ? "
            "WHERE status = 'processing' AND (worker_call_id IS NULL OR worker_call_id NOT IN "
            "(SELECT worker_call_id FROM workers WHERE updated_at >= ?))",
            (
                ErrorCode.UNKNOWN.value,
                "Processing was interrupted when the worker restarted. Try again.",
                _diagnostic_reference(),
                now,
                now,
                cutoff,
            ),
        )
        returned = cursor.rowcount
        self._prune_stale_workers(cutoff)
        return returned

    def _prune_stale_workers(self, cutoff: int) -> None:
        """Drop worker rows past the staleness window.

        The jobs UPDATE above already ran, so a pruned owner's processing job
        has been failed by now — deleting the row cannot hide it. Bookkeeping
        only, therefore best-effort: a locked database must not turn recovery
        into a startup failure.
        """
        try:
            self._connection.execute("DELETE FROM workers WHERE updated_at < ?", (cutoff,))
        except Exception:  # noqa: BLE001, S110 - hygiene, retried next tick
            pass
