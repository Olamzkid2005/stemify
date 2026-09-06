import { NextResponse } from "next/server";

import { CLIENT_LIMITS } from "@/lib/limits";
import { getStorage } from "@/lib/storage";

/**
 * POST /api/uploads/presign — issue a short-lived direct-upload URL (plan §11.1).
 * Server-side validation here is authoritative; the client checks are convenience.
 */
export async function POST(request: Request) {
  let body: { filename?: unknown; sizeBytes?: unknown; contentType?: unknown };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "invalid_json" }, { status: 400 });
  }

  const { filename, sizeBytes } = body;
  if (
    typeof filename !== "string" ||
    filename.length === 0 ||
    filename.length > 255 ||
    typeof sizeBytes !== "number" ||
    !Number.isInteger(sizeBytes) ||
    sizeBytes <= 0
  ) {
    return NextResponse.json({ error: "invalid_request" }, { status: 400 });
  }

  if (sizeBytes > CLIENT_LIMITS.maxUploadBytes) {
    return NextResponse.json({ error: "file_too_large" }, { status: 413 });
  }

  // Ownership comes from the server-generated uploadId embedded in the key.
  const uploadId = `upl_${crypto.randomUUID().replaceAll("-", "")}`;
  const storage = getStorage();
  const presigned = await storage.createPresignedUpload({
    uploadId,
    filename,
    expiresIn: 900,
  });

  return NextResponse.json(
    {
      uploadId,
      objectKey: presigned.objectKey,
      uploadUrl: presigned.uploadUrl,
      expiresInSeconds: presigned.expiresInSeconds,
    },
    { status: 201 },
  );
}
