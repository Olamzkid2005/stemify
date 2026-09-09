import { createReadStream } from "node:fs";
import { mkdtemp, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { Readable } from "node:stream";
import type { ReadableStream as WebReadableStream } from "node:stream/web";

import { NextResponse } from "next/server";

import { getOrCreateGuestId } from "@/lib/auth/guest";
import { db } from "@/lib/db/client";
import {
  contentDispositionFilename,
  parseDownloadRequest,
  parseSingleRange,
  resolveDownload,
} from "@/lib/downloads";
import { getLocalStorage } from "@/lib/storage";
import { parseStemSelection } from "@/lib/stem-selection";
import { readZipEntryFromFile, writeZipArchive } from "@/lib/zip";

/**
 * GET /api/jobs/{jobId}/downloads (plan Section 10.6 / Task 12, roadmap A3).
 *
 * Query parameters:
 *   kind=zip                      -> the worker-built results archive
 *   kind=zip&stems=vocals,drums   -> custom archive rebuilt from a selection
 *   kind=stem&stem=vocals         -> one encoded stem
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

  const searchParams = new URL(request.url).searchParams;
  const parsed = parseDownloadRequest(searchParams);
  if (!parsed) {
    return NextResponse.json({ error: "invalid_request" }, { status: 400 });
  }

  // Custom selection archive (roadmap A3): parse before authorization so a
  // malformed selection is a cheap 400, mirroring the kind/stem parsing.
  let selected: string[] | null = null;
  if (parsed.kind === "zip") {
    const selection = parseStemSelection(searchParams.get("stems"));
    if (!selection.ok) {
      // Absent `stems` means the default worker archive; a present-but-bad
      // value is a client error.
      if (searchParams.get("stems") !== null) {
        return NextResponse.json({ error: "invalid_request" }, { status: 400 });
      }
    } else {
      selected = selection.stems;
    }
  }

  const ownerKey = await getOrCreateGuestId();
  const resolution = await resolveDownload(jobId, ownerKey, parsed);
  if (!resolution.ok) {
    return NextResponse.json(
      { error: resolution.error },
      { status: resolution.status },
    );
  }

  const { output, sourceFilename, filePath, sizeBytes } = resolution;

  if (parsed.kind === "zip" && selected) {
    return buildCustomZipResponse({
      jobId,
      selected,
      archivePath: filePath,
      outputs: resolveSelectedOutputs(jobId, selected),
      sourceFilename,
    });
  }

  const signature = contentDispositionFilename(output, sourceFilename);
  const disposition = `attachment; filename="${signature.filename}"; filename*=UTF-8''${signature.filenameUtf8}`;
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

type OutputRow = { stem_key: string; relative_path: string };

/** Look up the stored output rows for the selected stem keys. */
function resolveSelectedOutputs(jobId: string, stems: string[]): OutputRow[] {
  const placeholders = stems.map(() => "?").join(", ");
  return db.all<OutputRow>(
    `SELECT stem_key, relative_path FROM job_outputs
     WHERE job_id = ? AND stem_key IN (${placeholders}) ORDER BY stem_key`,
    jobId,
    ...stems,
  );
}

/**
 * Rebuild the archive from the selected stems. The manifest embedded in the
 * worker-built archive is reused (so model id/revision travel with every
 * archive) and trimmed to the selection. Everything is staged in a temp file
 * and streamed; the temp directory is removed when the stream closes.
 */
async function buildCustomZipResponse(input: {
  jobId: string;
  selected: string[];
  archivePath: string;
  outputs: OutputRow[];
  sourceFilename: string | null;
}): Promise<NextResponse> {
  const manifestRaw = await readZipEntryFromFile(input.archivePath, "manifest.json");
  if (!manifestRaw) {
    return NextResponse.json({ error: "result_unavailable" }, { status: 410 });
  }
  const manifest = JSON.parse(manifestRaw.toString("utf8")) as {
    output?: { format?: string; stems?: { name?: string }[] };
  };
  const format = manifest.output?.format;
  const extension = typeof format === "string" && /^[a-z0-9]{1,5}$/.test(format) ? format : null;
  if (!extension) {
    return NextResponse.json({ error: "result_unavailable" }, { status: 410 });
  }

  // Every requested stem must have a stored output; anything else is stale.
  if (input.outputs.length !== input.selected.length) {
    return NextResponse.json({ error: "result_unavailable" }, { status: 410 });
  }

  // Trim the embedded manifest to the selection so the archive stays
  // self-describing; a mismatch means the manifest predates the selection.
  const selectedSet = new Set(input.selected);
  const manifestStems = (manifest.output?.stems ?? []).filter(
    (stem) => typeof stem.name === "string" && selectedSet.has(stem.name),
  );
  if (manifestStems.length !== input.selected.length) {
    return NextResponse.json({ error: "result_unavailable" }, { status: 410 });
  }
  const trimmedManifest = {
    ...manifest,
    output: { ...manifest.output, stems: manifestStems },
  };

  const storage = getLocalStorage();
  const stagedDir = await mkdtemp(path.join(tmpdir(), "stemify-custom-zip-"));
  const stagedArchive = path.join(stagedDir, "stems.zip");
  await writeZipArchive(stagedArchive, [
    ...input.outputs.map((outputRow) => ({
      name: `${outputRow.stem_key}.${extension}`,
      kind: "file" as const,
      // relative_path is server-owned; objectPath re-checks containment.
      path: storage.objectPath(outputRow.relative_path),
    })),
    {
      name: "manifest.json",
      kind: "buffer" as const,
      data: Buffer.from(`${JSON.stringify(trimmedManifest, null, 2)}\n`, "utf8"),
    },
  ]);

  const signature = contentDispositionFilename(
    { stem_key: "archive", relative_path: `results/${input.jobId}/stems.${extension}` } as never,
    input.sourceFilename,
  );
  const archiveStat = await stat(stagedArchive);
  const stream = createReadStream(stagedArchive);
  stream.on("close", () => {
    void rm(stagedDir, { recursive: true, force: true });
  });
  return new NextResponse(
    Readable.toWeb(stream) as unknown as WebReadableStream<Uint8Array> as ReadableStream<Uint8Array>,
    {
      status: 200,
      headers: {
        "content-type": "application/zip",
        "content-disposition": `attachment; filename="${signature.filename}"; filename*=UTF-8''${signature.filenameUtf8}`,
        "cache-control": "private, no-store",
        "content-length": String(archiveStat.size),
      },
    },
  );
}
