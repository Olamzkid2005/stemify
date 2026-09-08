/**
 * Sanitized job view for the local status API.
 *
 * Filesystem paths, owner identifiers, and worker internals never leave the
 * server. SQLite is the source of truth for the browser polling response.
 */
import { db } from "@/lib/db/client";
import { type JobOutputRow, type JobRow } from "@/lib/db/schema";
import { workerStatus } from "@/lib/worker-status";
import { isTerminalStatus, type JobView } from "./job-view-types";

const USER_STAGES: Record<string, string> = {
  starting: "Preparing audio",
  downloading: "Preparing audio",
  validating: "Analyzing track",
  preparing_audio: "Analyzing track",
  separating: "Separating stems",
  encoding: "Encoding files",
  uploading_results: "Preparing downloads",
  packaging: "Preparing downloads",
  cleanup: "Preparing downloads",
  completed: "Completed",
};

export type { JobView };
export { isTerminalStatus };

function isoTime(value: number): string {
  return new Date(value).toISOString();
}

export async function getJobView(jobId: string, ownerKey: string): Promise<JobView | null> {
  const job = db.get<JobRow>(
    "SELECT * FROM jobs WHERE id = ? AND owner_key = ? LIMIT 1",
    jobId,
    ownerKey,
  );
  if (!job) return null;

  const view: JobView = {
    jobId: job.id,
    status: job.status,
    stage: job.stage ?? (job.status === "queued" ? "starting" : job.status),
    userStage: USER_STAGES[job.stage ?? ""] ?? (job.status === "queued" ? "Preparing audio" : "Working"),
    progress: job.progress,
    source: { filename: job.source_filename },
    mode: job.mode,
    outputFormat: job.output_format,
    createdAt: isoTime(job.created_at),
    updatedAt: isoTime(job.updated_at),
  };

  if (job.status === "completed") {
    const outputs = db.all<JobOutputRow>(
      "SELECT * FROM job_outputs WHERE job_id = ? ORDER BY stem_key",
      job.id,
    );
    view.stems = outputs
      .filter((output) => output.stem_key !== "archive")
      .map((output) => ({
        id: output.stem_key,
        label: output.label,
        durationSeconds: output.duration_seconds,
      }));
    view.downloadUrl = `/api/jobs/${job.id}/downloads?kind=zip`;
    if (job.expires_at !== null) view.expiresAt = isoTime(job.expires_at);
  }

  if (job.status === "failed") {
    view.errorCode = job.error_code ?? "UNKNOWN";
    view.errorMessage = job.error_message_public;
  }

  // Task 13: non-sensitive worker-unavailable signal for active jobs only.
  if (job.status === "queued" || job.status === "processing") {
    view.workerRunning = workerStatus().running;
  }

  return view;
}
