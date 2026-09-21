import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it } from "node:test";

/** Installs the jsdom globals React's client renderer needs (must be first). */
import "@/lib/test-dom";

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { JobsProvider } from "@/components/jobs-context";
import { YourJobs } from "@/components/your-jobs";
import type { JobListItem, WorkerPoolInfo } from "@/lib/job-list-types";

/**
 * Home-page job list (concurrency plan C4). Covers the client half: one shared
 * poller feeding the list, and the strings a person actually reads — where a
 * waiting job sits, and the progress of the running one.
 */

const T0 = "2026-09-06T00:00:00.000Z";

function item(patch: Partial<JobListItem> & { jobId: string; status: string }): JobListItem {
  return {
    userStage: "Preparing audio",
    progressMessage: "Waiting for a worker",
    progress: 0,
    mode: "vocals_instrumental",
    outputFormat: "mp3",
    source: { type: "upload", filename: "song.mp3" },
    createdAt: T0,
    updatedAt: T0,
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

/** The pool block rides on this response too (lib/worker-pool.ts). */
const POOL: WorkerPoolInfo = {
  configured: 2,
  running: 2,
  threadsPerWorker: 2,
  ramPerWorkerMb: 1024,
  ramTotalMb: 2048,
  freeRamMb: 8192,
};

function respondWith(jobs: JobListItem[]): void {
  globalThis.fetch = (() =>
    Promise.resolve({
      ok: true,
      status: 200,
      json: async () => ({ jobs, pool: POOL }),
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
        <YourJobs />
      </JobsProvider>,
    );
  });
  // Flush the first poll.
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  return container;
}

describe("YourJobs", () => {
  it("renders nothing at all before any job exists", async () => {
    respondWith([]);
    const container = await render();
    assert.equal(container.textContent, "");
  });

  it("says a job is not running yet, and how many are ahead of it", async () => {
    respondWith([
      item({ jobId: "job_a", status: "processing", userStage: "Separating stems", progress: 42 }),
      item({ jobId: "job_b", status: "queued", queuePosition: 3 }),
    ]);
    const container = await render();

    assert.ok(container.textContent?.includes("Your jobs"));
    assert.ok(container.textContent?.includes("2 in progress"));
    assert.ok(container.textContent?.includes("Queued — 2 jobs ahead"));
    assert.ok(container.textContent?.includes("Separating stems"));
    assert.ok(container.textContent?.includes("42%"));
    // Every row links to its own job page.
    const hrefs = Array.from(container.querySelectorAll("a")).map((link) => link.getAttribute("href"));
    assert.deepEqual(hrefs, ["/jobs/job_a", "/jobs/job_b"]);
  });

  it("calls the front of the queue next in line, not next to run", async () => {
    respondWith([item({ jobId: "job_c", status: "queued", queuePosition: 1 })]);
    const container = await render();
    assert.ok(container.textContent?.includes("Queued — next in line"));
  });

  it("shows a finished job as ready with its format, and no progress bar", async () => {
    respondWith([
      item({
        jobId: "job_d",
        status: "completed",
        userStage: "Completed",
        progressMessage: "Your stems are ready",
        progress: 100,
        mode: "full_stems",
        outputFormat: "wav",
      }),
    ]);
    const container = await render();

    assert.ok(container.textContent?.includes("Ready"));
    assert.ok(container.textContent?.includes("3 stems"));
    assert.ok(container.textContent?.includes("WAV"));
    // A finished job has no moving bar: the width is only rendered while active.
    assert.equal(container.querySelector(".bg-gradient-to-r"), null);
  });

  it("keeps the last known rows when the list cannot be loaded", async () => {
    globalThis.fetch = (() =>
      Promise.resolve({ ok: false, status: 500, json: async () => ({}) } as unknown as Response)) as typeof fetch;
    const container = await render();
    assert.ok(container.textContent?.includes("Could not load your jobs"));
  });
});
