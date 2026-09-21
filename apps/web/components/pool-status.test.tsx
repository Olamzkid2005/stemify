import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it } from "node:test";

/** Installs the jsdom globals React's client renderer needs (must be first). */
import "@/lib/test-dom";

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { JobsProvider } from "@/components/jobs-context";
import { PoolStatus } from "@/components/pool-status";
import type { JobListItem, WorkerPoolInfo } from "@/lib/job-list-types";

/**
 * Home-page pool strip (concurrency plan C4). The strings a person reads before
 * submitting anything: how many workers are up, how many jobs they will run at
 * once, and what the pool needs from the machine.
 */

const POOL: WorkerPoolInfo = {
  configured: 2,
  running: 2,
  threadsPerWorker: 2,
  ramPerWorkerMb: 1024,
  ramTotalMb: 2048,
};

function item(patch: Partial<JobListItem> & { jobId: string; status: string }): JobListItem {
  return {
    userStage: "Preparing audio",
    progressMessage: "Waiting for a worker",
    progress: 0,
    mode: "vocals_instrumental",
    outputFormat: "mp3",
    source: { type: "upload", filename: "song.mp3" },
    createdAt: "2026-09-06T00:00:00.000Z",
    updatedAt: "2026-09-06T00:00:00.000Z",
    statusUrl: `/api/jobs/${patch.jobId}`,
    ...patch,
  };
}

let containers: HTMLElement[];
let roots: Root[];
let originalFetch: typeof fetch;

beforeEach(() => {
  containers = [];
  roots = [];
  originalFetch = globalThis.fetch;
});

afterEach(() => {
  act(() => {
    for (const root of roots) root.unmount();
  });
  for (const container of containers) container.remove();
  globalThis.fetch = originalFetch;
});

function respondWith(body: unknown, ok = true): void {
  globalThis.fetch = (() =>
    Promise.resolve({
      ok,
      status: ok ? 200 : 500,
      json: async () => body,
    } as unknown as Response)) as typeof fetch;
}

async function render(): Promise<HTMLElement> {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  containers.push(container);
  roots.push(root);
  act(() => {
    root.render(
      <JobsProvider>
        <PoolStatus />
      </JobsProvider>,
    );
  });
  // Flush the first poll.
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  return container;
}

describe("PoolStatus", () => {
  it("renders nothing before the first poll answers", async () => {
    // "No worker running" is a claim, and it must not be made before looking.
    respondWith({ jobs: [] }, false);
    const container = await render();
    assert.equal(container.textContent, "");
  });

  it("renders nothing when the response carries no pool block", async () => {
    respondWith({ jobs: [] });
    const container = await render();
    assert.equal(container.textContent, "");
  });

  it("says the pool is ready, with what it runs and what it needs", async () => {
    respondWith({ jobs: [], pool: POOL });
    const container = await render();
    const text = container.textContent ?? "";

    assert.ok(text.includes("2 workers ready"));
    assert.ok(text.includes("up to 2 jobs at once"));
    assert.ok(text.includes("2 threads each"));
    assert.ok(text.includes("about 2 GB RAM for the pool"));
  });

  it("says how short the pool is when one worker is down", async () => {
    respondWith({ jobs: [], pool: { ...POOL, running: 1 } });
    const container = await render();
    const text = container.textContent ?? "";

    assert.ok(text.includes("1 of 2 workers running"));
    // The configured size still governs what the machine is being asked to run.
    assert.ok(text.includes("up to 2 jobs at once"));
  });

  it("says jobs are waiting when nothing is running", async () => {
    respondWith({ jobs: [item({ jobId: "job_a", status: "queued", queuePosition: 1 })], pool: { ...POOL, running: 0 } });
    const container = await render();
    const text = container.textContent ?? "";

    assert.ok(text.includes("No worker running"));
    assert.ok(text.includes("jobs stay queued until the worker starts"));
    // Guidance is about the configured pool, so it survives a crash.
    assert.ok(text.includes("about 2 GB RAM for the pool"));
  });

  it("calls a pool of one one job at a time", async () => {
    respondWith({ jobs: [], pool: { ...POOL, configured: 1, running: 1, threadsPerWorker: 4, ramTotalMb: 1024 } });
    const container = await render();
    const text = container.textContent ?? "";

    assert.ok(text.includes("1 worker ready"));
    assert.ok(text.includes("one job at a time"));
    assert.ok(text.includes("4 threads each"));
    assert.ok(text.includes("about 1 GB RAM for the pool"));
  });

  it("uses the singular for a single thread per worker", async () => {
    respondWith({ jobs: [], pool: { ...POOL, threadsPerWorker: 1 } });
    const container = await render();
    assert.ok(container.textContent?.includes("1 thread each"));
  });

  it("omits the thread split when the launcher did not export one", async () => {
    respondWith({ jobs: [], pool: { ...POOL, threadsPerWorker: null } });
    const container = await render();
    const text = container.textContent ?? "";

    assert.ok(text.includes("2 workers ready"));
    assert.equal(text.includes("thread"), false);
  });

  it("shows sub-gigabyte guidance in megabytes, not as 0 GB", async () => {
    respondWith({ jobs: [], pool: { ...POOL, configured: 1, running: 1, ramPerWorkerMb: 512, ramTotalMb: 512 } });
    const container = await render();
    assert.ok(container.textContent?.includes("about 512 MB RAM for the pool"));
  });

  it("marks the numbers as last known when a refresh fails", async () => {
    // A stale count presented as current is the one way this strip can lie.
    // An active job keeps the poller on its fast interval, so the failing
    // refresh lands in this test rather than 15 s later.
    respondWith({
      jobs: [item({ jobId: "job_a", status: "processing", userStage: "Separating stems", progress: 20 })],
      pool: POOL,
    });
    const container = await render();
    assert.equal(container.textContent?.includes("last known"), false);

    respondWith({}, false);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2700));
    });
    assert.ok(container.textContent?.includes("last known"));
    assert.ok(container.textContent?.includes("2 workers ready"));
  });
});
