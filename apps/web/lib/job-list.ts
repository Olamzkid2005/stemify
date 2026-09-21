/**
 * The home page's "Your jobs" list (concurrency plan C4).
 *
 * Built on `getJobView` on purpose: the list is a *view* of the same sanitized
 * job object the status API returns, so one sanitizer keeps deciding what
 * leaves the server. A second, hand-rolled projection would eventually leak a
 * field the single-job route strips.
 *
 * Ordering: active jobs first (oldest first, so the list reads like the queue),
 * then finished jobs (newest first, so the most recent result is at the top).
 */
import { db } from "@/lib/db/client";
import { getJobView } from "@/lib/job-view";
import { isActiveStatus, type JobListItem } from "@/lib/job-list-types";

/** Most rows the endpoint will return, whatever the caller asks for. */
export const JOB_LIST_LIMIT = 10;

/**
 * Queue positions, 1-based, in the order the worker claims jobs
 * (`created_at, id` — the same ORDER BY as `claim_next_queued_job`). If those
 * two orderings ever diverge, the displayed position is a lie, so the ordering
 * is asserted by a test against the queue's own SQL.
 */
export function queuedPositions(): Map<string, number> {
  const rows = db.all<{ id: string }>(
    "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at, id",
  );
  return new Map(rows.map((row, index) => [row.id, index + 1]));
}

function toListItem(view: Awaited<ReturnType<typeof getJobView>>, position: number | undefined): JobListItem {
  if (!view) throw new Error("toListItem called with an empty view");
  const item: JobListItem = {
    jobId: view.jobId,
    status: view.status,
    userStage: view.userStage,
    progressMessage: view.progressMessage,
    progress: view.progress,
    mode: view.mode,
    outputFormat: view.outputFormat,
    source: {
      type: view.source.type,
      filename: view.source.filename,
      ...(view.source.artworkUrl ? { artworkUrl: view.source.artworkUrl } : {}),
    },
    createdAt: view.createdAt,
    updatedAt: view.updatedAt,
    statusUrl: `/api/jobs/${view.jobId}`,
  };
  if (view.status === "queued" && position !== undefined) {
    item.queuePosition = position;
  }
  return item;
}

/**
 * This owner's recent jobs. Owner-scoped in SQL, so another browser's job can
 * never appear even before the per-job view re-checks ownership.
 */
export async function listJobViews(
  ownerKey: string,
  limit: number = JOB_LIST_LIMIT,
): Promise<JobListItem[]> {
  const capped = Math.max(1, Math.min(JOB_LIST_LIMIT, Math.floor(limit)));
  const active = db.all<{ id: string }>(
    "SELECT id FROM jobs WHERE owner_key = ? AND status IN ('queued', 'processing') " +
      "ORDER BY created_at, id LIMIT ?",
    ownerKey,
    capped,
  );
  const finished = db.all<{ id: string }>(
    "SELECT id FROM jobs WHERE owner_key = ? AND status NOT IN ('queued', 'processing') " +
      "ORDER BY created_at DESC, id DESC LIMIT ?",
    ownerKey,
    capped,
  );

  const positions = queuedPositions();
  const items: JobListItem[] = [];
  for (const row of [...active, ...finished].slice(0, capped)) {
    const view = await getJobView(row.id, ownerKey);
    if (view) items.push(toListItem(view, positions.get(row.id)));
  }
  return items;
}

export { isActiveStatus };
