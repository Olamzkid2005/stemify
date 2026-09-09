import { NextResponse } from "next/server";

import { getOrCreateGuestId } from "@/lib/auth/guest";
import { db } from "@/lib/db/client";
import { type JobOutputRow, type JobRow } from "@/lib/db/schema";
import { idempotencyHash } from "@/lib/jobs";
import { getStorage } from "@/lib/storage";

/**
 * POST /api/jobs/{jobId}/refine-drums (roadmap Phase B).
 *
 * Queues a `drum_breakdown` job whose input is the completed parent job's
 * stored Drums output — the worker's normal validate/decode/separate pipeline
 * runs on it unchanged, and the drumsep model publishes Kick/Snare/Cymbals/
 * Toms as extra outputs of the new job. The parent job is left untouched.
 *
 * Guardrails: guest ownership, completed and unexpired parent, a stored drums
 * output that still exists, and one active job per owner (mirroring uploads).
 * The idempotency key is derived from the parent job id, so double clicks
 * return the same refine job.
 */
export async function POST(
  _request: Request,
  { params }: { params: Promise<{ jobId: string }> },
) {
  const { jobId } = await params;
  if (!/^job_[a-f0-9]{32}$/.test(jobId)) {
    return NextResponse.json({ error: "not_found" }, { status: 404 });
  }

  const ownerKey = await getOrCreateGuestId();
  const parent = db.get<JobRow>(
    "SELECT * FROM jobs WHERE id = ? AND owner_key = ? LIMIT 1",
    jobId,
    ownerKey,
  );
  if (!parent) return NextResponse.json({ error: "not_found" }, { status: 404 });
  if (parent.status !== "completed") {
    return NextResponse.json({ error: "job_not_completed" }, { status: 409 });
  }
  if (parent.expires_at !== null && parent.expires_at < Date.now()) {
    return NextResponse.json({ error: "results_expired" }, { status: 410 });
  }
  if (parent.mode === "drum_breakdown") {
    return NextResponse.json({ error: "already_a_refine_job" }, { status: 409 });
  }

  const drums = db.get<JobOutputRow>(
    "SELECT * FROM job_outputs WHERE job_id = ? AND stem_key = 'drums' LIMIT 1",
    jobId,
  );
  if (!drums) {
    return NextResponse.json({ error: "no_drums_stem" }, { status: 409 });
  }
  const storage = getStorage();
  const info = await storage.headObject(drums.relative_path);
  if (!info.exists || info.sizeBytes === null || info.sizeBytes === 0) {
    return NextResponse.json({ error: "results_expired" }, { status: 410 });
  }

  // One active job per owner, same as uploads (plan Task 13).
  const active = db.get<{ n: number }>(
    "SELECT COUNT(*) AS n FROM jobs WHERE owner_key = ? AND status IN ('queued', 'processing')",
    ownerKey,
  );
  if ((active?.n ?? 0) >= 1) {
    return NextResponse.json({ error: "too_many_active_jobs" }, { status: 429 });
  }

  // Deterministic per-parent idempotency: clicking twice returns the same job.
  const keyHash = idempotencyHash(ownerKey, `refine-drums:${jobId}`);
  const existing = db.get<Pick<JobRow, "id" | "status">>(
    "SELECT id, status FROM jobs WHERE owner_key = ? AND idempotency_key_hash = ? LIMIT 1",
    ownerKey,
    keyHash,
  );
  if (existing) {
    return NextResponse.json(
      { jobId: existing.id, status: existing.status, statusUrl: `/api/jobs/${existing.id}` },
      { status: 200 },
    );
  }

  const refineJobId = `job_${crypto.randomUUID().replaceAll("-", "")}`;
  db.run(
    `INSERT INTO jobs (
      id, owner_key, source_type, source_filename, source_object_key,
      mode, output_format, status, idempotency_key_hash
    ) VALUES (?, ?, 'upload', ?, ?, 'drum_breakdown', ?, 'queued', ?)`,
    refineJobId,
    ownerKey,
    parent.source_filename,
    drums.relative_path, // the parent's drums output feeds the worker pipeline
    parent.output_format,
    keyHash,
  );

  return NextResponse.json(
    { jobId: refineJobId, status: "queued", statusUrl: `/api/jobs/${refineJobId}` },
    { status: 201 },
  );
}
