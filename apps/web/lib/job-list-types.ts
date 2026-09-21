/**
 * Client-safe job list types (concurrency plan C4).
 *
 * Split from job-list.ts for the same reason job-view-types.ts is split from
 * job-view.ts: client components must never import the database layer.
 */

export type JobListItem = {
  jobId: string;
  status: string;
  /** Human-readable stage, e.g. "Separating stems". */
  userStage: string;
  progressMessage: string;
  progress: number;
  mode: string;
  outputFormat: string;
  source: {
    type: "upload" | "youtube" | "spotify";
    filename: string | null;
    artworkUrl?: string;
  };
  /**
   * 1-based position in the queue, in the exact order the worker claims jobs
   * (`created_at, id`). Present only while the job is queued: a processing job
   * is not waiting for anything. The queue is global, so this counts every
   * waiting job, not only this browser's.
   */
  queuePosition?: number;
  createdAt: string;
  updatedAt: string;
  /** This job's own status endpoint, so a list row can be polled directly. */
  statusUrl: string;
};

export type JobListResponse = { jobs: JobListItem[] };

/** Statuses that mean "still holding the machine". */
export const ACTIVE_STATUSES = ["queued", "processing"] as const;

export function isActiveStatus(status: string): boolean {
  return (ACTIVE_STATUSES as readonly string[]).includes(status);
}
