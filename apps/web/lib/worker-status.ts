/**
 * Worker liveness (plan Task 13 / Section 19.2).
 *
 * The worker writes a heartbeat row every few seconds from a daemon thread.
 * A queued or processing job with a stale/missing heartbeat means the local
 * worker is not running; the UI shows a non-sensitive state for that.
 */
import { db } from "@/lib/db/client";

/** Older than this and the worker is considered down (several heartbeat periods). */
export const WORKER_STALE_MS = 20_000;

export function workerStatus(now = Date.now()): { running: boolean; lastSeenAt: number | null } {
  const row = db.get<{ updated_at: number }>(
    "SELECT updated_at FROM worker_heartbeat WHERE id = 1",
  );
  if (!row) return { running: false, lastSeenAt: null };
  return { running: now - row.updated_at < WORKER_STALE_MS, lastSeenAt: row.updated_at };
}
