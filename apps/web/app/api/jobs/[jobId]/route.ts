import { NextResponse } from "next/server";

import { getOrCreateGuestId } from "@/lib/auth/guest";
import { getJobView } from "@/lib/job-view";

/**
 * GET /api/jobs/{jobId} (plan §11.3). Guest-cookie authorized; responses are
 * sanitized by job-view. Returns 404 for unknown jobs and jobs owned by
 * someone else — indistinguishable on purpose.
 */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ jobId: string }> },
) {
  const { jobId } = await params;
  if (!/^job_[a-f0-9]{32}$/.test(jobId)) {
    return NextResponse.json({ error: "not_found" }, { status: 404 });
  }

  const ownerKey = await getOrCreateGuestId();
  const view = await getJobView(jobId, ownerKey);
  if (!view) {
    return NextResponse.json({ error: "not_found" }, { status: 404 });
  }
  return NextResponse.json(view);
}
