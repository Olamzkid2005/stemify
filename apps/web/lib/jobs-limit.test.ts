import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";

/**
 * Active-job limit tests (plan Task 13 / Section 15: MAX_ACTIVE_JOBS).
 * Uses the in-memory fake storage adapter and the shared local database.
 *
 * test-env must be imported FIRST: it points STEMIFY_DATA_DIR at a fresh
 * temp directory before the @/lib modules (and the `db` singleton) load.
 */
import "./test-env";
import { db, closeDatabase } from "@/lib/db/client";
import { FakeStorage } from "@/lib/storage/fake";
import { __setStorageForTests } from "@/lib/storage";
import { createJob, DEFAULT_ACTIVE_JOB_LIMIT, activeJobLimit } from "@/lib/jobs";

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
    // The database is closed by the last suite in this file, so the suites
    // below can keep using it (each file gets its own temp data directory).
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

describe("active job limit default (concurrency plan C3)", () => {
  it("defaults to the pool plus one waiting job when unset", () => {
    delete process.env.MAX_ACTIVE_JOBS;
    assert.equal(activeJobLimit(), DEFAULT_ACTIVE_JOB_LIMIT);
    assert.equal(DEFAULT_ACTIVE_JOB_LIMIT, 3);
  });

  it("falls back to the default for values that cannot be a cap", () => {
    for (const value of ["", "abc", "0", "-2", NaN.toString()]) {
      process.env.MAX_ACTIVE_JOBS = value;
      assert.equal(activeJobLimit(), DEFAULT_ACTIVE_JOB_LIMIT, `value ${value}`);
    }
    delete process.env.MAX_ACTIVE_JOBS;
  });

  it("never grants more than an explicit cap asks for", () => {
    process.env.MAX_ACTIVE_JOBS = "4";
    assert.equal(activeJobLimit(), 4);
    process.env.MAX_ACTIVE_JOBS = "2.9";
    assert.equal(activeJobLimit(), 2);
    delete process.env.MAX_ACTIVE_JOBS;
  });
});

describe("a browser may run a full pool (concurrency plan C3)", () => {
  const OWNER_POOL = "gid_poolowner00000000001";

  before(() => {
    storage = new FakeStorage();
    __setStorageForTests(storage);
  });

  after(() => {
    delete process.env.MAX_ACTIVE_JOBS;
    db.run("DELETE FROM jobs WHERE owner_key = ?", OWNER_POOL);
    db.run("DELETE FROM uploads WHERE owner_key = ?", OWNER_POOL);
    closeDatabase();
  });

  function poolBody(index: number): Record<string, unknown> {
    const uploadId = `upl_${(index + 0x100).toString(16).padStart(32, "0")}`;
    const objectKey = `sources/${uploadId}/song.mp3`;
    db.run(
      `INSERT OR REPLACE INTO uploads (id, owner_key, filename, object_key, size_bytes, expires_at)
       VALUES (?, ?, 'song.mp3', ?, 2048, ?)`,
      uploadId,
      OWNER_POOL,
      objectKey,
      Date.now() + 60_000,
    );
    (storage as FakeStorage).put(objectKey, Buffer.alloc(2048));
    return {
      source: { type: "upload", uploadId, objectKey, filename: "song.mp3" },
      mode: "vocals_instrumental",
      outputFormat: "mp3",
      idempotencyKey: `pool-key-${index.toString().padStart(20, "0")}`,
    };
  }

  it("accepts one job per pool slot plus one waiting, then refuses", async () => {
    delete process.env.MAX_ACTIVE_JOBS;
    for (let index = 1; index <= DEFAULT_ACTIVE_JOB_LIMIT; index += 1) {
      const accepted = await createJob({ ownerKey: OWNER_POOL, body: poolBody(index) });
      assert.equal(accepted.ok, true, `job ${index} should be accepted`);
    }
    const refused = await createJob({ ownerKey: OWNER_POOL, body: poolBody(99) });
    assert.equal(refused.ok, false);
    if (!refused.ok) assert.equal(refused.status, 429);
  });

  it("the accepted jobs are exactly the ones a parallel pool can work", async () => {
    const row = db.get<{ n: number }>(
      "SELECT COUNT(*) AS n FROM jobs WHERE owner_key = ? AND status IN ('queued', 'processing')",
      OWNER_POOL,
    );
    assert.equal(row?.n, DEFAULT_ACTIVE_JOB_LIMIT);
  });
});
