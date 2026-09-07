import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { after, before, describe, it } from "node:test";

/**
 * Download authorization tests (plan Task 12 acceptance): an owner can resolve
 * every completed stem and the ZIP; other sessions are indistinguishable from
 * unknown jobs (404); expired or missing results return a clear 410.
 *
 * Uses the real SQLite database and a temp LocalStorage data directory; the
 * HTTP streaming layer lives in the route and only wraps these results.
 */
import { closeDatabase, db } from "@/lib/db/client";
import {
  contentDispositionFilename,
  parseDownloadRequest,
  parseSingleRange,
  resolveDownload,
} from "@/lib/downloads";
import { LocalStorage } from "@/lib/storage/local";
import { __setStorageForTests } from "@/lib/storage";

const OWNER = "gid_testowner0000000001";
const OTHER = "gid_testowner0000000002";
const COMPLETED = `job_${"a".repeat(32)}`;
const FOREIGN = `job_${"b".repeat(32)}`;
const PROCESSING = `job_${"c".repeat(32)}`;
const EXPIRED = `job_${"d".repeat(32)}`;
const NOFILE = `job_${"e".repeat(32)}`;
const UNKNOWN = `job_${"f".repeat(32)}`;

function insertJob(id: string, ownerKey: string, status: string, expiresAt: number | null): void {
  db.run(
    `INSERT OR REPLACE INTO jobs (id, owner_key, source_type, source_filename, mode, output_format, status, expires_at)
     VALUES (?, ?, 'upload', 'song.mp3', 'vocals_instrumental', 'mp3', ?, ?)`,
    id,
    ownerKey,
    status,
    expiresAt,
  );
}

function insertOutput(
  jobId: string,
  stemKey: string,
  relativePath: string,
  expiresAt: number | null,
): void {
  db.run(
    `INSERT OR REPLACE INTO job_outputs (id, job_id, stem_key, label, relative_path, mime_type, size_bytes, expires_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
    `out_${jobId.slice(4, 12)}_${stemKey}`,
    jobId,
    stemKey,
    stemKey === "archive" ? "All stems (ZIP)" : stemKey,
    relativePath,
    stemKey === "archive" ? "application/zip" : "audio/mpeg",
    1024,
    expiresAt,
  );
}

describe("resolveDownload", () => {
  before(async () => {
    const tempDir = await mkdtemp(path.join(tmpdir(), "stemify-downloads-"));
    const storage = new LocalStorage(tempDir);
    __setStorageForTests(storage);

    insertJob(COMPLETED, OWNER, "completed", Date.now() + 60_000);
    insertOutput(COMPLETED, "vocals", `results/${COMPLETED}/vocals.mp3`, null);
    insertOutput(COMPLETED, "archive", `results/${COMPLETED}/stems.zip`, null);
    await storage.putObject(`results/${COMPLETED}/vocals.mp3`, Buffer.alloc(1024, 1));
    await storage.putObject(`results/${COMPLETED}/stems.zip`, Buffer.alloc(2048, 2));

    insertJob(FOREIGN, OTHER, "completed", Date.now() + 60_000);
    insertOutput(FOREIGN, "vocals", `results/${FOREIGN}/vocals.mp3`, null);

    insertJob(PROCESSING, OWNER, "processing", null);
    insertJob(EXPIRED, OWNER, "completed", Date.now() - 60_000);
    insertOutput(EXPIRED, "vocals", `results/${EXPIRED}/vocals.mp3`, null);
    insertJob(NOFILE, OWNER, "completed", Date.now() + 60_000);
    insertOutput(NOFILE, "vocals", `results/${NOFILE}/vocals.mp3`, null);
  });

  after(async () => {
    db.run("DELETE FROM job_outputs WHERE job_id IN (?, ?, ?, ?, ?)", COMPLETED, FOREIGN, PROCESSING, EXPIRED, NOFILE);
    db.run("DELETE FROM jobs WHERE id IN (?, ?, ?, ?, ?)", COMPLETED, FOREIGN, PROCESSING, EXPIRED, NOFILE);
    closeDatabase();
  });

  it("resolves a completed stem for the owning session", async () => {
    const result = await resolveDownload(COMPLETED, OWNER, { kind: "stem", stem: "vocals" });
    assert.equal(result.ok, true);
    if (result.ok) {
      assert.equal(result.sizeBytes, 1024);
      assert.ok(result.filePath.replace(/\\/g, "/").includes(`results/${COMPLETED}/vocals.mp3`));
    }
  });

  it("resolves the ZIP through the archive output", async () => {
    const result = await resolveDownload(COMPLETED, OWNER, { kind: "zip" });
    assert.equal(result.ok, true);
    if (result.ok) {
      assert.equal(result.output.stem_key, "archive");
      assert.equal(result.output.mime_type, "application/zip");
    }
  });

  it("treats another session's job as unknown (404)", async () => {
    const result = await resolveDownload(FOREIGN, OWNER, { kind: "stem", stem: "vocals" });
    assert.deepEqual(result, { ok: false, status: 404, error: "not_found" });
  });

  it("returns 404 for unknown jobs", async () => {
    const result = await resolveDownload(UNKNOWN, OWNER, { kind: "zip" });
    assert.deepEqual(result, { ok: false, status: 404, error: "not_found" });
  });

  it("rejects jobs without completed outputs (409)", async () => {
    const result = await resolveDownload(PROCESSING, OWNER, { kind: "zip" });
    assert.deepEqual(result, { ok: false, status: 409, error: "job_not_completed" });
  });

  it("returns 410 for expired jobs", async () => {
    const result = await resolveDownload(EXPIRED, OWNER, { kind: "stem", stem: "vocals" });
    assert.deepEqual(result, { ok: false, status: 410, error: "results_expired" });
  });

  it("returns 410 when the result file is missing", async () => {
    const result = await resolveDownload(NOFILE, OWNER, { kind: "stem", stem: "vocals" });
    assert.deepEqual(result, { ok: false, status: 410, error: "result_unavailable" });
  });

  it("rejects stem keys outside the allowlist before any lookup", async () => {
    const result = await resolveDownload(COMPLETED, OWNER, { kind: "stem", stem: "archive" });
    assert.deepEqual(result, { ok: false, status: 400, error: "invalid_request" });
  });
});

describe("parseDownloadRequest", () => {
  it("accepts kind=zip and kind=stem with a simple stem key", () => {
    assert.deepEqual(parseDownloadRequest(new URLSearchParams("kind=zip")), { kind: "zip" });
    assert.deepEqual(parseDownloadRequest(new URLSearchParams("kind=stem&stem=vocals")), {
      kind: "stem",
      stem: "vocals",
    });
  });

  it("rejects path-like or malformed input", () => {
    assert.equal(parseDownloadRequest(new URLSearchParams("kind=stem")), null);
    assert.equal(parseDownloadRequest(new URLSearchParams("kind=stem&stem=../../secret")), null);
    assert.equal(parseDownloadRequest(new URLSearchParams("kind=stem&stem=archive.zip")), null);
    assert.equal(parseDownloadRequest(new URLSearchParams("kind=file")), null);
  });
});

describe("parseSingleRange", () => {
  const size = 100;

  it("parses start-end, open-ended, and suffix ranges", () => {
    assert.deepEqual(parseSingleRange("bytes=0-49", size), { start: 0, end: 49 });
    assert.deepEqual(parseSingleRange("bytes=50-", size), { start: 50, end: 99 });
    assert.deepEqual(parseSingleRange("bytes=-10", size), { start: 90, end: 99 });
    assert.deepEqual(parseSingleRange("bytes=95-120", size), { start: 95, end: 99 });
  });

  it("flags unsatisfiable ranges as invalid (416)", () => {
    assert.equal(parseSingleRange("bytes=100-", size), "invalid");
    assert.equal(parseSingleRange("bytes=-0", size), "invalid");
  });

  it("ignores absent and multi-range headers (full body)", () => {
    assert.equal(parseSingleRange(null, size), null);
    assert.equal(parseSingleRange("bytes=0-1,5-9", size), null);
  });
});

describe("contentDispositionFilename", () => {
  it("builds safe ASCII filenames from the stored output", () => {
    const output = {
      stem_key: "vocals",
      relative_path: "results/x/vocals.mp3",
    } as never;
    const archive = {
      stem_key: "archive",
      relative_path: "results/x/stems.zip",
    } as never;
    const jobId = "job_" + "a".repeat(32);
    assert.equal(contentDispositionFilename(output, jobId), `vocals-${jobId}.mp3`);
    assert.equal(contentDispositionFilename(archive, jobId), `stemify-${jobId}.zip`);
  });
});
