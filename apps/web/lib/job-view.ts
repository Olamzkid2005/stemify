/**
 * Sanitized job view for the local status API.
 *
 * Filesystem paths, owner identifiers, and worker internals never leave the
 * server. SQLite is the source of truth for the browser polling response.
 */
import { db } from "@/lib/db/client";
import { type JobOutputRow, type JobRow } from "@/lib/db/schema";
import { getLocalStorage } from "@/lib/storage";
import { readZipEntryFromFile } from "@/lib/zip";
import { workerStatus } from "@/lib/worker-status";
import { isTerminalStatus, type JobView } from "./job-view-types";

const USER_STAGES: Record<string, string> = {
  starting: "Preparing audio",
  downloading: "Downloading audio",
  validating: "Checking your audio",
  preparing_audio: "Preparing audio",
  separating: "Separating stems",
  encoding: "Encoding files",
  uploading_results: "Preparing downloads",
  packaging: "Preparing downloads",
  cleanup: "Preparing downloads",
  completed: "Completed",
};

const DEFAULT_PROGRESS_MESSAGES: Record<string, string> = {
  starting: "Starting the local worker",
  downloading: "Downloading audio as MP3",
  validating: "Validating the audio file",
  preparing_audio: "Preparing audio for separation",
  separating: "Running the separation model",
  encoding: "Encoding the separated stem files",
  packaging: "Building the ZIP and manifest",
  cleanup: "Finishing up",
  completed: "Your stems are ready",
};


export type { JobView };
export { isTerminalStatus };

function isoTime(value: number): string {
  return new Date(value).toISOString();
}

async function latestProgressMessage(jobId: string, stage: string | null): Promise<string | undefined> {
  if (!stage) return undefined;
  // rowid, not created_at: several progress updates can land in the same
  // millisecond and the id is random, so only insertion order is reliable.
  const event = db.get<{ detail: string | null }>(
    "SELECT detail FROM job_events WHERE job_id = ? AND event_type = 'progress' " +
      "AND stage = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
    jobId,
    stage,
  );
  return event?.detail ?? undefined;
}

/**
 * Read the analysis block (roadmap Phase C) from the manifest embedded in the
 * worker-built archive. Best-effort: any absence or malformation simply means
 * no badge is shown — it must never break the job view.
 */
async function readAnalysis(
  jobId: string,
  outputs: JobOutputRow[],
): Promise<JobView["analysis"] | undefined> {
  try {
    const archive = outputs.find((output) => output.stem_key === "archive");
    if (!archive) return undefined;
    const manifestRaw = await readZipEntryFromFile(
      getLocalStorage().objectPath(archive.relative_path),
      "manifest.json",
    );
    if (!manifestRaw) return undefined;
    const parsed = JSON.parse(manifestRaw.toString("utf8")) as {
      analysis?: { bpm?: unknown; key?: unknown; camelot?: unknown };
    };
    const { bpm, key, camelot } = parsed.analysis ?? {};
    if (
      typeof bpm !== "number" ||
      !Number.isFinite(bpm) ||
      bpm <= 0 ||
      bpm > 400 ||
      typeof key !== "string" ||
      typeof camelot !== "string"
    ) {
      return undefined;
    }
    return { bpm, key, camelot };
  } catch {
    return undefined;
  }
}

export async function getJobView(jobId: string, ownerKey: string): Promise<JobView | null> {
  const job = db.get<JobRow>(
    "SELECT * FROM jobs WHERE id = ? AND owner_key = ? LIMIT 1",
    jobId,
    ownerKey,
  );
  if (!job) return null;

  const stage = job.stage ?? (job.status === "queued" ? "starting" : job.status);
  const userStage = USER_STAGES[stage] ?? (job.status === "queued" ? "Preparing audio" : "Working");
  const progressMessage =
    (await latestProgressMessage(job.id, stage)) ??
    (stage === "downloading"
      ? job.progress >= 24
        ? "MP3 downloaded; checking the audio"
        : job.progress >= 12
          ? "Downloading audio as MP3"
          : "Checking the YouTube link"
      : DEFAULT_PROGRESS_MESSAGES[stage] ?? "Working locally");

  const view: JobView = {
    jobId: job.id,
    status: job.status,
    stage,
    userStage,
    progressMessage,
    progress: job.progress,
    source: { type: job.source_type, filename: job.source_filename },
    mode: job.mode,
    outputFormat: job.output_format,
    ...(job.quality ? { quality: job.quality as "fast" | "balanced" | "best" } : {}),
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
    const analysis = await readAnalysis(job.id, outputs);
    if (analysis) view.analysis = analysis;
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
