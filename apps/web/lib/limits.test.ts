import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";

import {
  DEFAULT_LIMITS,
  maxDurationForMode,
  serverEffectiveLimits,
  uploadErrorMessage,
  validateFileSelection,
} from "@/lib/limits";

/**
 * Product-limit tests (plan Section 9).
 *
 * These caps are documented in `.env.example` and exported by start.sh to both
 * processes. They used to be read by nothing, which is indistinguishable from
 * having no limit: these pin the reading, the fallbacks and the mode split.
 */

const ENV_NAMES = [
  "MAX_UPLOAD_BYTES",
  "MAX_DURATION_SECONDS",
  "MAX_FULL_STEMS_DURATION_SECONDS",
] as const;

function clearLimitEnv(): void {
  for (const name of ENV_NAMES) delete process.env[name];
}

function audioFile(bytes: number, name = "song.mp3", type = "audio/mpeg"): File {
  return new File([new Uint8Array(bytes)], name, { type });
}

describe("effective limits (plan Section 9)", () => {
  afterEach(clearLimitEnv);

  it("uses the shipped defaults when nothing is configured", () => {
    clearLimitEnv();
    assert.deepEqual(serverEffectiveLimits(), {
      maxUploadBytes: 100 * 1024 * 1024,
      maxDurationSeconds: 480,
      fullStemsMaxDurationSeconds: 360,
    });
  });

  it("honors the variables the launcher exports to both processes", () => {
    process.env.MAX_UPLOAD_BYTES = "52428800";
    process.env.MAX_DURATION_SECONDS = "600";
    process.env.MAX_FULL_STEMS_DURATION_SECONDS = "120";
    assert.deepEqual(serverEffectiveLimits(), {
      maxUploadBytes: 52_428_800,
      maxDurationSeconds: 600,
      fullStemsMaxDurationSeconds: 120,
    });
  });

  it("keeps the default for a value that cannot be a cap", () => {
    // A typo must not switch a limit off, so every unusable value falls back.
    for (const value of ["", "   ", "abc", "0", "-1", "Infinity", "NaN"]) {
      for (const name of ENV_NAMES) process.env[name] = value;
      assert.deepEqual(serverEffectiveLimits(), DEFAULT_LIMITS, `value ${JSON.stringify(value)}`);
    }
  });
});

describe("mode-specific duration cap (plan Section 9)", () => {
  afterEach(clearLimitEnv);

  it("caps the 3-stem split below the 2-stem split", () => {
    clearLimitEnv();
    assert.ok(
      DEFAULT_LIMITS.fullStemsMaxDurationSeconds < DEFAULT_LIMITS.maxDurationSeconds,
      "the heavier split must be the tighter cap",
    );
    assert.equal(maxDurationForMode("vocals_instrumental"), DEFAULT_LIMITS.maxDurationSeconds);
    assert.equal(maxDurationForMode("full_stems"), DEFAULT_LIMITS.fullStemsMaxDurationSeconds);
  });

  it("follows the configured values", () => {
    process.env.MAX_DURATION_SECONDS = "600";
    process.env.MAX_FULL_STEMS_DURATION_SECONDS = "300";
    const limits = serverEffectiveLimits();
    assert.equal(maxDurationForMode("full_stems", limits), 300);
    assert.equal(maxDurationForMode("vocals_instrumental", limits), 600);
    // Refine consumes a worker-produced stem, never longer than the upload that
    // already passed the ladder, so it takes the general cap.
    assert.equal(maxDurationForMode("drum_breakdown", limits), 600);
  });
});

describe("client-side file selection", () => {
  it("compares against the cap it is handed, not a baked-in one", () => {
    assert.deepEqual(validateFileSelection(audioFile(4096), 1024), {
      ok: false,
      reason: "too-large",
    });
    assert.deepEqual(validateFileSelection(audioFile(4096), 4096), { ok: true });
    assert.deepEqual(validateFileSelection(audioFile(2048)), { ok: true });
  });

  it("refuses a type the worker would refuse", () => {
    assert.deepEqual(validateFileSelection(audioFile(10, "clip.mp4", "video/mp4")), {
      ok: false,
      reason: "invalid-type",
    });
    assert.deepEqual(validateFileSelection(audioFile(10, "song.flac", "")), { ok: true });
  });
});

describe("upload refusal messages", () => {
  it("names the reason for each stable error code", () => {
    assert.match(uploadErrorMessage(413, { error: "file_too_large" }), /size limit/i);
    assert.match(
      uploadErrorMessage(400, { error: "unsupported_file_type" }),
      /MP3, WAV, FLAC, OGG, or M4A/,
    );
    assert.match(uploadErrorMessage(400, { error: "invalid_multipart" }), /Try again/);
    assert.match(uploadErrorMessage(500, { error: "upload_failed" }), /could not store/i);
    assert.match(uploadErrorMessage(403, null), /session/i);
    assert.match(uploadErrorMessage(429, null), /already running/i);
  });

  it("falls back without inventing a reason", () => {
    assert.equal(uploadErrorMessage(418, null), "Upload failed. Try again.");
    assert.equal(uploadErrorMessage(418, "not json"), "Upload failed. Try again.");
    assert.equal(uploadErrorMessage(400, { error: "brand_new_code" }), "Upload failed. Try again.");
  });
});
