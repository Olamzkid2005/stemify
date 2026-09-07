/**
 * Authorized download resolution (plan Task 12 / Section 10.6).
 *
 * The route verifies session ownership and terminal state here, then streams
 * the file the worker recorded in `job_outputs.relative_path`. User input never
 * constructs a filesystem path: the stem parameter is matched against the
 * allowlisted keys and looked up in the database.
 */
import { db } from "@/lib/db/client";
import { type JobOutputRow, type JobRow } from "@/lib/db/schema";
import { getLocalStorage } from "@/lib/storage";
import { STEM_KEYS } from "@/lib/limits";

export type DownloadKind = "zip" | "stem";

export type DownloadFailure =
  | { ok: false; status: 400 | 404 | 409 | 410; error: string }
  | { ok: false; status: 500; error: string };

export type DownloadResolution =
  | {
      ok: true;
      output: JobOutputRow;
      /** Absolute local path inside the data directory. */
      filePath: string;
      sizeBytes: number;
    }
  | DownloadFailure;

/** Strict request shape: kind plus stem, no path-like input accepted. */
export type DownloadRequest = {
  kind: DownloadKind;
  /** Stem key for kind=stem; must match ^[a-z]{1,16}$ before any DB lookup. */
  stem?: string;
};

export function parseDownloadRequest(params: URLSearchParams): DownloadRequest | null {
  const kind = params.get("kind");
  if (kind === "zip") return { kind: "zip" };
  if (kind !== "stem") return null;
  const stem = params.get("stem");
  if (stem === null || !/^[a-z]{1,16}$/.test(stem)) return null;
  return { kind: "stem", stem };
}

export async function resolveDownload(
  jobId: string,
  ownerKey: string,
  request: DownloadRequest,
): Promise<DownloadResolution> {
  if (!/^job_[a-f0-9]{32}$/.test(jobId)) {
    return { ok: false, status: 404, error: "not_found" };
  }

  // Ownership: jobs of other sessions are indistinguishable from unknown ones.
  const job = db.get<JobRow>("SELECT * FROM jobs WHERE id = ? AND owner_key = ? LIMIT 1", jobId, ownerKey);
  if (!job) return { ok: false, status: 404, error: "not_found" };

  if (job.status !== "completed") {
    return { ok: false, status: 409, error: "job_not_completed" };
  }
  if (job.expires_at !== null && job.expires_at < Date.now()) {
    return { ok: false, status: 410, error: "results_expired" };
  }

  let stemKey: string;
  if (request.kind === "zip") {
    stemKey = "archive"; // reserved job_outputs.stem_key for the results ZIP
  } else {
    stemKey = request.stem ?? "";
    if (!(STEM_KEYS as readonly string[]).includes(stemKey)) {
      return { ok: false, status: 400, error: "invalid_request" };
    }
  }

  const output = db.get<JobOutputRow>(
    "SELECT * FROM job_outputs WHERE job_id = ? AND stem_key = ? LIMIT 1",
    jobId,
    stemKey,
  );
  if (!output) return { ok: false, status: 410, error: "result_unavailable" };

  // The relative_path is server-owned ("results/{jobId}/..."), but the containment
  // check below still guarantees it resolves inside the data directory.
  if (!/^results\/[A-Za-z0-9_-]+\/[A-Za-z0-9._-]+$/.test(output.relative_path)) {
    return { ok: false, status: 500, error: "internal_error" };
  }

  const storage = getLocalStorage();
  let filePath: string;
  try {
    filePath = storage.objectPath(output.relative_path);
  } catch {
    return { ok: false, status: 500, error: "internal_error" };
  }

  const info = await storage.headObject(output.relative_path);
  if (!info.exists || info.sizeBytes === null || info.sizeBytes === 0) {
    return { ok: false, status: 410, error: "result_unavailable" };
  }

  return { ok: true, output, filePath, sizeBytes: info.sizeBytes };
}

/** `attachment; filename="..."` header value for a stored output. */
export function contentDispositionFilename(output: JobOutputRow, jobId: string): string {
  const extension = output.relative_path.split(".").pop() ?? "bin";
  const base = output.stem_key === "archive" ? "stemify" : output.stem_key;
  return `${base}-${jobId}.${extension}`;
}

/**
 * Parse a single-range header (RFC 9110 Section 14.1.2). Returns null for
 * absent/multi-range requests (serve the full body), "invalid" for bad or
 * unsatisfiable ranges (416), or the inclusive byte window.
 */
export function parseSingleRange(
  header: string | null,
  sizeBytes: number,
): { start: number; end: number } | "invalid" | null {
  if (!header) return null;
  const match = /^bytes=(\d*)-(\d*)$/i.exec(header.trim());
  if (!match) return null; // multi-range or unsupported unit -> full body
  const [, rawStart, rawEnd] = match;
  if (rawStart === "" && rawEnd === "") return null;

  if (rawStart === "") {
    // suffix range: last N bytes
    const suffixLength = Number(rawEnd);
    if (suffixLength === 0 || !Number.isSafeInteger(suffixLength)) return "invalid";
    if (suffixLength > sizeBytes) return { start: 0, end: sizeBytes - 1 };
    return { start: sizeBytes - suffixLength, end: sizeBytes - 1 };
  }

  const start = Number(rawStart);
  if (!Number.isSafeInteger(start) || start >= sizeBytes) return "invalid";
  const end = rawEnd === "" ? sizeBytes - 1 : Math.min(Number(rawEnd), sizeBytes - 1);
  if (!Number.isSafeInteger(end) || end < start) return "invalid";
  return { start, end };
}
