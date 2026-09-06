/**
 * Sanitized job view for public API responses (plan §11.3).
 *
 * Never exposes: owner_key, worker_call_id, object keys, storage internals,
 * or raw errors. Stage names map to the five user-visible stages (§8.3).
 */
import { and, eq } from "drizzle-orm";

import { db } from "@/lib/db/client";
import { jobOutputs, jobs } from "@/lib/db/schema";
import { isTerminalStatus, type JobView } from "./job-view-types";

/** Internal stage → user-visible stage label (§8.3: five simple stages). */
const USER_STAGES: Record<string, string> = {
  starting: "Preparing audio",
  downloading: "Preparing audio",
  validating: "Analyzing track",
  preparing_audio: "Analyzing track",
  separating: "Separating stems",
  encoding: "Encoding files",
  uploading_results: "Preparing downloads",
  cleanup: "Preparing downloads",
  completed: "Completed",
};

export type { JobView };
export { isTerminalStatus };

export async function getJobView(
  jobId: string,
  ownerKey: string,
): Promise<JobView | null> {
  const [job] = await db
    .select()
    .from(jobs)
    .where(and(eq(jobs.id, jobId), eq(jobs.ownerKey, ownerKey)))
    .limit(1);
  if (!job) return null; // unknown AND foreign jobs are indistinguishable

  const view: JobView = {
    jobId: job.id,
    status: job.status,
    stage: job.stage ?? job.status,
    userStage: USER_STAGES[job.stage ?? ""] ?? "Working",
    progress: job.progress,
    source: { filename: job.sourceFilename },
    mode: job.mode,
    outputFormat: job.outputFormat,
    createdAt: job.createdAt.toISOString(),
    updatedAt: job.updatedAt.toISOString(),
  };

  if (job.status === "completed") {
    const outputs = await db
      .select({
        stemKey: jobOutputs.stemKey,
        label: jobOutputs.label,
        durationSeconds: jobOutputs.durationSeconds,
      })
      .from(jobOutputs)
      .where(eq(jobOutputs.jobId, job.id));
    view.stems = outputs.map((o) => ({
      id: o.stemKey,
      label: o.label,
      durationSeconds: o.durationSeconds,
    }));
    view.downloadUrl = `/api/jobs/${job.id}/downloads?kind=zip`;
    if (job.expiresAt) view.expiresAt = job.expiresAt.toISOString();
  }

  if (job.status === "failed") {
    view.errorCode = job.errorCode ?? "UNKNOWN";
    view.errorMessage = job.errorMessagePublic;
  }

  return view;
}
