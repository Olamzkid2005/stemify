import assert from "node:assert/strict";
import { after, afterEach, describe, it } from "node:test";

/**
 * Pool status tests (concurrency plan C4). The home page's strip is only as
 * honest as this function: how many workers it counts, which rows it refuses to
 * count, and the guidance numbers it prints.
 *
 * test-env must be imported FIRST: it points STEMIFY_DATA_DIR at a fresh temp
 * directory before the @/lib modules (and the `db` singleton) load.
 */
import "./test-env";
import { db, closeDatabase } from "@/lib/db/client";
import { DEFAULT_POOL_SIZE, RAM_PER_WORKER_MB, workerPoolStatus } from "@/lib/worker-pool";
import { WORKER_STALE_MS } from "@/lib/worker-status";

afterEach(() => {
  db.run("DELETE FROM workers");
});

after(() => {
  db.run("DELETE FROM workers");
  closeDatabase();
});

/**
 * A fixed clock: the function takes `now`, so the tests pin the boundary rather
 * than racing the wall clock.
 */
const NOW = 1_800_000_000_000;

function setWorkers(rows: Array<{ id: string; updatedAt: number }>): void {
  db.run("DELETE FROM workers");
  for (const row of rows) {
    db.run(
      "INSERT INTO workers (worker_call_id, pid, started_at, updated_at) VALUES (?, ?, ?, ?)",
      row.id,
      4242,
      NOW - 60_000,
      row.updatedAt,
    );
  }
}

function withEnv(values: Record<string, string | undefined>): () => void {
  const previous = new Map<string, string | undefined>();
  for (const [key, value] of Object.entries(values)) {
    previous.set(key, process.env[key]);
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  return () => {
    for (const [key, value] of previous) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  };
}

describe("worker pool status", () => {
  it("counts the live worker rows and ignores the ones past the window", () => {
    setWorkers([
      { id: "w1", updatedAt: NOW - 1_000 },
      { id: "w2", updatedAt: NOW - 4_000 },
      // A crashed worker: its row lingers until the worker prunes it at 30 s,
      // but the strip must not count it as running.
      { id: "w3", updatedAt: NOW - WORKER_STALE_MS - 1 },
    ]);
    assert.equal(workerPoolStatus(NOW).running, 2);
  });

  it("counts a row exactly at the window as running", () => {
    // The boundary is the same one the job view uses for "worker unavailable",
    // so the two signals on one page cannot disagree at the edge.
    setWorkers([{ id: "w1", updatedAt: NOW - WORKER_STALE_MS }]);
    assert.equal(workerPoolStatus(NOW).running, 1);
  });

  it("reports nothing running before any worker registers", () => {
    setWorkers([]);
    assert.equal(workerPoolStatus(NOW).running, 0);
  });

  it("falls back to the shipped pool size when the launcher exported nothing", () => {
    const restore = withEnv({ STEMIFY_WORKER_CONCURRENCY: undefined });
    try {
      assert.equal(workerPoolStatus(NOW).configured, DEFAULT_POOL_SIZE);
    } finally {
      restore();
    }
  });

  it("reads the configured pool size from the same variable start.sh uses", () => {
    const restore = withEnv({ STEMIFY_WORKER_CONCURRENCY: "4" });
    try {
      assert.equal(workerPoolStatus(NOW).configured, 4);
    } finally {
      restore();
    }
  });

  it("treats a nonsense pool size as the default rather than as zero workers", () => {
    // "You have 0 workers" would be worse than the default: it reads as a fault.
    for (const value of ["0", "-3", "two", "", "  "]) {
      const restore = withEnv({ STEMIFY_WORKER_CONCURRENCY: value });
      try {
        assert.equal(workerPoolStatus(NOW).configured, DEFAULT_POOL_SIZE, `for ${JSON.stringify(value)}`);
      } finally {
        restore();
      }
    }
  });

  it("omits the thread split when the launcher did not export one", () => {
    const restore = withEnv({ STEMIFY_WORKER_THREADS: undefined });
    try {
      assert.equal(workerPoolStatus(NOW).threadsPerWorker, null);
    } finally {
      restore();
    }
  });

  it("reports the thread split the launcher exported", () => {
    const restore = withEnv({ STEMIFY_WORKER_THREADS: "2" });
    try {
      assert.equal(workerPoolStatus(NOW).threadsPerWorker, 2);
    } finally {
      restore();
    }
  });

  it("sizes the RAM guidance for the configured pool, not the part that is up", () => {
    // Someone deciding whether to start a second worker needs the number for the
    // pool they configured; a crashed worker must not shrink the estimate.
    const restore = withEnv({ STEMIFY_WORKER_CONCURRENCY: "3" });
    try {
      setWorkers([{ id: "w1", updatedAt: NOW }]);
      const status = workerPoolStatus(NOW);
      assert.equal(status.running, 1);
      assert.equal(status.ramPerWorkerMb, RAM_PER_WORKER_MB);
      assert.equal(status.ramTotalMb, 3 * RAM_PER_WORKER_MB);
    } finally {
      restore();
    }
  });

  it("keeps the per-worker guidance at or above the measured footprint", () => {
    // docs/BENCHMARKS.md measures 0.9-1.0 GB peak RSS per worker. Guidance that
    // understates it is worse than no guidance, so this is a floor, not a guess.
    assert.ok(RAM_PER_WORKER_MB >= 1024, "per-worker guidance must round the measurement up");
  });
});
