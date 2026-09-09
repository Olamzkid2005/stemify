import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";

/**
 * Job creation integration tests for the local SQLite queue.
 * Uses the in-memory fake storage adapter and the local database file.
 *
 * test-env must be imported FIRST: it points STEMIFY_DATA_DIR at a fresh
 * temp directory before the @/lib modules (and the `db` singleton) load.
 */
import "./test-env";
import { db, closeDatabase } from "@/lib/db/client";
import { FakeStorage } from "@/lib/storage/fake";
import { __setStorageForTests } from "@/lib/storage";
import { createJob } from "@/lib/jobs";

// These tests exercise idempotency/ownership, not the Task 13 active-job limit;
// lift the limit so multiple jobs for one owner don't trip 429s here.
process.env.MAX_ACTIVE_JOBS = "100";

const OWNER = "gid_testowner0000000001";
const UPLOAD_ID = `upl_${crypto.randomUUID().replaceAll("-", "")}`;
const OBJECT_KEY = `sources/${UPLOAD_ID}/song.mp3`;

const validBody: Record<string, unknown> = {
  source: { type: "upload", uploadId: UPLOAD_ID, objectKey: OBJECT_KEY, filename: "song.mp3" },
  mode: "vocals_instrumental",
  outputFormat: "mp3",
  idempotencyKey: "client-key-0123456789abcdef",
};

describe("createJob", () => {
  before(() => {
    const storage = new FakeStorage();
    __setStorageForTests(storage);
    storage.put(OBJECT_KEY, Buffer.alloc(2048));
    db.run(
      `INSERT OR REPLACE INTO uploads (id, owner_key, filename, object_key, size_bytes, expires_at)
       VALUES (?, ?, ?, ?, ?, ?)`,
      UPLOAD_ID,
      OWNER,
      "song.mp3",
      OBJECT_KEY,
      2048,
      Date.now() + 60_000,
    );
  });

  after(() => {
    delete process.env.MAX_ACTIVE_JOBS;
    db.run("DELETE FROM jobs WHERE owner_key = ?", OWNER);
    db.run("DELETE FROM uploads WHERE owner_key = ?", OWNER);
    closeDatabase();
  });

  it("creates a queued job for a valid uploaded object", async () => {
    const result = await createJob({ ownerKey: OWNER, body: structuredClone(validBody) });
    assert.equal(result.ok, true);
    if (result.ok) {
      assert.equal(result.status, 201);
      assert.match(result.job.id, /^job_[a-f0-9]{32}$/);
      assert.equal(result.job.status, "queued");
    }
  });

  it("returns the same job for a repeated idempotency key", async () => {
    const body = { ...structuredClone(validBody), idempotencyKey: "client-key-repeat-000000001" };
    const first = await createJob({ ownerKey: OWNER, body });
    const second = await createJob({ ownerKey: OWNER, body: structuredClone(body) });
    assert.equal(first.ok && second.ok, true);
    if (first.ok && second.ok) {
      assert.equal(first.status, 201);
      assert.equal(second.status, 200);
      assert.equal(first.job.id, second.job.id);
    }
  });

  it("rejects invalid modes and formats", async () => {
    const bad = { ...structuredClone(validBody), mode: "karaoke" };
    const result = await createJob({ ownerKey: OWNER, body: bad });
    assert.equal(result.ok, false);
    if (!result.ok) assert.equal(result.status, 400);
  });

  it("rejects uploads outside the caller's session", async () => {
    const evil = structuredClone(validBody) as { source: Record<string, unknown> };
    evil.source.uploadId = "upl_ffffffffffffffffffffffffffffffff";
    const result = await createJob({ ownerKey: OWNER, body: evil });
    assert.equal(result.ok, false);
    if (!result.ok) assert.equal(result.status, 403);
  });

  it("rejects uploads whose object never arrived", async () => {
    const missing = structuredClone(validBody) as {
      source: Record<string, unknown>;
      idempotencyKey: string;
    };
    missing.idempotencyKey = "client-key-ffffffffffffffff";
    missing.source.uploadId = `upl_${crypto.randomUUID().replaceAll("-", "")}`;
    missing.source.objectKey = `sources/${missing.source.uploadId}/song.mp3`;
    const result = await createJob({ ownerKey: OWNER, body: missing });
    assert.equal(result.ok, false);
    if (!result.ok) assert.equal(result.status, 403);
  });

  it("rejects YouTube URLs outside the allowlisted hosts", async () => {
    const evil = structuredClone(validBody) as { source: Record<string, unknown> };
    evil.source = { type: "youtube", url: "https://evil.example.com/watch?v=x" };
    const result = await createJob({ ownerKey: OWNER, body: evil });
    assert.equal(result.ok, false);
    if (!result.ok) assert.equal(result.status, 400);
  });
});
