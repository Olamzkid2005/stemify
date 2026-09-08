import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";

/**
 * Active-job limit tests (plan Task 13 / Section 15: MAX_ACTIVE_JOBS).
 * Uses the in-memory fake storage adapter and the shared local database.
 */
import { db, closeDatabase } from "@/lib/db/client";
import { FakeStorage } from "@/lib/storage/fake";
import { __setStorageForTests } from "@/lib/storage";
import { createJob } from "@/lib/jobs";

process.env.JOB_ACCESS_TOKEN_SECRET ??= "test-secret-for-local-tests-only";

const OWNER = "gid_limitowner0000000001";

function makeBody(index: number): Record<string, unknown> {
  const uploadId = `upl_${index.toString(16).padStart(32, "0")}`;
  const objectKey = `sources/${uploadId}/song.mp3`;
  db.run(
    `INSERT OR REPLACE INTO uploads (id, owner_key, filename, object_key, size_bytes, expires_at)
     VALUES (?, ?, 'song.mp3', ?, 2048, ?)`,
    uploadId,
    OWNER,
    objectKey,
    Date.now() + 60_000,
  );
  (storage as FakeStorage).put(objectKey, Buffer.alloc(2048));
  return {
    source: { type: "upload", uploadId, objectKey, filename: "song.mp3" },
    mode: "vocals_instrumental",
    outputFormat: "mp3",
    idempotencyKey: `limit-key-${index.toString().padStart(20, "0")}`,
  };
}

let storage: FakeStorage;

describe("active job limit", () => {
  before(() => {
    storage = new FakeStorage();
    __setStorageForTests(storage);
    process.env.MAX_ACTIVE_JOBS = "1";
  });

  after(() => {
    delete process.env.MAX_ACTIVE_JOBS;
    db.run("DELETE FROM jobs WHERE owner_key = ?", OWNER);
    db.run("DELETE FROM uploads WHERE owner_key = ?", OWNER);
    closeDatabase();
  });

  it("accepts the first job and rejects a second while one is active", async () => {
    const first = await createJob({ ownerKey: OWNER, body: makeBody(1) });
    assert.equal(first.ok, true);
    if (first.ok) assert.equal(first.status, 201);

    const second = await createJob({ ownerKey: OWNER, body: makeBody(2) });
    assert.equal(second.ok, false);
    if (!second.ok) {
      assert.equal(second.status, 429);
      assert.equal(second.error, "too_many_active_jobs");
    }
  });

  it("allows new jobs once the active one reaches a terminal state", async () => {
    db.run("UPDATE jobs SET status = 'completed' WHERE owner_key = ?", OWNER);
    const next = await createJob({ ownerKey: OWNER, body: makeBody(3) });
    assert.equal(next.ok, true);
    if (next.ok) assert.equal(next.status, 201);
  });

  it("counts queued and processing but not failed jobs", async () => {
    db.run("UPDATE jobs SET status = 'failed' WHERE owner_key = ? AND status = 'queued'", OWNER);
    // No active jobs remain (completed + failed are terminal).
    const next = await createJob({ ownerKey: OWNER, body: makeBody(4) });
    assert.equal(next.ok, true);
  });
});
