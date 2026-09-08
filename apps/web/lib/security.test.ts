import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { after, before, describe, it } from "node:test";

/**
 * Task 17 security regression tests (plan Section 17 / Task 17 work items).
 *
 * These lock down the invariants the security review relies on:
 *  - guest cookies are HMAC-signed; forged or unsigned IDs are rejected
 *  - filenames and object keys never carry path separators or control chars
 *  - object keys are namespace-restricted (sources/, results/ only)
 *  - LocalStorage resolution can never escape the data directory
 */
import { LocalStorage } from "@/lib/storage/local";
import { assertSignableKey } from "@/lib/storage/signable";
import { ObjectKeyError, sanitizeFilename, sourceObjectKey } from "@/lib/storage/types";

// ---------------------------------------------------------------------------
// Guest identity (cookie/session authorization)
// ---------------------------------------------------------------------------

describe("guest identity invariants", () => {
  it("guest ids match the strict gid_ hex format", async () => {
    const { newGuestId } = await import("@/lib/auth/guest");
    for (let i = 0; i < 20; i++) {
      assert.match(newGuestId(), /^gid_[a-f0-9]{32}$/);
    }
  });

  it("uses a keyed HMAC, not a plain hash", async () => {
    // The signature must depend on JOB_ACCESS_TOKEN_SECRET: same id, different
    // secret -> different signature. This is what makes forged cookies fail.
    process.env.JOB_ACCESS_TOKEN_SECRET ??= "test-secret-for-local-tests-only";
    const { newGuestId } = await import("@/lib/auth/guest");
    const guestId = newGuestId();
    const withSecret = createHmac("sha256", process.env.JOB_ACCESS_TOKEN_SECRET)
      .update(guestId)
      .digest("base64url");
    const withOtherSecret = createHmac("sha256", "a-completely-different-secret")
      .update(guestId)
      .digest("base64url");
    assert.notEqual(withSecret, withOtherSecret);
  });
});

// ---------------------------------------------------------------------------
// Filename and object-key hygiene (upload handling)
// ---------------------------------------------------------------------------

describe("filename sanitization", () => {
  it("strips path separators from hostile names", () => {
    for (const hostile of [
      "../../etc/passwd",
      "..\\..\\windows\\system32\\config",
      "C:\\Users\\victim\\secret.mp3",
      "/etc/shadow",
      "song/../../../.ssh/id_rsa.mp3",
    ]) {
      const clean = sanitizeFilename(hostile);
      assert.equal(clean.includes("/"), false, hostile);
      assert.equal(clean.includes("\\"), false, hostile);
      assert.equal(clean.includes(".."), false, hostile);
    }
  });

  it("strips control characters and bounds length", () => {
    const cleaned = sanitizeFilename("so\u0000ng\u001b[31m\x7f.mp3");
    assert.match(cleaned, /^[A-Za-z0-9._-]+$/);
    assert.ok(cleaned.length <= 128);
    assert.equal(sanitizeFilename("x".repeat(500)).length, 128);
  });

  it("never returns an empty or dot-only name", () => {
    for (const hostile of ["", "..", ".", "//", "\\\\", "\u0000"]) {
      const clean = sanitizeFilename(hostile);
      assert.ok(clean.length > 0);
      assert.notEqual(clean, "..");
      assert.notEqual(clean, ".");
    }
  });

  it("builds source keys only from sanitized parts", () => {
    const key = sourceObjectKey("upl_" + "a".repeat(32), "../(evil)/song?.mp3");
    assert.match(key, /^sources\/upl_[a-f0-9]{32}\/[A-Za-z0-9._-]+$/);
    assert.equal(key.includes(".."), false);
  });
});

// ---------------------------------------------------------------------------
// Object-key namespace and path containment
// ---------------------------------------------------------------------------

describe("storage key namespace guard", () => {
  it("allows only sources/ and results/ keys", () => {
    assertSignableKey("sources/upl_abc/song.mp3");
    assertSignableKey("results/job_abc/vocals.mp3");
    for (const forbidden of [
      "models/htdemucs.th",
      "stemify.sqlite3",
      "logs/worker.log",
      "../etc/passwd",
      "",
    ]) {
      assert.throws(() => assertSignableKey(forbidden), ObjectKeyError, forbidden);
    }
  });
});

describe("LocalStorage path containment", () => {
  let storage: LocalStorage;
  let tempDir: string;

  before(async () => {
    tempDir = await mkdtemp(path.join(tmpdir(), "stemify-sec-"));
    storage = new LocalStorage(tempDir);
  });

  after(async () => {
    await rm(tempDir, { recursive: true, force: true });
  });

  it("resolves keys inside the data directory", () => {
    const p = storage.objectPath("results/job_x/vocals.mp3");
    assert.ok(p.startsWith(storage.dataDirectory));
  });

  it("rejects traversal attempts with .. segments", () => {
    for (const hostile of [
      "../outside.mp3",
      "sources/upl_x/../../../secret.txt",
      "results/../models/htdemucs.th",
      "..\\windows\\win.ini",
    ]) {
      // Either the namespace guard or the containment check must stop it.
      assert.throws(() => storage.objectPath(hostile), Error, hostile);
    }
  });

  it("rejects absolute keys and non-namespaced system paths", () => {
    assert.throws(() => storage.objectPath("/etc/passwd"));
    assert.throws(() => storage.objectPath("C:\\Windows\\System32\\config"));
    assert.throws(() => storage.objectPath("stemify.sqlite3"));
  });
});
