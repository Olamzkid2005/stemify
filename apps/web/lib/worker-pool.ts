/**
 * Live worker pool status for the home page (concurrency plan C4).
 *
 * `worker_heartbeat` answers "is any worker up"; the `workers` table answers the
 * question a person looking at a pool actually has: how many are up, and what
 * the machine is carrying for them. Each worker upserts its own row on the same
 * 5 s tick, so counting fresh rows is counting live processes — no extra
 * liveness concept, and nothing here can disagree with the worker's own
 * recovery rule about which rows are alive.
 */
import { freemem } from "node:os";

import { db } from "@/lib/db/client";
import { WORKER_STALE_MS } from "@/lib/worker-status";

/** Pool size assumed when the launcher exported nothing (mirrors start.sh). */
export const DEFAULT_POOL_SIZE = 2;

/**
 * Memory guidance per worker, from measurement rather than arithmetic: peak RSS
 * plateaus at 0.9-1.0 GB per worker and is flat in track length, because the
 * model dominates the footprint (docs/BENCHMARKS.md). Rounded **up**, because
 * guidance that understates what a machine needs is worse than no guidance.
 */
export const RAM_PER_WORKER_MB = 1024;

export type WorkerPoolStatus = {
  /** Workers the launcher was asked to run (`STEMIFY_WORKER_CONCURRENCY`). */
  configured: number;
  /** Live worker rows right now — a row older than the staleness window does not count. */
  running: number;
  /**
   * Threads each worker may use, or null when the launcher did not export a
   * budget (a hand-run loop, or a worker started outside start.sh). Null is the
   * honest answer: this process's core count is not what those workers got.
   */
  threadsPerWorker: number | null;
  ramPerWorkerMb: number;
  /** Guidance for the configured pool, not just the part that is up. */
  ramTotalMb: number;
  /**
   * Free memory on this machine at this poll, or null when the platform would
   * not report it. A pool that does not fit in it will swap, so this is the one
   * number that can turn the guidance above into a warning.
   *
   * Read as the worker's memory: the worker runs on the same machine as this
   * app (start.sh starts both), so there is no second host to ask.
   */
  freeRamMb: number | null;
};

function positiveInt(value: string | undefined): number | null {
  if (value === undefined || value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 1 ? Math.floor(parsed) : null;
}

/**
 * Free memory in whole MB, or null when the platform will not say.
 *
 * Windows and macOS report *available* memory — free plus reclaimable cache —
 * which is what a new worker can actually be given. Linux reports MemFree
 * alone, so there the figure is conservative and the warning can fire before
 * the machine is genuinely short. That direction is deliberate: a spurious
 * "low memory" costs one glance, a missed one costs a swap-thrashed
 * separation. Injected into `workerPoolStatus` so tests pin the comparison
 * rather than the host's current load.
 */
export function systemFreeRamMb(): number | null {
  try {
    const bytes = freemem();
    return Number.isFinite(bytes) && bytes >= 0 ? Math.floor(bytes / (1024 * 1024)) : null;
  } catch {
    // A capacity hint is never worth failing the job list it rides on.
    return null;
  }
}

export function workerPoolStatus(
  now = Date.now(),
  freeRam = systemFreeRamMb(),
): WorkerPoolStatus {
  // Same staleness window the job view uses for "worker unavailable", so the
  // two signals on one page can never contradict each other.
  const row = db.get<{ n: number }>(
    "SELECT COUNT(*) AS n FROM workers WHERE updated_at >= ?",
    now - WORKER_STALE_MS,
  );
  const configured = positiveInt(process.env.STEMIFY_WORKER_CONCURRENCY) ?? DEFAULT_POOL_SIZE;
  return {
    configured,
    running: row?.n ?? 0,
    threadsPerWorker: positiveInt(process.env.STEMIFY_WORKER_THREADS),
    ramPerWorkerMb: RAM_PER_WORKER_MB,
    ramTotalMb: configured * RAM_PER_WORKER_MB,
    freeRamMb: freeRam,
  };
}
