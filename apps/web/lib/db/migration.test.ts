import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import sqlite3 from "node:sqlite";
import { describe, it } from "node:test";

import "../test-env";

import { LocalDatabase } from "./client";

/**
 * Regression tests for the jobs-table rebuild migration (roadmap Phase B).
 *
 * History: with SQLite's default ALTER TABLE rename behavior, rebuilding the
 * legacy `jobs` table (2-value mode CHECK) rewrote job_outputs' `REFERENCES
 * jobs` clause to point at the temporary jobs_old table, and the subsequent
 * DROP left it dangling — every later insert into job_outputs failed with
 * "no such table: main.jobs_old". These tests pin the fixed behavior on the
 * web side, mirroring worker/tests/test_database_migration.py.
 */

const LEGACY_JOB_OUTPUTS_DDL = `
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
`;

function makeLegacyDb(dataDir: string): void {
  const { DatabaseSync } = sqlite3;
  const con = new DatabaseSync(path.join(dataDir, "stemify.sqlite3"));
  con.exec("PRAGMA foreign_keys = ON;");
  // Legacy jobs table: the current DDL with the mode CHECK narrowed back to
  // the two pre-drum_breakdown values (fabricated inline; the point is that
  // it lacks "drum_breakdown", which is what triggers the rebuild).
  con.exec(`
    CREATE TABLE jobs (
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
      progress INTEGER NOT NULL DEFAULT 0,
      cancel_requested INTEGER NOT NULL DEFAULT 0,
      worker_call_id TEXT,
      idempotency_key_hash TEXT,
      error_code TEXT,
      error_message_public TEXT,
      diagnostic_reference TEXT,
      created_at INTEGER NOT NULL DEFAULT 0,
      started_at INTEGER,
      completed_at INTEGER,
      expires_at INTEGER,
      updated_at INTEGER NOT NULL DEFAULT 0
    );
  `);
  con.exec(LEGACY_JOB_OUTPUTS_DDL);
  con.prepare(
    `INSERT INTO jobs (id, owner_key, source_type, mode, output_format, status)
     VALUES ('job_legacy', 'owner', 'upload', 'vocals_instrumental', 'mp3', 'completed')`,
  ).run();
  con.prepare(
    `INSERT INTO job_outputs (id, job_id, stem_key, label, relative_path, mime_type)
     VALUES ('out_legacy', 'job_legacy', 'vocals', 'Vocals', 'r/v.mp3', 'audio/mpeg')`,
  ).run();
  con.close();
}

describe("jobs-mode migration (roadmap Phase B)", () => {
  it("rebuild preserves job_outputs' foreign key and existing rows", () => {
    const dataDir = mkdtempSync(path.join(tmpdir(), "stemify-migration-"));
    makeLegacyDb(dataDir);

    const db = new LocalDatabase(dataDir);
    try {
      const ddl = db.get<{ sql: string }>(
        "SELECT sql FROM sqlite_master WHERE name = 'job_outputs'",
      )?.sql;
      assert.ok(ddl);
      assert.ok(!ddl.includes("jobs_old"), "job_outputs must not reference jobs_old");
      assert.ok(ddl.includes("REFERENCES jobs"));

      const job = db.get<{ id: string; mode: string }>(
        "SELECT id, mode FROM jobs WHERE id = 'job_legacy'",
      );
      assert.equal(job?.id, "job_legacy");
      assert.equal(job?.mode, "vocals_instrumental");

      assert.deepEqual(db.all("PRAGMA foreign_key_check"), []);
      // PRAGMA result columns are named after the pragma itself.
      assert.equal(db.get<{ foreign_keys: number }>("PRAGMA foreign_keys")?.foreign_keys, 1);
    } finally {
      db.close();
    }
  });

  it("rebuilt table accepts drum_breakdown and job_outputs inserts work", () => {
    const dataDir = mkdtempSync(path.join(tmpdir(), "stemify-migration-"));
    makeLegacyDb(dataDir);

    const db = new LocalDatabase(dataDir);
    try {
      db.run(
        `INSERT INTO jobs (id, owner_key, source_type, mode, output_format, status)
         VALUES ('job_new', 'owner', 'upload', 'drum_breakdown', 'mp3', 'completed')`,
      );
      // The exact statement that failed with "no such table: main.jobs_old".
      db.run(
        `INSERT INTO job_outputs (id, job_id, stem_key, label, relative_path, mime_type)
         VALUES ('out_new', 'job_new', 'drums', 'Drums', 'r/d.mp3', 'audio/mpeg')`,
      );
    } finally {
      db.close();
    }
  });

  it("a fresh database is created on the current schema with no rebuild", () => {
    const dataDir = mkdtempSync(path.join(tmpdir(), "stemify-migration-"));
    const db = new LocalDatabase(dataDir);
    try {
      const ddl = db.get<{ sql: string }>(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'",
      )?.sql;
      assert.ok(ddl?.includes("drum_breakdown"));
      db.run(
        `INSERT INTO jobs (id, owner_key, source_type, mode, output_format, status)
         VALUES ('job_fresh', 'owner', 'upload', 'drum_breakdown', 'mp3', 'queued')`,
      );
      db.run(
        `INSERT INTO job_outputs (id, job_id, stem_key, label, relative_path, mime_type)
         VALUES ('out_fresh', 'job_fresh', 'vocals', 'Vocals', 'r/v.mp3', 'audio/mpeg')`,
      );
    } finally {
      db.close();
    }
  });
});
