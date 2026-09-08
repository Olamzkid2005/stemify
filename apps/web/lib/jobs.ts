/**
 * Local SQLite job creation service.
 *
 * Validates policy, verifies the uploaded object exists, and creates exactly one
 * queued job for an owner/idempotency-key pair. The worker claims this row later.
 */
import { createHmac, randomUUID } from "node:crypto";

import { db } from "@/lib/db/client";
import { type JobRow, type JobStatus, type SeparationMode, type OutputFormat, type UploadRow } from "@/lib/db/schema";
import { getStorage } from "@/lib/storage";
import { CLIENT_LIMITS, OUTPUT_FORMATS, SEPARATION_MODES } from "@/lib/limits";

export { OUTPUT_FORMATS, SEPARATION_MODES };

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

function idempotencyHash(ownerKey: string, key: string): string {
  const secret = process.env.JOB_ACCESS_TOKEN_SECRET;
  if (!secret) throw new Error("JOB_ACCESS_TOKEN_SECRET is not set");
  return createHmac("sha256", secret).update(`${ownerKey}:${key}`).digest("hex");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Active-job limit (plan Task 13 / Section 15: MAX_ACTIVE_JOBS).
 * Queued + processing jobs count toward the limit; terminal states do not.
 */
function activeJobLimit(): number {
  const raw = Number(process.env.MAX_ACTIVE_JOBS ?? "1");
  return Number.isFinite(raw) && raw >= 1 ? Math.floor(raw) : 1;
}

function activeJobCount(ownerKey: string): number {
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

  const { source, mode, outputFormat, idempotencyKey } = input.body;
  if (
    !(SEPARATION_MODES as readonly unknown[]).includes(mode) ||
    !(OUTPUT_FORMATS as readonly unknown[]).includes(outputFormat) ||
    typeof idempotencyKey !== "string" ||
    idempotencyKey.length < 16 ||
    idempotencyKey.length > 128
  ) {
    return { ok: false, status: 400, error: "invalid_request" };
  }

  if (!isRecord(source)) {
    return { ok: false, status: 400, error: "invalid_request" };
  }

  // Active-job limit (plan Task 13): reject before touching storage or rows.
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
      mode, output_format, status, idempotency_key_hash
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)`,
    jobId,
    input.ownerKey,
    source.type === "youtube" ? "youtube" : "upload",
    sourceFilename,
    sourceObjectKey,
    source.type === "youtube" ? source.url : null,
    mode as SeparationMode,
    outputFormat as OutputFormat,
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
