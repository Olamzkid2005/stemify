/**
 * Local SQLite smoke helper for the database schema.
 *
 * Run from apps/web with: npx tsx lib/db/seed-test.ts
 */
import { randomUUID } from "node:crypto";

import { db, closeDatabase } from "./client";

const jobId = "job_seedtest000000000001";

try {
  db.run("DELETE FROM jobs WHERE id = ?", jobId);
  db.run(
    `INSERT INTO jobs (
      id, owner_key, source_type, source_filename, mode, output_format,
      status, stage, progress, idempotency_key_hash
    ) VALUES (?, ?, 'upload', ?, 'vocals_instrumental', 'mp3', 'processing', 'separating', 54, ?)`,
    jobId,
    "seed-test-owner",
    "seed-test.mp3",
    `seedtest-${randomUUID()}`,
  );
  db.run(
    `INSERT INTO job_outputs (
      id, job_id, stem_key, label, relative_path, mime_type, size_bytes, duration_seconds
    ) VALUES (?, ?, 'vocals', 'Vocals', 'jobs/job_seedtest000000000001/outputs/vocals.mp3', 'audio/mpeg', 3400000, 214.2)`,
    randomUUID(),
    jobId,
  );

  const job = db.get<{ status: string; progress: number }>(
    "SELECT status, progress FROM jobs WHERE id = ?",
    jobId,
  );
  const outputs = db.get<{ count: number }>(
    "SELECT count(*) AS count FROM job_outputs WHERE job_id = ?",
    jobId,
  );
  if (job?.status !== "processing" || job.progress !== 54 || outputs?.count !== 1) {
    throw new Error("SQLite roundtrip failed");
  }

  db.run("DELETE FROM jobs WHERE id = ?", jobId);
  const remaining = db.get<{ count: number }>(
    "SELECT count(*) AS count FROM job_outputs WHERE job_id = ?",
    jobId,
  );
  if (remaining?.count !== 0) throw new Error("cascade delete failed");

  console.log("LOCAL SQLITE SMOKE PASS");
} finally {
  closeDatabase();
}
