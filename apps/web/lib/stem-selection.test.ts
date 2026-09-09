import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { parseStemSelection } from "@/lib/stem-selection";

/** Locks the allowlist contract for custom ZIP selection (roadmap A3). */
describe("parseStemSelection", () => {
  it("parses, dedupes, and normalizes a selection into STEM_KEYS order", () => {
    assert.deepEqual(parseStemSelection("drums,vocals,drums"), {
      ok: true,
      stems: ["vocals", "drums"],
    });
    assert.deepEqual(parseStemSelection(" VOCALS , bass "), {
      ok: true,
      stems: ["vocals", "bass"],
    });
  });

  it("rejects absent, blank, and unknown selections", () => {
    assert.deepEqual(parseStemSelection(null), { ok: false, reason: "missing" });
    assert.deepEqual(parseStemSelection("   "), { ok: false, reason: "missing" });
    assert.deepEqual(parseStemSelection(",,,,"), { ok: false, reason: "missing" });
    assert.deepEqual(parseStemSelection("vocals,cowbell"), { ok: false, reason: "unknown" });
    // archive is reserved and not in the stem allowlist
    assert.deepEqual(parseStemSelection("archive"), { ok: false, reason: "unknown" });
  });
});
