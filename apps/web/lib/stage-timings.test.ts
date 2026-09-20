import assert from "node:assert/strict";
import { describe, it } from "node:test";

/**
 * Per-stage elapsed timing (plan §8.4): the job page shows how long each stage
 * took — and how long the running one has been going — so a slow separation
 * does not look stalled.
 *
 * Pure module, no database: the same arithmetic backs the server's derivation
 * and the client's live tick.
 */
import { deriveStageTimings, formatElapsed, stageElapsedMs } from "@/lib/stage-timings";

const T0 = Date.parse("2026-09-20T10:00:00.000Z");
const at = (seconds: number) => T0 + seconds * 1000;
const iso = (seconds: number) => new Date(at(seconds)).toISOString();

describe("deriveStageTimings", () => {
  it("anchors a queued job on submission, with nothing closed yet", () => {
    assert.deepEqual(deriveStageTimings(T0, [], null), [
      { stage: "starting", startedAt: iso(0) },
    ]);
  });

  it("closes each stage when the next one starts", () => {
    const timings = deriveStageTimings(
      T0,
      [
        { stage: "validating", startedAt: at(5) },
        { stage: "separating", startedAt: at(20) },
      ],
      null,
    );
    assert.deepEqual(timings, [
      { stage: "starting", startedAt: iso(0), endedAt: iso(5) },
      { stage: "validating", startedAt: iso(5), endedAt: iso(20) },
      { stage: "separating", startedAt: iso(20) },
    ]);
  });

  it("leaves the running stage open so the page can tick it", () => {
    const timings = deriveStageTimings(T0, [{ stage: "separating", startedAt: at(30) }], null);
    const running = timings[timings.length - 1];
    assert.equal(running.endedAt, undefined);
    assert.equal(stageElapsedMs(running, at(30 + 252)), 252_000);
  });

  it("closes the last stage when a terminal job ends, freezing its duration", () => {
    const timings = deriveStageTimings(T0, [{ stage: "separating", startedAt: at(30) }], at(90));
    const last = timings[timings.length - 1];
    assert.equal(last.endedAt, iso(90));
    assert.equal(stageElapsedMs(last, at(9999)), 60_000);
  });

  it("never leaves a finished stage ticking when a close time precedes its start", () => {
    // Clock skew between the worker and the web process must not produce a
    // negative span, nor a finished stage that keeps counting.
    const timings = deriveStageTimings(T0, [{ stage: "separating", startedAt: at(30) }], at(10));
    const last = timings[timings.length - 1];
    assert.equal(last.endedAt, iso(30));
    assert.equal(stageElapsedMs(last, at(9999)), 0);
  });

  it("prefers an explicit starting event over the submission anchor", () => {
    const timings = deriveStageTimings(
      T0,
      [
        { stage: "starting", startedAt: at(3) },
        { stage: "validating", startedAt: at(9) },
      ],
      null,
    );
    assert.deepEqual(
      timings.map((timing) => timing.stage),
      ["starting", "validating"],
    );
    assert.equal(timings[0].startedAt, iso(3));
    assert.equal(timings[0].endedAt, iso(9));
  });

  it("reports no elapsed time for an unparseable timestamp", () => {
    assert.equal(stageElapsedMs({ stage: "separating", startedAt: "not-a-date" }, at(10)), 0);
  });
});

describe("formatElapsed", () => {
  it("formats seconds, minutes and hours", () => {
    assert.equal(formatElapsed(0), "0s");
    assert.equal(formatElapsed(8_400), "8s");
    assert.equal(formatElapsed(252_000), "4m 12s");
    assert.equal(formatElapsed(3_930_000), "1h 05m");
  });

  it("clamps a negative span instead of printing a minus sign", () => {
    assert.equal(formatElapsed(-5_000), "0s");
  });
});
