/**
 * Sanitized job view for the local status API.
 *
 * Filesystem paths, owner identifiers, and worker internals never leave the
 * server. SQLite is the source of truth for the browser polling response.
 */
import { findJobArtwork, jobArtworkUrl } from "@/lib/artwork";
import { db } from "@/lib/db/client";
import { type JobOutputRow, type JobRow } from "@/lib/db/schema";
import { isStemSelection, type StemSelectionKey } from "@/lib/limits";
import { getLocalStorage } from "@/lib/storage";
import { readZipEntryFromFile } from "@/lib/zip";
import { workerStatus } from "@/lib/worker-status";
import { deriveStageTimings, type StageStart } from "./stage-timings";
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

/**
 * Decode jobs.stem_selection for the view (stem-selection plan). The column is
 * written by this process and defensively decoded: junk reads as absent, the
 * same policy as the worker's claim path.
 */
function parseSelectionColumn(raw: string | null): StemSelectionKey[] | null {
  if (raw === null || raw === "") return null;
  try {
    const value: unknown = JSON.parse(raw);
    return isStemSelection(value) ? value : null;
  } catch {
    return null;
  }
}

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

/**
 * When each stage began: the worker's first progress event for it. Progress
 * events are the only stage-stamped rows the worker writes, so this is the
 * earliest per-stage timestamp that exists. Ordered by start, with the stage
 * name as a deterministic tiebreak for events in the same millisecond.
 */
function stageStarts(jobId: string): StageStart[] {
  return db
    .all<{ stage: string; started_at: number }>(
      "SELECT stage, MIN(created_at) AS started_at FROM job_events " +
        "WHERE job_id = ? AND event_type = 'progress' AND stage IS NOT NULL " +
        "GROUP BY stage ORDER BY started_at, stage",
      jobId,
    )
    .map((row) => ({ stage: row.stage, startedAt: row.started_at }));
}

/**
 * Fallback text for the DOWNLOADING stage, used only when no progress event has
 * landed for it yet. Each link source describes its own transfer: a YouTube job
 * downloads an MP3, a Spotify job streams the track's original audio. The
 * thresholds mirror the worker's progress slices (12% start, 24% complete).
 */
function downloadFallbackMessage(sourceType: string, progress: number): string {
  if (sourceType === "spotify") {
    if (progress >= 24) return "Audio streamed; checking the audio";
    if (progress >= 12) return "Streaming audio from Spotify";
    return "Checking the Spotify link";
  }
  if (progress >= 24) return "MP3 downloaded; checking the audio";
  if (progress >= 12) return "Downloading audio as MP3";
  return "Checking the YouTube link";
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
      ? downloadFallbackMessage(job.source_type, job.progress)
      : DEFAULT_PROGRESS_MESSAGES[stage] ?? "Working locally");

  // Richer source metadata (album + cover). Both are optional by nature: the
  // album comes from the worker's best-effort lookup and the cover only exists
  // when the fetch actually saved one.
  const source: JobView["source"] = {
    type: job.source_type,
    filename: job.source_filename,
  };
  if (job.source_album) source.album = job.source_album;
  if (await findJobArtwork(job.id)) source.artworkUrl = jobArtworkUrl(job.id);

  const view: JobView = {
    jobId: job.id,
    status: job.status,
    stage,
    userStage,
    progressMessage,
    progress: job.progress,
    source,
    mode: job.mode,
    outputFormat: job.output_format,
    ...(job.quality ? { quality: job.quality as "fast" | "balanced" } : {}),
    createdAt: isoTime(job.created_at),
    updatedAt: isoTime(job.updated_at),
  };
  // The ticked stems for mode='custom' (stem-selection plan). Decoded here once
  // so the progress line can say "3 stems" without re-deriving it per render.
  const selection = parseSelectionColumn(job.stem_selection);
  if (selection) view.stemSelection = selection;

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

  // Per-stage timing (plan §8.4). Only for jobs still showing the stage list:
  // a completed job's page shows stems, and polling has already stopped there.
  if (job.status !== "completed") {
    view.stageTimings = deriveStageTimings(
      job.created_at,
      stageStarts(job.id),
      // A terminal job closes its last stage; an active one stays open so the
      // page can keep counting. `updated_at` covers terminal states that do not
      // set completed_at (expiry by the cleanup pass).
      isTerminalStatus(job.status) ? (job.completed_at ?? job.updated_at) : null,
    );
  }

  return view;
}
