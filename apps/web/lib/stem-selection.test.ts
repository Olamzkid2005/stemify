import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { parseStemSelection, zipSelection } from "@/lib/stem-selection";

const JOB_ID = "job_0123456789abcdef0123456789abcdef";

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

/**
 * The completed-job page's ZIP control (roadmap A3). An empty selection must
 * not become a link: it builds `?stems=`, which the downloads route rejects as
 * a client error, so the button used to answer with a JSON error page.
 */
describe("zipSelection", () => {
  const stems = ["vocals", "instrumental", "drums", "bass"];

  it("offers nothing when no stem is selected", () => {
    assert.deepEqual(zipSelection(JOB_ID, stems, []), { kind: "none" });
    // Also for a job that somehow published no stems at all.
    assert.deepEqual(zipSelection(JOB_ID, [], []), { kind: "none" });
  });

  it("uses the worker archive when every stem is selected", () => {
    assert.deepEqual(zipSelection(JOB_ID, stems, stems), { kind: "all" });
  });

  it("builds a subset URL for a partial selection", () => {
    assert.deepEqual(zipSelection(JOB_ID, stems, ["vocals", "drums"]), {
      kind: "partial",
      url: `/api/jobs/${JOB_ID}/downloads?kind=zip&stems=vocals,drums`,
    });
  });

  it("every offered URL parses back into the same selection", () => {
    const subset = zipSelection(JOB_ID, stems, ["bass", "vocals"]);
    assert.equal(subset.kind, "partial");
    if (subset.kind !== "partial") return;
    const params = new URLSearchParams(subset.url.slice(subset.url.indexOf("?")));
    assert.equal(params.get("kind"), "zip");
    assert.deepEqual(parseStemSelection(params.get("stems")), {
      ok: true,
      stems: ["vocals", "bass"],
    });
  });
});
