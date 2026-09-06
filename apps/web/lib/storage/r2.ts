/**
 * Live S3-compatible adapter (Cloudflare R2 by default; AWS S3 works as-is).
 *
 * All objects stay private — the adapter only ever hands out short-lived
 * signed URLs, never public ones (plan Section 6.4/15.2).
 */
import {
  DeleteObjectCommand,
  GetObjectCommand,
  HeadObjectCommand,
  PutObjectCommand,
  S3Client,
} from "@aws-sdk/client-s3";
import { ObjectKeyError } from "./types";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";
import {
  assertKeyInPrefix,
  sanitizeFilename,
  sourceObjectKey,
  type ObjectInfo,
  type PresignedUpload,
  type SignedDownload,
  type StorageAdapter,
} from "./types";

/** Keys the adapter will sign or delete — sources and results only, ever. */
function assertSignableKey(objectKey: string): void {
  if (objectKey.startsWith("sources/")) return;
  if (objectKey.startsWith("results/")) return;
  throw new ObjectKeyError(objectKey, "sources/ or results/");
}

export type R2Config = {
  accountId: string;
  accessKeyId: string;
  secretAccessKey: string;
  bucket: string;
};

export function r2ConfigFromEnv(): R2Config {
  const accountId = process.env.STORAGE_ACCOUNT_ID;
  const accessKeyId = process.env.STORAGE_ACCESS_KEY_ID;
  const secretAccessKey = process.env.STORAGE_SECRET_ACCESS_KEY;
  const bucket = process.env.STORAGE_BUCKET;
  if (!accountId || !accessKeyId || !secretAccessKey || !bucket) {
    throw new Error(
      "Storage is not configured: set STORAGE_ACCOUNT_ID, STORAGE_ACCESS_KEY_ID, STORAGE_SECRET_ACCESS_KEY, STORAGE_BUCKET.",
    );
  }
  return { accountId, accessKeyId, secretAccessKey, bucket };
}

export class R2Storage implements StorageAdapter {
  private readonly client: S3Client;
  private readonly bucket: string;

  constructor(config: R2Config) {
    this.bucket = config.bucket;
    this.client = new S3Client({
      region: "auto",
      endpoint: `https://${config.accountId}.r2.cloudflarestorage.com`,
      credentials: {
        accessKeyId: config.accessKeyId,
        secretAccessKey: config.secretAccessKey,
      },
    });
  }

  async createPresignedUpload(input: {
    uploadId: string;
    filename: string;
    expiresIn: number;
  }): Promise<PresignedUpload> {
    const objectKey = sourceObjectKey(input.uploadId, input.filename);
    const command = new PutObjectCommand({
      Bucket: this.bucket,
      Key: objectKey,
    });
    const uploadUrl = await getSignedUrl(this.client, command, {
      expiresIn: input.expiresIn,
    });
    return { uploadUrl, objectKey, expiresInSeconds: input.expiresIn };
  }

  async createSignedDownload(input: {
    objectKey: string;
    expiresIn: number;
    filename?: string;
  }): Promise<SignedDownload> {
    // Authorization (job ownership, expiry) is enforced by the API route
    // before calling this; the adapter only signs known namespaces.
    assertSignableKey(input.objectKey);
    const command = new GetObjectCommand({
      Bucket: this.bucket,
      Key: input.objectKey,
      ...(input.filename
        ? { ResponseContentDisposition: `attachment; filename="${sanitizeFilename(input.filename)}"` }
        : {}),
    });
    const downloadUrl = await getSignedUrl(this.client, command, {
      expiresIn: input.expiresIn,
    });
    return { downloadUrl, expiresInSeconds: input.expiresIn };
  }

  async headObject(objectKey: string): Promise<ObjectInfo> {
    try {
      const out = await this.client.send(
        new HeadObjectCommand({ Bucket: this.bucket, Key: objectKey }),
      );
      return { exists: true, sizeBytes: out.ContentLength ?? null };
    } catch (err) {
      const name = (err as { name?: string }).name;
      if (name === "NotFound" || name === "404") {
        return { exists: false, sizeBytes: null };
      }
      throw err;
    }
  }

  async deleteObject(objectKey: string): Promise<void> {
    assertSignableKey(objectKey);
    await this.client.send(
      new DeleteObjectCommand({ Bucket: this.bucket, Key: objectKey }),
    );
  }
}
