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
};

function positiveInt(value: string | undefined): number | null {
  if (value === undefined || value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 1 ? Math.floor(parsed) : null;
}

export function workerPoolStatus(now = Date.now()): WorkerPoolStatus {
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
  };
}
