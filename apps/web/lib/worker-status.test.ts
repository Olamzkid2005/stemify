import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";

/**
 * Worker liveness tests (plan Task 13 / Section 19.2): the heartbeat row the
 * worker writes, the web-side staleness check, and the job-view mapping.
 *
 * test-env must be imported FIRST: it points STEMIFY_DATA_DIR at a fresh
 * temp directory before the @/lib modules (and the `db` singleton) load.
 */
import "./test-env";
import { db, closeDatabase } from "@/lib/db/client";
import { workerStatus, WORKER_STALE_MS } from "@/lib/worker-status";
import { getJobView } from "@/lib/job-view";

const OWNER = "gid_heartbeatowner0001";

function insertJob(id: string, status: string): void {
  db.run(
    `INSERT OR REPLACE INTO jobs (id, owner_key, source_type, mode, output_format, status)
     VALUES (?, ?, 'upload', 'vocals_instrumental', 'mp3', ?)`,
    id,
    OWNER,
    status,
  );
}

describe("worker status", () => {
  before(() => {
    insertJob("job_active_1", "processing");
    insertJob("job_done_1", "completed");
  });

  after(() => {
    db.run("DELETE FROM jobs WHERE owner_key = ?", OWNER);
    closeDatabase();
  });

  it("reports not running when no heartbeat row exists", () => {
    // Other test files share this database; make the precondition explicit.
    db.run("DELETE FROM worker_heartbeat WHERE id = 1");
    const status = workerStatus();
    assert.equal(status.running, false);
    assert.equal(status.lastSeenAt, null);
  });

  it("reports running for a fresh heartbeat", () => {
    db.run("INSERT OR REPLACE INTO worker_heartbeat (id, updated_at) VALUES (1, ?)", Date.now());
    assert.equal(workerStatus().running, true);
  });

  it("reports not running for a stale heartbeat", () => {
    db.run(
      "INSERT OR REPLACE INTO worker_heartbeat (id, updated_at) VALUES (1, ?)",
      Date.now() - WORKER_STALE_MS - 1000,
    );
    assert.equal(workerStatus().running, false);
  });

  it("exposes workerRunning only for active jobs", async () => {
    // Fresh heartbeat so the active job reports running=true.
    db.run("INSERT OR REPLACE INTO worker_heartbeat (id, updated_at) VALUES (1, ?)", Date.now());
    const active = await getJobView("job_active_1", OWNER);
    assert.equal(active?.workerRunning, true);

    const done = await getJobView("job_done_1", OWNER);
    assert.equal(done?.workerRunning, undefined);
  });

  it("exposes workerRunning=false for an active job with a stale heartbeat", async () => {
    db.run(
      "INSERT OR REPLACE INTO worker_heartbeat (id, updated_at) VALUES (1, ?)",
      Date.now() - WORKER_STALE_MS - 1000,
    );
    const active = await getJobView("job_active_1", OWNER);
    assert.equal(active?.workerRunning, false);
  });
});
