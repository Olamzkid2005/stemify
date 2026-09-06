import { NextResponse } from "next/server";

import { getOrCreateGuestId } from "@/lib/auth/guest";
import { createJob } from "@/lib/jobs";

/**
 * POST /api/jobs (plan §11.2). Guest-authenticated; body validation and
 * idempotency live in the jobs service. Worker launch lands in Task 13.
 */
export async function POST(request: Request) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "invalid_json" }, { status: 400 });
  }

  try {
    const ownerKey = await getOrCreateGuestId();
    const result = await createJob({ ownerKey, body });
    if (!result.ok) {
      return NextResponse.json({ error: result.error }, { status: result.status });
    }
    return NextResponse.json(
      {
        jobId: result.job.id,
        status: result.job.status,
        statusUrl: `/api/jobs/${result.job.id}`,
      },
      { status: result.status },
    );
  } catch (err) {
    const message = err instanceof Error ? err.message : "unknown";
    if (message.includes("JOB_ACCESS_TOKEN_SECRET")) {
      return NextResponse.json({ error: "server_misconfigured" }, { status: 500 });
    }
    return NextResponse.json({ error: "internal_error" }, { status: 500 });
  }
}
