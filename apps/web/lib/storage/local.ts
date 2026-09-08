import { mkdir, readFile, stat, unlink, writeFile } from "node:fs/promises";
import path from "node:path";

import { assertSignableKey } from "./signable";
import { sanitizeFilename, sourceObjectKey } from "./types";
import type { ObjectInfo, PresignedUpload, SignedDownload, StorageAdapter } from "./types";

function configuredDataDirectory(): string {
  return path.resolve(process.env.STEMIFY_DATA_DIR ?? path.join(process.cwd(), "data"));
}

export class LocalStorage implements StorageAdapter {
  readonly dataDirectory: string;

  constructor(dataDirectory = configuredDataDirectory()) {
    this.dataDirectory = path.resolve(dataDirectory);
  }

  objectPath(objectKey: string): string {
    assertSignableKey(objectKey);
    // Reject any `..` segment outright: resolving them would let a namespaced
    // key like `results/../models/x.th` reach sibling directories of the data
    // directory that are still outside the object namespace.
    const segments = objectKey.split("/");
    if (segments.some((segment) => segment === ".." || segment === ".")) {
      throw new Error("object key must not contain path navigation segments");
    }
    const resolved = path.resolve(this.dataDirectory, objectKey);
    const relative = path.relative(this.dataDirectory, resolved);
    if (relative.startsWith("..") || path.isAbsolute(relative)) {
      throw new Error("object key escapes the local data directory");
    }
    return resolved;
  }

  async putObject(objectKey: string, data: Uint8Array): Promise<ObjectInfo> {
    const destination = this.objectPath(objectKey);
    await mkdir(path.dirname(destination), { recursive: true });
    await writeFile(destination, data, { flag: "wx" });
    return { exists: true, sizeBytes: data.byteLength };
  }

  async createPresignedUpload(input: {
    uploadId: string;
    filename: string;
    expiresIn: number;
  }): Promise<PresignedUpload> {
    const objectKey = sourceObjectKey(input.uploadId, input.filename);
    return {
      uploadUrl: "/api/uploads",
      objectKey,
      expiresInSeconds: input.expiresIn,
    };
  }

  async createSignedDownload(input: {
    objectKey: string;
    expiresIn: number;
    filename?: string;
  }): Promise<SignedDownload> {
    this.objectPath(input.objectKey);
    return {
      downloadUrl: `/api/local-files?key=${encodeURIComponent(input.objectKey)}${
        input.filename ? `&filename=${encodeURIComponent(sanitizeFilename(input.filename))}` : ""
      }`,
      expiresInSeconds: input.expiresIn,
    };
  }

  async headObject(objectKey: string): Promise<ObjectInfo> {
    try {
      const result = await stat(this.objectPath(objectKey));
      return { exists: result.isFile(), sizeBytes: result.size };
    } catch (error) {
      const code = (error as { code?: string }).code;
      if (code === "ENOENT" || code === "ENOTDIR") return { exists: false, sizeBytes: null };
      throw error;
    }
  }

  async readObject(objectKey: string): Promise<Buffer> {
    return readFile(this.objectPath(objectKey));
  }

  async deleteObject(objectKey: string): Promise<void> {
    try {
      await unlink(this.objectPath(objectKey));
    } catch (error) {
      const code = (error as { code?: string }).code;
      if (code !== "ENOENT") throw error;
    }
  }
}
