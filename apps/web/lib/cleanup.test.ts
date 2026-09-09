import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { after, before, describe, it } from "node:test";

/**
 * Cleanup and retention tests (plan Task 13 / Section 19.3).
 * Uses a temporary data directory and the real local storage adapter.
 *
 * test-env must be imported FIRST: it points STEMIFY_DATA_DIR at a fresh
 * temp directory before the @/lib modules (and the `db` singleton) load.
 */
import "./test-env";
import { db, closeDatabase } from "@/lib/db/client";
import {
  deleteExpiredJobResults,
  deleteExpiredUploads,
  deleteOrphanedUploadObjects,
  markExpiredJobs,
  runCleanup,
} from "@/lib/cleanup";
import { LocalStorage } from "@/lib/storage/local";
import { __setStorageForTests } from "@/lib/storage";

const DATA_DIR = process.env.STEMIFY_DATA_DIR as string;

const HOUR = 60 * 60 * 1000;
const NOW = Date.now();

function insertJob(id: string, opts: { status: string; expiresAt: number | null }): void {
  db.run(
    `INSERT OR REPLACE INTO jobs (id, owner_key, source_type, mode, output_format, status, expires_at)
     VALUES (?, 'cleanup-owner', 'upload', 'vocals_instrumental', 'mp3', ?, ?)`,
    id,
    opts.status,
    opts.expiresAt,
  );
}

function insertUpload(id: string, opts: { expiresAt: number | null; withObject: boolean }): string {
  const objectKey = `sources/${id}/song.mp3`;
  if (opts.withObject) {
    // Written async by the caller before cleanup runs; the test that needs it
    // seeds the object explicitly with putObject.
    void objectKey;
  }
  db.run(
    `INSERT OR REPLACE INTO uploads (id, owner_key, filename, object_key, size_bytes, expires_at)
     VALUES (?, 'cleanup-owner', 'song.mp3', ?, 16, ?)`,
    id,
    objectKey,
    opts.expiresAt,
  );
  return objectKey;
}

describe("cleanup (Task 13)", () => {
  before(() => {
    __setStorageForTests(new LocalStorage());
  });

  after(() => {
    db.run("DELETE FROM jobs WHERE owner_key = 'cleanup-owner'");
    db.run("DELETE FROM uploads WHERE owner_key = 'cleanup-owner'");
    closeDatabase();
  });

  it("marks only expired terminal jobs as expired", () => {
    insertJob("job_expired_completed", { status: "completed", expiresAt: NOW - HOUR });
    insertJob("job_fresh_completed", { status: "completed", expiresAt: NOW + HOUR });
    insertJob("job_no_expiry", { status: "completed", expiresAt: null });
    insertJob("job_expired_processing", { status: "processing", expiresAt: NOW - HOUR });

    const marked = markExpiredJobs(NOW);
    assert.equal(marked, 1);
    const status = (id: string) =>
      db.get<{ status: string }>("SELECT status FROM jobs WHERE id = ?", id)?.status;
    assert.equal(status("job_expired_completed"), "expired");
    assert.equal(status("job_fresh_completed"), "completed");
    assert.equal(status("job_no_expiry"), "completed");
    // A processing job is never expired by cleanup; stale recovery owns it.
    assert.equal(status("job_expired_processing"), "processing");
  });

  it("deletes expired result directories and output rows, idempotently", async () => {
    const jobId = "job_expired_completed";
    const resultsDir = path.join(DATA_DIR, "results", jobId);
    await mkdir(resultsDir, { recursive: true });
    await writeFile(path.join(resultsDir, "vocals.mp3"), Buffer.alloc(32));
    db.run(
      `INSERT OR REPLACE INTO job_outputs (id, job_id, stem_key, label, relative_path, mime_type)
       VALUES ('out_1', ?, 'vocals', 'Vocals', 'results/x/vocals.mp3', 'audio/mpeg')`,
      jobId,
    );

    const deleted = await deleteExpiredJobResults(NOW);
    assert.ok(deleted >= 1);
    assert.equal(
      db.get<{ n: number }>("SELECT COUNT(*) AS n FROM job_outputs WHERE job_id = ?", jobId)?.n,
      0,
    );

    // Second run: nothing left to delete, no error.
    const again = await deleteExpiredJobResults(NOW);
    assert.equal(again, 0);
  });

  it("deletes expired uploads and their objects, idempotently", async () => {
    const uploadId = `upl_${"a".repeat(32)}`;
    const objectKey = insertUpload(uploadId, { expiresAt: NOW - HOUR, withObject: false });
    const storage = new LocalStorage();
    await storage.putObject(objectKey, Buffer.alloc(16));

    const deleted = await deleteExpiredUploads(NOW);
    assert.equal(deleted, 1);
    assert.equal(await storage.headObject(objectKey).then((i) => i.exists), false);
    assert.equal(db.get<{ n: number }>("SELECT COUNT(*) AS n FROM uploads WHERE id = ?", uploadId)?.n, 0);

    const again = await deleteExpiredUploads(NOW);
    assert.equal(again, 0);
  });

  it("deletes orphaned source directories with no uploads row or job reference", async () => {
    const orphan = `upl_${"b".repeat(32)}`;
    const referenced = `upl_${"c".repeat(32)}`;
    await mkdir(path.join(DATA_DIR, "sources", orphan), { recursive: true });
    await writeFile(path.join(DATA_DIR, "sources", orphan, "song.mp3"), Buffer.alloc(8));
    await mkdir(path.join(DATA_DIR, "sources", referenced), { recursive: true });
    await writeFile(path.join(DATA_DIR, "sources", referenced, "song.mp3"), Buffer.alloc(8));
    // The referenced one has an uploads row (still live).
    db.run(
      `INSERT OR REPLACE INTO uploads (id, owner_key, filename, object_key, size_bytes, expires_at)
       VALUES (?, 'cleanup-owner', 'song.mp3', ?, 8, ?)`,
      referenced,
      `sources/${referenced}/song.mp3`,
      NOW + HOUR,
    );

    const deleted = await deleteOrphanedUploadObjects();
    assert.ok(deleted >= 1);
    assert.equal(
      await stat2(path.join(DATA_DIR, "sources", orphan)),
      false,
      "orphan directory must be deleted",
    );
    assert.equal(
      await stat2(path.join(DATA_DIR, "sources", referenced)),
      true,
      "referenced directory must survive",
    );
  });

  it("runCleanup reports counts and is safe to repeat", async () => {
    const report = await runCleanup(NOW);
    assert.equal(typeof report.expiredJobsMarked, "number");
    assert.equal(typeof report.expiredUploadsDeleted, "number");
    assert.deepEqual(report.errors, []);

    const second = await runCleanup(NOW);
    assert.equal(second.expiredJobsMarked, 0);
  });
});

async function stat2(target: string): Promise<boolean> {
  const { stat } = await import("node:fs/promises");
  try {
    await stat(target);
    return true;
  } catch {
    return false;
  }
}
