/**
 * Local SQLite job creation service.
 *
 * Validates policy, verifies the uploaded object exists, and creates exactly one
 * queued job for an owner/idempotency-key pair. The worker claims this row later.
 */
import { createHmac, randomUUID } from "node:crypto";

import { spotifyEnabled } from "@/lib/capabilities";
import { db } from "@/lib/db/client";
import { type JobRow, type JobStatus, type SeparationMode, type OutputFormat, type UploadRow } from "@/lib/db/schema";
import { getStorage } from "@/lib/storage";
import { CLIENT_LIMITS, OUTPUT_FORMATS, QUALITY_PRESETS, SEPARATION_MODES } from "@/lib/limits";

export { OUTPUT_FORMATS, QUALITY_PRESETS, SEPARATION_MODES };

type CreateJobSuccess = { ok: true; status: 201 | 200; job: { id: string; status: JobStatus } };
type CreateJobFailure = {
  ok: false;
  status: 400 | 403 | 409 | 413 | 429;
  error: string;
};

export type CreateJobInput = {
  ownerKey: string;
  body: unknown;
};

export type CreateJobResult = CreateJobSuccess | CreateJobFailure;

/**
 * HMAC of an idempotency key for one owner. Exported for the refine-drums
 * route (roadmap Phase B), which derives a deterministic key per parent job so
 * double clicks return the same refine job instead of stacking duplicates.
 */
export function idempotencyHash(ownerKey: string, key: string): string {
  const secret = process.env.JOB_ACCESS_TOKEN_SECRET;
  if (!secret) throw new Error("JOB_ACCESS_TOKEN_SECRET is not set");
  return createHmac("sha256", secret).update(`${ownerKey}:${key}`).digest("hex");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Active-job limit (plan Task 13 / Section 15: MAX_ACTIVE_JOBS; concurrency
 * plan C3). Queued + processing jobs count toward the limit; terminal states
 * do not.
 *
 * The default is the worker pool (STEMIFY_WORKER_CONCURRENCY, 2) plus one job
 * of headroom, so submitting while others run queues instead of being refused
 * — the parallel-pool product goal. The limit stays a backstop, not the pool
 * size: it bounds what one browser may pile up.
 */
/** Default cap for one browser: pool 2 + 1 waiting job (concurrency plan 8). */
export const DEFAULT_ACTIVE_JOB_LIMIT = 3;

export function activeJobLimit(): number {
  const raw = Number(process.env.MAX_ACTIVE_JOBS ?? DEFAULT_ACTIVE_JOB_LIMIT);
  return Number.isFinite(raw) && raw >= 1 ? Math.floor(raw) : DEFAULT_ACTIVE_JOB_LIMIT;
}

/** Queued + processing jobs for one owner — what the cap is measured against. */
export function activeJobCount(ownerKey: string): number {
  const row = db.get<{ n: number }>(
    "SELECT COUNT(*) AS n FROM jobs WHERE owner_key = ? AND status IN ('queued', 'processing')",
    ownerKey,
  );
  return row?.n ?? 0;
}

export async function createJob(input: CreateJobInput): Promise<CreateJobResult> {
  if (!isRecord(input.body)) {
    return { ok: false, status: 400, error: "invalid_request" };
  }

  const { source, mode, outputFormat, quality, idempotencyKey } = input.body;
  if (
    !(SEPARATION_MODES as readonly unknown[]).includes(mode) ||
    !(OUTPUT_FORMATS as readonly unknown[]).includes(outputFormat) ||
    typeof idempotencyKey !== "string" ||
    idempotencyKey.length < 16 ||
    idempotencyKey.length > 128
  ) {
    return { ok: false, status: 400, error: "invalid_request" };
  }
  // Quality is optional: undefined/null = worker default (STEMIFY_QUALITY);
  // otherwise it must be an exact preset name.
  if (
    quality !== undefined &&
    quality !== null &&
    !(QUALITY_PRESETS as readonly unknown[]).includes(quality)
  ) {
    return { ok: false, status: 400, error: "invalid_request" };
  }

  if (!isRecord(source)) {
    return { ok: false, status: 400, error: "invalid_request" };
  }

  // Active-job limit (plan Task 13): reject before touching storage or rows.
  // 429 stays the backstop for a client that keeps submitting (concurrency
  // plan C3) — the default cap leaves room for a pool to be busy.
  if (activeJobCount(input.ownerKey) >= activeJobLimit()) {
    return { ok: false, status: 429, error: "too_many_active_jobs" };
  }

  let sourceFilename: string | null = null;
  let sourceObjectKey: string | null = null;

  if (source.type === "youtube") {
    const url = source.url;
    if (typeof url !== "string" || !isAllowedYouTubeUrl(url)) {
      return { ok: false, status: 400, error: "unsupported_source" };
    }
  } else if (source.type === "spotify") {
    // Spotify plan S4: single tracks only. Album/playlist links and
    // spotify.link short links are rejected because their target cannot be
    // verified without a network redirect. The worker re-validates this same
    // allowlist — it is the final policy authority (plan Section 8).
    const url = source.url;
    if (typeof url !== "string" || !isAllowedSpotifyUrl(url)) {
      return { ok: false, status: 400, error: "unsupported_source" };
    }
    // Capability, not policy: a machine with Spotify switched off (or without
    // the Premium setup) makes the worker refuse every Spotify job, so refuse
    // it here, where the UI can say what to do about it, instead of accepting a
    // job that dies on claim.
    if (!spotifyEnabled()) {
      return { ok: false, status: 400, error: "spotify_unavailable" };
    }
  } else if (source.type === "upload") {
    const { uploadId } = source;
    if (typeof uploadId !== "string" || !/^upl_[a-f0-9]{32}$/.test(uploadId)) {
      return { ok: false, status: 400, error: "invalid_request" };
    }

    const upload = db.get<UploadRow>(
      "SELECT * FROM uploads WHERE id = ? AND owner_key = ? LIMIT 1",
      uploadId,
      input.ownerKey,
    );
    if (!upload) return { ok: false, status: 403, error: "upload_forbidden" };
    if (upload.expires_at !== null && upload.expires_at < Date.now()) {
      return { ok: false, status: 409, error: "upload_expired" };
    }

    const info = await getStorage().headObject(upload.object_key);
    if (!info.exists) return { ok: false, status: 409, error: "object_missing" };
    if (info.sizeBytes !== null && info.sizeBytes > CLIENT_LIMITS.maxUploadBytes) {
      return { ok: false, status: 413, error: "file_too_large" };
    }

    sourceFilename = upload.filename;
    sourceObjectKey = upload.object_key;
  } else {
    return { ok: false, status: 400, error: "unsupported_source" };
  }

  const keyHash = idempotencyHash(input.ownerKey, idempotencyKey);
  const existing = db.get<Pick<JobRow, "id" | "status">>(
    "SELECT id, status FROM jobs WHERE owner_key = ? AND idempotency_key_hash = ? LIMIT 1",
    input.ownerKey,
    keyHash,
  );
  if (existing) return { ok: true, status: 200, job: existing };

  const jobId = `job_${randomUUID().replaceAll("-", "")}`;
  db.run(
    `INSERT OR IGNORE INTO jobs (
      id, owner_key, source_type, source_filename, source_object_key, source_url,
      mode, output_format, quality, status, idempotency_key_hash
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)`,
    jobId,
    input.ownerKey,
    source.type,
    sourceFilename,
    sourceObjectKey,
    source.type === "upload" ? null : source.url,
    mode as SeparationMode,
    outputFormat as OutputFormat,
    typeof quality === "string" ? quality : null,
    keyHash,
  );

  const created = db.get<Pick<JobRow, "id" | "status">>(
    "SELECT id, status FROM jobs WHERE owner_key = ? AND idempotency_key_hash = ? LIMIT 1",
    input.ownerKey,
    keyHash,
  );
  if (!created) throw new Error("job insert did not produce a row");

  return {
    ok: true,
    status: created.id === jobId ? 201 : 200,
    job: created,
  };
}

/**
 * Spotify track policy, mirroring the worker's (worker/worker/spotify.py).
 * Accepts `https://open.spotify.com/track/<22-char id>` — optionally behind
 * the /intl-xx/ locale prefix Spotify itself adds, and with a share query
 * string — plus `spotify:track:` URIs. Album/playlist and short links are
 * rejected: v1 is single-track.
 */
const SPOTIFY_TRACK_PATH = /^\/(?:intl-[a-z]{2}(?:-[A-Za-z]{2})?\/)?track\/[A-Za-z0-9]{22}$/;
const SPOTIFY_TRACK_URI = /^spotify:track:[A-Za-z0-9]{22}$/;

function isAllowedSpotifyUrl(value: string): boolean {
  if (value.length > 2048) return false;
  if (SPOTIFY_TRACK_URI.test(value)) return true;
  // Never allow userinfo: URL.hostname reads the part after the last "@", so
  // `https://open.spotify.com@evil.example/track/...` would look allowlisted.
  if (value.includes("@")) return false;
  try {
    const url = new URL(value);
    return (
      url.protocol === "https:" &&
      url.hostname === "open.spotify.com" &&
      SPOTIFY_TRACK_PATH.test(url.pathname)
    );
  } catch {
    return false;
  }
}

function isAllowedYouTubeUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return (
      url.protocol === "https:" &&
      (url.hostname === "youtube.com" ||
        url.hostname === "www.youtube.com" ||
        url.hostname === "youtu.be") &&
      url.pathname.length > 1 &&
      value.length <= 2048
    );
  } catch {
    return false;
  }
}
