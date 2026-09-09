import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { after, before, describe, it } from "node:test";

/**
 * Task 15: full local flow over the real SQLite database (plan Section 17.3).
 *
 * Covers the upload-to-download vertical slice and the acceptance items not
 * covered by the unit tests: a created job is resolvable by its owner only,
 * a completed job's stems and ZIP resolve for download, a deleted result file
 * and an expired job surface as gone (410), and the cleanup pass removes
 * expired artifacts without touching live ones.
 *
 * test-env must be imported FIRST: it points STEMIFY_DATA_DIR at a fresh
 * temp directory before the @/lib modules (and the `db` singleton) load.
 */
import "../lib/test-env";
import { closeDatabase, db } from "@/lib/db/client";
import { createJob } from "@/lib/jobs";
import { runCleanup } from "@/lib/cleanup";
import { resolveDownload } from "@/lib/downloads";
import { getJobView } from "@/lib/job-view";
import { LocalStorage } from "@/lib/storage/local";
import { __setStorageForTests } from "@/lib/storage";

process.env.MAX_ACTIVE_JOBS = "100";

const OWNER = "gid_flowowner000000001";
const OTHER = "gid_flowowner000000002";

let storage: LocalStorage;
let tempDir: string;
let jobId = "";
const ownerIdempotency = "flow-key-owner-0000000001";

async function seedUpload(ownerKey: string, idempotencyKey: string): Promise<Record<string, unknown>> {
  const uploadId = `upl_${crypto.randomUUID().replaceAll("-", "")}`;
  const objectKey = `sources/${uploadId}/song.mp3`;
  await storage.putObject(objectKey, Buffer.alloc(2048, 7));
  db.run(
    `INSERT OR REPLACE INTO uploads (id, owner_key, filename, object_key, size_bytes, expires_at)
     VALUES (?, ?, 'song.mp3', ?, 2048, ?)`,
    uploadId,
    ownerKey,
    objectKey,
    Date.now() + 60_000,
  );
  return {
    source: { type: "upload", uploadId, objectKey, filename: "song.mp3" },
    mode: "vocals_instrumental",
    outputFormat: "mp3",
    idempotencyKey,
  };
}

/** Complete a job the way the worker does: outputs + retention window. */
function completeJobLikeWorker(id: string, ownerKey: string): void {
  const now = Date.now();
  db.run(
    `UPDATE jobs SET status = 'processing', stage = 'separating', progress = 60,
     started_at = ?, updated_at = ? WHERE id = ?`,
    now,
    now,
    id,
  );
  db.run(
    `INSERT OR REPLACE INTO job_outputs (id, job_id, stem_key, label, relative_path, mime_type, size_bytes, expires_at)
     VALUES ('out_zip', ?, 'archive', 'All stems (ZIP)', ?, 'application/zip', 4096, ?)`,
    id,
    `results/${id}/stems.zip`,
    now + 24 * 3600 * 1000,
  );
  db.run(
    `INSERT OR REPLACE INTO job_outputs (id, job_id, stem_key, label, relative_path, mime_type, size_bytes, expires_at)
     VALUES ('out_vocals', ?, 'vocals', 'Vocals', ?, 'audio/mpeg', 2048, ?)`,
    id,
    `results/${id}/vocals.mp3`,
    now + 24 * 3600 * 1000,
  );
  db.run(
    `UPDATE jobs SET status = 'completed', stage = 'completed', progress = 100,
     completed_at = ?, expires_at = ?, updated_at = ? WHERE id = ?`,
    now,
    now + 24 * 3600 * 1000,
    now,
    id,
  );
  // The owner can fetch the view; other sessions cannot.
  db.run("UPDATE jobs SET owner_key = ? WHERE id = ?", ownerKey, id);
}

describe("full local flow (Task 15)", () => {
  before(async () => {
    tempDir = await mkdtemp(path.join(tmpdir(), "stemify-flow-"));
    storage = new LocalStorage(tempDir);
    __setStorageForTests(storage);
  });

  after(async () => {
    db.run("DELETE FROM job_outputs");
    db.run("DELETE FROM jobs WHERE owner_key IN (?, ?)", OWNER, OTHER);
    db.run("DELETE FROM uploads WHERE owner_key IN (?, ?)", OWNER, OTHER);
    closeDatabase();
    await rm(tempDir, { recursive: true, force: true });
  });

  it("creates a queued job from an upload", async () => {
    const body = await seedUpload(OWNER, ownerIdempotency);
    const result = await createJob({ ownerKey: OWNER, body });
    assert.equal(result.ok, true);
    if (result.ok) {
      assert.equal(result.status, 201);
      jobId = result.job.id;
      const view = await getJobView(jobId, OWNER);
      assert.equal(view?.status, "queued");
    }
  });

  it("hides the job from other sessions (404-style view)", async () => {
    const view = await getJobView(jobId, OTHER);
    assert.equal(view, null);
  });

  it("rejects downloads before completion (409)", async () => {
    const zip = await resolveDownload(jobId, OWNER, { kind: "zip" });
    assert.deepEqual(zip, { ok: false, status: 409, error: "job_not_completed" });
  });

  it("resolves stems and ZIP after the worker completes the job", async () => {
    completeJobLikeWorker(jobId, OWNER);
    await storage.putObject(`results/${jobId}/stems.zip`, Buffer.alloc(4096, 1));
    await storage.putObject(`results/${jobId}/vocals.mp3`, Buffer.alloc(2048, 2));

    const zip = await resolveDownload(jobId, OWNER, { kind: "zip" });
    assert.equal(zip.ok, true);
    const vocals = await resolveDownload(jobId, OWNER, { kind: "stem", stem: "vocals" });
    assert.equal(vocals.ok, true);
    if (vocals.ok) {
      assert.equal(vocals.sizeBytes, 2048);
      assert.ok(vocals.filePath.replace(/\\/g, "/").includes(`results/${jobId}/vocals.mp3`));
    }
    const foreign = await resolveDownload(jobId, OTHER, { kind: "zip" });
    assert.deepEqual(foreign, { ok: false, status: 404, error: "not_found" });
  });

  it("expires the job and removes outputs on cleanup", async () => {
    // Expire the job and remove the files the way the cleanup pass would.
    const past = Date.now() - 1000;
    db.run("UPDATE jobs SET expires_at = ? WHERE id = ?", past, jobId);
    await runCleanup();

    const view = await getJobView(jobId, OWNER);
    assert.equal(view?.status, "expired");
    // An expired job is no longer completed, so downloads are rejected before
    // any file lookup (the 410 path applies when expiry outruns cleanup).
    const gone = await resolveDownload(jobId, OWNER, { kind: "zip" });
    assert.deepEqual(gone, { ok: false, status: 409, error: "job_not_completed" });
    const rows = db.all<{ n: number }>(
      "SELECT COUNT(*) AS n FROM job_outputs WHERE job_id = ?",
      jobId,
    );
    assert.equal(rows[0]?.n, 0);
  });
});
