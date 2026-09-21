import { NextResponse } from "next/server";

import { getOrCreateGuestId } from "@/lib/auth/guest";
import { requestCancel } from "@/lib/jobs";

/**
 * POST /api/jobs/{jobId}/cancel — the stop button's endpoint. Guest-cookie
 * authorized like every other job route: another browser's job is 404, the
 * same as the status route, so ownership is checked in the one place the
 * owner key already exists.
 *
 * Two outcomes, both honest: a queued job is canceled immediately; a
 * processing job gets the cancel flag set, which the worker polls at chunk
 * boundaries — usually stopping within a few seconds, never mid-write.
 * A finished job is 409 (nothing to stop), an unknown/foreign job is 404 —
 * indistinguishable from the status route on purpose.
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
  const result = await requestCancel(jobId, ownerKey);
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: result.status });
  }
  return NextResponse.json({ jobId, canceled: result.outcome });
}
