/**
 * Storage abstraction (plan Task 4 / Section 6.4).
 *
 * Hides the object-storage provider from application code. Implementations:
 *  - `r2.ts`  — S3-compatible live adapter (Cloudflare R2 or AWS S3)
 *  - `fake.ts` — in-memory adapter for tests and local development
 *
 * Rules from the plan:
 *  - Objects are private; downloads happen through short-lived signed URLs.
 *  - Object keys are server-generated; never accept raw keys from untrusted input.
 *  - Presigned upload URLs are short-lived and bound to one key.
 */

/** Raised when an object key does not belong to the expected namespace. */
export class ObjectKeyError extends Error {
  constructor(key: string, prefix: string) {
    super(`object key "${key}" does not belong to prefix "${prefix}"`);
    this.name = "ObjectKeyError";
  }
}

/** Strict prefix ownership check — the plan's "never accept arbitrary keys" rule. */
export function assertKeyInPrefix(key: string, prefix: string): void {
  if (!key.startsWith(prefix.endsWith("/") ? prefix : `${prefix}/`)) {
    throw new ObjectKeyError(key, prefix);
  }
}

export type PresignedUpload = {
  /** URL the browser PUTs the file bytes to, directly against storage. */
  uploadUrl: string;
  /** Server-owned key the file will live at. */
  objectKey: string;
  /** Seconds until the upload URL stops working. */
  expiresInSeconds: number;
};

export type ObjectInfo = {
  exists: boolean;
  sizeBytes: number | null;
};

export type SignedDownload = {
  downloadUrl: string;
  expiresInSeconds: number;
};

export interface StorageAdapter {
  /** Presign a direct browser upload bound to one server-generated key. */
  createPresignedUpload(input: {
    uploadId: string;
    filename: string;
    expiresIn: number;
  }): Promise<PresignedUpload>;

  /** Short-lived signed GET for an object the caller is authorized to read. */
  createSignedDownload(input: {
    objectKey: string;
    expiresIn: number;
    /** Signed responses must be safe to stream/range-request for audio previews. */
    filename?: string;
  }): Promise<SignedDownload>;

  /** Metadata check used to verify an upload landed before a job is created. */
  headObject(objectKey: string): Promise<ObjectInfo>;

  /** Remove one object. Idempotent: deleting a missing object is success. */
  deleteObject(objectKey: string): Promise<void>;
}

/** Canonical, server-owned source object keys. */
export function sourceObjectKey(uploadId: string, filename: string): string {
  const safeName = sanitizeFilename(filename);
  return `sources/${uploadId}/${safeName}`;
}

/** Canonical, server-owned result object keys. */
export function resultObjectKey(jobId: string, stem: string, ext: string): string {
  return `results/${jobId}/${sanitizeFilename(stem)}.${sanitizeExt(ext)}`;
}

const CONTROL_CHARS = /[\x00-\x1f\x7f]/g;
const UNSAFE_CHARS = /[^A-Za-z0-9._-]+/g;

/** Filenames may appear in URLs/headers; strip everything unsafe (plan §32.15). */
export function sanitizeFilename(name: string): string {
  const base = name.split(/[\\/]/).pop() ?? "file";
  const cleaned = base.replace(CONTROL_CHARS, "").replace(UNSAFE_CHARS, "_");
  return cleaned.length > 0 ? cleaned.slice(0, 128) : "file";
}

function sanitizeExt(ext: string): string {
  return ext.replace(UNSAFE_CHARS, "").slice(0, 8) || "bin";
}
