import { NextResponse } from "next/server";

import { getOrCreateGuestId } from "@/lib/auth/guest";
import { createJob } from "@/lib/jobs";
import { listJobViews } from "@/lib/job-list";
import { workerPoolStatus } from "@/lib/worker-pool";

/**
 * GET /api/jobs (concurrency plan C4).
 *
 * This browser's own jobs, newest activity first, capped at JOB_LIST_LIMIT.
 * Owner-scoped in SQL and projected through the same `getJobView` the status
 * endpoint uses, so the list cannot show a field the single-job route hides.
 * A queued row carries its global queue position (1 = next to run).
 *
 * The pool summary rides along rather than getting its own endpoint: the page
 * already polls this one, and two pollers could report two different machines.
 */
export async function GET() {
  try {
    const ownerKey = await getOrCreateGuestId();
    const jobs = await listJobViews(ownerKey);
    return NextResponse.json(
      { jobs, pool: workerPoolStatus() },
      { headers: { "cache-control": "no-store" } },
    );
  } catch {
    // A list is convenience, never the source of truth: failing it must not
    // look like "you have no jobs", so the client can tell it apart.
    return NextResponse.json({ error: "internal_error" }, { status: 500 });
  }
}

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
