import { randomUUID } from "node:crypto";
import { NextResponse } from "next/server";

import { getOrCreateGuestId } from "@/lib/auth/guest";
import { db } from "@/lib/db/client";
import { CLIENT_LIMITS } from "@/lib/limits";
import { getLocalStorage } from "@/lib/storage";
import { sanitizeFilename, sourceObjectKey } from "@/lib/storage/types";

const UPLOAD_ID_PATTERN = /^upl_[a-f0-9]{32}$/;

/** POST /api/uploads — receive one audio file into local storage. */
export async function POST(request: Request) {
  let form: FormData;
  try {
    form = await request.formData();
  } catch {
    return NextResponse.json({ error: "invalid_multipart" }, { status: 400 });
  }

  const file = form.get("file");
  if (!(file instanceof File) || file.size <= 0) {
    return NextResponse.json({ error: "file_required" }, { status: 400 });
  }
  if (file.size > CLIENT_LIMITS.maxUploadBytes) {
    return NextResponse.json({ error: "file_too_large" }, { status: 413 });
  }

  const filename = sanitizeFilename(file.name);
  if (!isSupportedAudioFilename(filename)) {
    return NextResponse.json({ error: "unsupported_file_type" }, { status: 400 });
  }

  const ownerKey = await getOrCreateGuestId();
  const uploadId = `upl_${randomUUID().replaceAll("-", "")}`;
  if (!UPLOAD_ID_PATTERN.test(uploadId)) {
    return NextResponse.json({ error: "internal_error" }, { status: 500 });
  }

  const objectKey = sourceObjectKey(uploadId, filename);
  const storage = getLocalStorage();
  try {
    await storage.putObject(objectKey, new Uint8Array(await file.arrayBuffer()));
    db.run(
      `INSERT INTO uploads (id, owner_key, filename, object_key, size_bytes, expires_at)
       VALUES (?, ?, ?, ?, ?, ?)`,
      uploadId,
      ownerKey,
      filename,
      objectKey,
      file.size,
      Date.now() + 24 * 60 * 60 * 1000,
    );
  } catch {
    await storage.deleteObject(objectKey).catch(() => undefined);
    return NextResponse.json({ error: "upload_failed" }, { status: 500 });
  }

  return NextResponse.json(
    { uploadId, filename, sizeBytes: file.size, status: "uploaded" },
    { status: 201 },
  );
}

function isSupportedAudioFilename(filename: string): boolean {
  return [".mp3", ".wav", ".flac", ".ogg", ".m4a"].some((extension) =>
    filename.toLowerCase().endsWith(extension),
  );
}
