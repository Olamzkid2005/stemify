import { createReadStream } from "node:fs";
import { Readable } from "node:stream";
import type { ReadableStream as WebReadableStream } from "node:stream/web";

import { NextResponse } from "next/server";

import { ARTWORK_MIME_TYPE, findJobArtwork } from "@/lib/artwork";
import { getOrCreateGuestId } from "@/lib/auth/guest";
import { db } from "@/lib/db/client";

/**
 * GET /api/jobs/{jobId}/artwork — the album cover saved with this job.
 *
 * Local-first: the worker downloaded the cover during the fetch and this route
 * streams that file from the local data directory, so the browser never
 * contacts Spotify's image CDN. Owner-checked like every other job artifact
 * (another browser's job is indistinguishable from an unknown one), and the
 * path is derived from the job id rather than the request.
 */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ jobId: string }> },
) {
  const { jobId } = await params;

  const ownerKey = await getOrCreateGuestId();
  const job = db.get<{ id: string }>(
    "SELECT id FROM jobs WHERE id = ? AND owner_key = ? LIMIT 1",
    jobId,
    ownerKey,
  );
  if (!job) return NextResponse.json({ error: "not_found" }, { status: 404 });

  const artwork = await findJobArtwork(jobId);
  if (!artwork) return NextResponse.json({ error: "artwork_unavailable" }, { status: 404 });

  const stream = createReadStream(artwork.filePath);
  return new NextResponse(
    Readable.toWeb(stream) as unknown as WebReadableStream<Uint8Array> as ReadableStream<Uint8Array>,
    {
      status: 200,
      headers: {
        "content-type": ARTWORK_MIME_TYPE,
        "content-length": String(artwork.sizeBytes),
        // Private and short: the file never changes for a given job id, but a
        // shared cache must never hold another session's thumbnail.
        "cache-control": "private, max-age=300",
      },
    },
  );
}
