/**
 * Album cover the worker saves for a link job (richer job metadata).
 *
 * Local-first by design: the Spotify fetch child downloads the cover into the
 * job's results directory and this app serves that file, so the browser never
 * talks to Spotify's image CDN — the same posture the rest of the UI keeps
 * (self-hosted fonts, no external assets). It sits in `results/{jobId}/`, which
 * retention already deletes wholesale with the stems.
 *
 * The file name is fixed and its bytes were checked by the worker for the JPEG
 * signature before anything was written, which is what lets the route below
 * advertise `image/jpeg` without sniffing the file.
 */
import { getLocalStorage } from "@/lib/storage";

export const ARTWORK_FILENAME = "artwork.jpg";
export const ARTWORK_MIME_TYPE = "image/jpeg";

/** Same strictness as the download resolver: server-generated ids only. */
const JOB_ID = /^job_[a-f0-9]{32}$/;
const ARTWORK_RELATIVE_PATH = /^results\/[A-Za-z0-9_-]+\/[A-Za-z0-9._-]+$/;

export type JobArtwork = {
  relativePath: string;
  filePath: string;
  sizeBytes: number;
};

/** The cover URL for a job, as the job view exposes it. */
export function jobArtworkUrl(jobId: string): string {
  return `/api/jobs/${jobId}/artwork`;
}

/**
 * The stored cover for a job, or null when there is none.
 *
 * Absence is the normal case (uploads, and any link job whose cover lookup
 * failed), so this returns null rather than throwing: a missing thumbnail must
 * never turn into a failed job view or a 500 on the route.
 */
export async function findJobArtwork(jobId: string): Promise<JobArtwork | null> {
  if (!JOB_ID.test(jobId)) return null;
  // Server-owned key, but the containment check inside objectPath is still the
  // authority — same reasoning as the downloads route.
  const relativePath = `results/${jobId}/${ARTWORK_FILENAME}`;
  if (!ARTWORK_RELATIVE_PATH.test(relativePath)) return null;

  const storage = getLocalStorage();
  let filePath: string;
  try {
    filePath = storage.objectPath(relativePath);
  } catch {
    return null;
  }

  const info = await storage.headObject(relativePath);
  if (!info.exists || info.sizeBytes === null || info.sizeBytes === 0) return null;
  return { relativePath, filePath, sizeBytes: info.sizeBytes };
}
