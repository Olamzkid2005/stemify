import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { FakeStorage } from "./fake";
import {
  ObjectKeyError,
  assertKeyInPrefix,
  sanitizeFilename,
  sourceObjectKey,
} from "./types";

describe("sanitizeFilename", () => {
  it("strips path traversal", () => {
    assert.equal(sanitizeFilename("../../etc/passwd"), "passwd");
    assert.equal(sanitizeFilename("C:\\Users\\evil\\song.mp3"), "song.mp3");
  });

  it("removes control characters and replaces unsafe characters", () => {
    assert.equal(sanitizeFilename("so\x00ng\x1f.mp3"), "song.mp3");
    assert.equal(sanitizeFilename("my song (final).mp3"), "my_song_final_.mp3");
  });

  it("falls back to 'file' when nothing survives", () => {
    assert.equal(sanitizeFilename(""), "file");
  });
});

describe("assertKeyInPrefix", () => {
  it("accepts keys inside the prefix", () => {
    assert.doesNotThrow(() =>
      assertKeyInPrefix("sources/upl_abc1234567890123/input.mp3", "sources/upl_abc1234567890123"),
    );
  });

  it("rejects keys outside the prefix (unauthorized access)", () => {
    assert.throws(
      () => assertKeyInPrefix("sources/upl_OTHER1234567890/steal.mp3", "sources/upl_abc1234567890123"),
      ObjectKeyError,
    );
    assert.throws(
      () => assertKeyInPrefix("results/job_x/vocals.mp3", "sources/upl_abc1234567890123"),
      ObjectKeyError,
    );
  });
});

describe("FakeStorage", () => {
  it("roundtrips presign -> upload -> head", async () => {
    const storage = new FakeStorage();
    const uploadId = "upl_abc1234567890123";
    const presigned = await storage.createPresignedUpload({
      uploadId,
      filename: "song.mp3",
      expiresIn: 900,
    });
    assert.equal(presigned.objectKey, sourceObjectKey(uploadId, "song.mp3"));
    assert.equal(presigned.uploadUrl, "/api/dev-storage/sources/upl_abc1234567890123/song.mp3?token=upl_abc1234567890123");

    // Simulate the browser PUTting bytes.
    storage.put(presigned.objectKey, Buffer.alloc(1024));
    const info = await storage.headObject(presigned.objectKey);
    assert.equal(info.exists, true);
    assert.equal(info.sizeBytes, 1024);
  });

  it("reports missing objects", async () => {
    const storage = new FakeStorage();
    const info = await storage.headObject("sources/upl_abc1234567890123/never.mp3");
    assert.equal(info.exists, false);
  });

  it("signs downloads only for known namespaces", async () => {
    const storage = new FakeStorage();
    const ok = await storage.createSignedDownload({
      objectKey: "sources/upl_abc1234567890123/input.mp3",
      expiresIn: 300,
    });
    assert.ok(ok.downloadUrl.startsWith("http://fake-storage.local/get/"));

    assert.rejects(
      storage.createSignedDownload({ objectKey: "private/secret.mp3", expiresIn: 300 }),
      ObjectKeyError,
    );
  });

  it("deletes idempotently", async () => {
    const storage = new FakeStorage();
    storage.put("sources/upl_abc1234567890123/input.mp3", Buffer.alloc(4));
    await storage.deleteObject("sources/upl_abc1234567890123/input.mp3");
    await storage.deleteObject("sources/upl_abc1234567890123/input.mp3");
    const info = await storage.headObject("sources/upl_abc1234567890123/input.mp3");
    assert.equal(info.exists, false);
  });
});
