/**
 * Job creation service (plan Task 6 / §11.2).
 *
 * Validates policy server-side, verifies the uploaded object exists and
 * belongs to the caller's upload session, and creates the job row exactly
 * once per (owner, idempotency key).
 */
import { createHmac } from "node:crypto";
import { and, eq } from "drizzle-orm";

import { db } from "@/lib/db/client";
import { jobs } from "@/lib/db/schema";
import { getStorage } from "@/lib/storage";
import { CLIENT_LIMITS } from "@/lib/limits";

export const SEPARATION_MODES = ["vocals_instrumental", "full_stems"] as const;
export const OUTPUT_FORMATS = ["mp3", "wav", "flac", "ogg", "m4a"] as const;

export type CreateJobInput = {
  ownerKey: string;
  body: unknown;
};

export type CreateJobResult =
  | { ok: true; status: 201 | 200; job: { id: string; status: string } }
  | { ok: false; status: 400 | 403 | 409 | 413; error: string };

function idempotencyHash(ownerKey: string, key: string): string {
  return createHmac("sha256", process.env.JOB_ACCESS_TOKEN_SECRET ?? "")
    .update(`${ownerKey}:${key}`)
    .digest("hex");
}

export async function createJob(input: CreateJobInput): Promise<CreateJobResult> {
  const body = input.body as Record<string, unknown> | null;
  if (!body || typeof body !== "object") {
    return { ok: false, status: 400, error: "invalid_request" };
  }

  const { source, mode, outputFormat, idempotencyKey } = body as Record<string, unknown>;
  if (
    !(SEPARATION_MODES as readonly unknown[]).includes(mode) ||
    !(OUTPUT_FORMATS as readonly unknown[]).includes(outputFormat) ||
    typeof idempotencyKey !== "string" ||
    idempotencyKey.length < 16 ||
    idempotencyKey.length > 128
  ) {
    return { ok: false, status: 400, error: "invalid_request" };
  }

  const sourceObj = source as Record<string, unknown> | undefined;
  if (!sourceObj || typeof sourceObj !== "object") {
    return { ok: false, status: 400, error: "invalid_request" };
  }

  let sourceFilename: string | null = null;
  let sourceObjectKey: string | null = null;

  if (sourceObj.type === "youtube") {
    // Worker-side validation happens later; policy lives in §10.2. The URL is
    // stored minimized; strict hostname checks land with Task 16.
    const url = sourceObj.url;
    if (typeof url !== "string" || !/^https:\/\/(www\.)?(youtube\.com|youtu\.be)\//.test(url)) {
      return { ok: false, status: 400, error: "unsupported_source" };
    }
  } else if (sourceObj.type === "upload") {
    const { uploadId, objectKey, filename } = sourceObj as Record<string, unknown>;
    if (
      typeof uploadId !== "string" ||
      typeof objectKey !== "string" ||
      typeof filename !== "string" ||
      !/^upl_[a-f0-9]{32}$/.test(uploadId)
    ) {
      return { ok: false, status: 400, error: "invalid_request" };
    }
    // Ownership: the object key must live inside THIS upload session's prefix.
    const expectedPrefix = `sources/${uploadId}/`;
    if (!objectKey.startsWith(expectedPrefix)) {
      return { ok: false, status: 403, error: "object_forbidden" };
    }
    const storage = getStorage();
    const info = await storage.headObject(objectKey);
    if (!info.exists) {
      return { ok: false, status: 409, error: "object_missing" };
    }
    if (info.sizeBytes !== null && info.sizeBytes > CLIENT_LIMITS.maxUploadBytes) {
      return { ok: false, status: 413, error: "file_too_large" };
    }
    sourceFilename = filename.slice(0, 255);
    sourceObjectKey = objectKey;
  } else {
    return { ok: false, status: 400, error: "unsupported_source" };
  }

  const keyHash = idempotencyHash(input.ownerKey, idempotencyKey);

  // Idempotent creation: return the existing job for a repeated key.
  const [existing] = await db
    .select({ id: jobs.id, status: jobs.status })
    .from(jobs)
    .where(and(eq(jobs.ownerKey, input.ownerKey), eq(jobs.idempotencyKeyHash, keyHash)))
    .limit(1);
  if (existing) {
    return { ok: true, status: 200, job: existing };
  }

  const jobId = `job_${crypto.randomUUID().replaceAll("-", "")}`;
  const [created] = await db
    .insert(jobs)
    .values({
      id: jobId,
      ownerKey: input.ownerKey,
      sourceType: sourceObj.type === "youtube" ? "youtube" : "upload",
      sourceFilename,
      sourceObjectKey,
      sourceUrl: sourceObj.type === "youtube" ? (sourceObj.url as string) : null,
      mode: mode as (typeof SEPARATION_MODES)[number],
      outputFormat: outputFormat as (typeof OUTPUT_FORMATS)[number],
      status: "queued",
      idempotencyKeyHash: keyHash,
    })
    .returning({ id: jobs.id, status: jobs.status });

  return { ok: true, status: 201, job: created };
}
