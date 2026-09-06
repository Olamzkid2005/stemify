/**
 * In-memory fake storage adapter for tests and local development (plan Task 4:
 * "Implement a fake adapter for tests"). Mirrors the real adapter's semantics:
 * private objects, presigned uploads bound to one key, ownership-checked keys.
 */

import {
  assertKeyInPrefix,
  sourceObjectKey,
  type ObjectInfo,
  type PresignedUpload,
  type SignedDownload,
  type StorageAdapter,
} from "./types";

type StoredObject = { sizeBytes: number; data: Buffer };

export class FakeStorage implements StorageAdapter {
  private readonly objects = new Map<string, StoredObject>();
  private readonly validUploadTokens = new Set<string>();

  /** Test helper: pre-seed an object as if it had been uploaded. */
  put(objectKey: string, data: Buffer): void {
    this.objects.set(objectKey, { sizeBytes: data.length, data });
  }

  /** Test helper: mark an uploadId as legitimately issued by the server. */
  issueUploadId(uploadId: string): void {
    this.validUploadTokens.add(uploadId);
  }

  async createPresignedUpload(input: {
    uploadId: string;
    filename: string;
    expiresIn: number;
  }): Promise<PresignedUpload> {
    this.validUploadTokens.add(input.uploadId);
    const objectKey = sourceObjectKey(input.uploadId, input.filename);
    // Relative URL to the dev-only storage route; a real browser can reach it.
    return {
      uploadUrl: `/api/dev-storage/${objectKey}?token=${input.uploadId}`,
      objectKey,
      expiresInSeconds: input.expiresIn,
    };
  }

  async createSignedDownload(input: {
    objectKey: string;
    expiresIn: number;
    filename?: string;
  }): Promise<SignedDownload> {
    assertKeyInPrefix(input.objectKey, "sources");
    return {
      downloadUrl: `http://fake-storage.local/get/${encodeURIComponent(input.objectKey)}?exp=${input.expiresIn}`,
      expiresInSeconds: input.expiresIn,
    };
  }

  async headObject(objectKey: string): Promise<ObjectInfo> {
    const obj = this.objects.get(objectKey);
    return obj
      ? { exists: true, sizeBytes: obj.sizeBytes }
      : { exists: false, sizeBytes: null };
  }

  async deleteObject(objectKey: string): Promise<void> {
    this.objects.delete(objectKey);
  }
}
