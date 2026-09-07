import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { Readable } from "node:stream";
import type { ReadableStream as WebReadableStream } from "node:stream/web";

import { NextResponse } from "next/server";

import { getOrCreateGuestId } from "@/lib/auth/guest";
import {
  contentDispositionFilename,
  parseDownloadRequest,
  parseSingleRange,
  resolveDownload,
} from "@/lib/downloads";

/**
 * GET /api/jobs/{jobId}/downloads (plan Section 10.6 / Task 12).
 *
 * Query parameters:
 *   kind=zip              -> the results archive (stem_key "archive")
 *   kind=stem&stem=vocals -> one encoded stem
 *
 * Authorization: guest-session ownership, terminal completed state, and expiry
 * are all checked before any file is opened. The file is resolved from stored
 * output metadata, never from the request string. Audio responses support a
 * single byte range so <audio> seeking works in previews.
 */

/** Convert a Node stream to the web ReadableStream the Response expects. */
function toWebStream(filePath: string, start?: number, end?: number): ReadableStream<Uint8Array> {
  const nodeStream = createReadStream(filePath, start !== undefined ? { start, end } : undefined);
  return Readable.toWeb(nodeStream) as unknown as WebReadableStream<Uint8Array> as ReadableStream<Uint8Array>;
}

export async function GET(
  request: Request,
  { params }: { params: Promise<{ jobId: string }> },
) {
  const { jobId } = await params;

  const parsed = parseDownloadRequest(new URL(request.url).searchParams);
  if (!parsed) {
    return NextResponse.json({ error: "invalid_request" }, { status: 400 });
  }

  const ownerKey = await getOrCreateGuestId();
  const resolution = await resolveDownload(jobId, ownerKey, parsed);
  if (!resolution.ok) {
    return NextResponse.json(
      { error: resolution.error },
      { status: resolution.status },
    );
  }

  const { output, filePath, sizeBytes } = resolution;
  const disposition = `attachment; filename="${contentDispositionFilename(output, jobId)}"`;
  const baseHeaders: Record<string, string> = {
    "content-type": output.mime_type,
    "content-disposition": disposition,
    "cache-control": "private, no-store",
    "accept-ranges": "bytes",
  };

  // Single byte range (RFC 9110 §14.1.2): enough for <audio> seeking; multi
  // ranges fall back to the full body.
  const rangeResult = parseSingleRange(request.headers.get("range"), sizeBytes);
  if (rangeResult === "invalid") {
    return new NextResponse(null, {
      status: 416,
      headers: { "content-range": `bytes */${sizeBytes}` },
    });
  }
  if (rangeResult) {
    const { start, end } = rangeResult;
    return new NextResponse(toWebStream(filePath, start, end), {
      status: 206,
      headers: {
        ...baseHeaders,
        "content-range": `bytes ${start}-${end}/${sizeBytes}`,
        "content-length": String(end - start + 1),
      },
    });
  }

  // Stream the whole file without buffering it in memory.
  const fileStat = await stat(filePath);
  return new NextResponse(toWebStream(filePath), {
    status: 200,
    headers: { ...baseHeaders, "content-length": String(fileStat.size) },
  });
}

