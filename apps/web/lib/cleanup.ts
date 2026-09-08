/**
 * Local cleanup and retention (plan Task 13 / Section 19.3).
 *
 * One idempotent pass that:
 *  1. Finds expired jobs and abandoned uploads.
 *  2. Deletes their source and result files.
 *  3. Removes orphaned upload objects with no database row.
 *  4. Marks metadata expired or deleted.
 *
 * Never touches `data/models/` — model checkpoints are not job data.
 * Safe to run repeatedly: every step tolerates already-deleted state.
 */
import { readdir, rm, stat } from "node:fs/promises";
import path from "node:path";

import { db } from "@/lib/db/client";
import { getLocalStorage } from "@/lib/storage";

export type CleanupReport = {
  expiredJobsMarked: number;
  expiredUploadsDeleted: number;
  orphanedUploadObjectsDeleted: number;
  resultDirectoriesDeleted: number;
  errors: string[];
};

function nowMs(): number {
  return Date.now();
}

/** Mark jobs past their retention window as expired (terminal, never re-claimed). */
export function markExpiredJobs(now = nowMs()): number {
  const result = db.run(
    `UPDATE jobs SET status = 'expired', updated_at = ?
     WHERE expires_at IS NOT NULL AND expires_at < ? AND status IN ('completed', 'failed', 'canceled')`,
    now,
    now,
  );
  return result.changes;
}

/** Delete result files of expired jobs, then their job_outputs rows. */
export async function deleteExpiredJobResults(now = nowMs()): Promise<number> {
  const storage = getLocalStorage();
  const rows = db.all<{ id: string }>(
    `SELECT id FROM jobs WHERE expires_at IS NOT NULL AND expires_at < ? AND status = 'expired'`,
    now,
  );
  let deleted = 0;
  for (const row of rows) {
    const resultsDir = path.join(storage.dataDirectory, "results", row.id);
    try {
      // Count only directories that actually existed so repeat runs report 0.
      await stat(resultsDir);
    } catch {
      continue;
    }
    try {
      await rm(resultsDir, { recursive: true, force: true });
      deleted += 1;
    } catch {
      // Deletion is retried on the next cleanup run.
      continue;
    }
  }
  db.run(
    `DELETE FROM job_outputs WHERE job_id IN (
       SELECT id FROM jobs WHERE expires_at IS NOT NULL AND expires_at < ? AND status = 'expired'
     )`,
    now,
  );
  return deleted;
}

/** Delete expired uploads: the stored object, then the row. */
export async function deleteExpiredUploads(now = nowMs()): Promise<number> {
  const storage = getLocalStorage();
  const rows = db.all<{ id: string; object_key: string }>(
    `SELECT id, object_key FROM uploads WHERE expires_at IS NOT NULL AND expires_at < ?`,
    now,
  );
  let deleted = 0;
  for (const row of rows) {
    await storage.deleteObject(row.object_key).catch(() => undefined);
    const result = db.run("DELETE FROM uploads WHERE id = ? AND expires_at < ?", row.id, now);
    deleted += result.changes;
  }
  return deleted;
}

/**
 * Delete source objects that exist on disk but have no uploads row (e.g. the
 * process died between the file write and the INSERT). Keys look like
 * `sources/upl_<32hex>/...`; anything else under sources/ is left alone.
 */
export async function deleteOrphanedUploadObjects(): Promise<number> {
  const storage = getLocalStorage();
  const sourcesDir = path.join(storage.dataDirectory, "sources");
  let entries: string[];
  try {
    entries = await readdir(sourcesDir);
  } catch {
    return 0; // no sources directory yet — nothing to do
  }

  const knownKeys = new Set(
    db.all<{ object_key: string }>("SELECT object_key FROM uploads").map((row) => row.object_key),
  );
  const activeJobKeys = new Set(
    db
      .all<{ object_key: string | null }>(
        "SELECT source_object_key AS object_key FROM jobs WHERE source_object_key IS NOT NULL",
      )
      .map((row) => row.object_key as string),
  );

  let deleted = 0;
  for (const entry of entries) {
    if (!/^upl_[a-f0-9]{32}$/.test(entry)) continue; // never touch unexpected names
    const prefix = `sources/${entry}/`;
    const hasRow = [...knownKeys].some((key) => key.startsWith(prefix));
    const hasJob = [...activeJobKeys].some((key) => key.startsWith(prefix));
    if (hasRow || hasJob) continue;
    try {
      const info = await stat(path.join(sourcesDir, entry));
      if (!info.isDirectory()) continue;
      await rm(path.join(sourcesDir, entry), { recursive: true, force: true });
      deleted += 1;
    } catch {
      continue;
    }
  }
  return deleted;
}

/** Run one full cleanup pass. Retries are safe; failures are collected, not thrown. */
export async function runCleanup(now = nowMs()): Promise<CleanupReport> {
  const errors: string[] = [];
  const expiredJobsMarked = markExpiredJobs(now);

  let resultDirectoriesDeleted = 0;
  try {
    resultDirectoriesDeleted = await deleteExpiredJobResults(now);
  } catch (error) {
    errors.push(`result cleanup: ${String(error)}`);
  }

  let expiredUploadsDeleted = 0;
  try {
    expiredUploadsDeleted = await deleteExpiredUploads(now);
  } catch (error) {
    errors.push(`upload cleanup: ${String(error)}`);
  }

  let orphanedUploadObjectsDeleted = 0;
  try {
    orphanedUploadObjectsDeleted = await deleteOrphanedUploadObjects(now);
  } catch (error) {
    errors.push(`orphan cleanup: ${String(error)}`);
  }

  return { expiredJobsMarked, expiredUploadsDeleted, orphanedUploadObjectsDeleted, resultDirectoriesDeleted, errors };
}
