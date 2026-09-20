/**
 * Per-stage wall-clock timing for the job page (plan §8.4).
 *
 * The worker stamps every progress event with the stage it belongs to, so the
 * first event for a stage is when that stage began and the next stage's first
 * event is when it ended. There is no separate stage-transition event, so this
 * derivation is the only per-stage timing the database supports. A stage is
 * left open (no `endedAt`) while it runs, which lets the page tick it live —
 * that is what tells the user a slow separation has been going for 6 minutes
 * rather than looking stalled.
 *
 * Pure and DB-free: the server derives, the client formats, and both use the
 * same span arithmetic.
 */

export type StageTiming = {
  stage: string;
  startedAt: string;
  /** Absent while the stage is still running, or while the job is active. */
  endedAt?: string;
};

export type StageStart = { stage: string; startedAt: number };

/** The queue wait before the worker's first step is a stage of its own. */
const STARTING_STAGE = "starting";

/**
 * Chain stage starts into spans. `createdAt` anchors the implicit `starting`
 * stage: for a job whose worker never came up, that wait is the whole story.
 * `endedAt` closes the last span for a terminal job; pass null while the job is
 * still active so the running stage keeps its elapsed time growing.
 */
export function deriveStageTimings(
  createdAt: number,
  stageStarts: StageStart[],
  endedAt: number | null,
): StageTiming[] {
  // Defensive: if the worker ever emits an explicit `starting` event, prefer it
  // over the created_at anchor instead of emitting the stage twice.
  const explicitStarting = stageStarts.find((start) => start.stage === STARTING_STAGE);
  const spans: StageStart[] = [
    { stage: STARTING_STAGE, startedAt: explicitStarting?.startedAt ?? createdAt },
    ...stageStarts.filter((start) => start.stage !== STARTING_STAGE),
  ];

  return spans.map((span, index) => {
    const next = spans[index + 1];
    const end = next ? next.startedAt : endedAt;
    // Clamp a close time that precedes the start (clock skew between the worker
    // and the web process): a finished stage must report a duration, never a
    // live-ticking one.
    const closedEnd = end === null ? null : Math.max(end, span.startedAt);
    return {
      stage: span.stage,
      startedAt: new Date(span.startedAt).toISOString(),
      ...(closedEnd === null ? {} : { endedAt: new Date(closedEnd).toISOString() }),
    };
  });
}

/** Elapsed time for one stage; open stages measure up to `now`. */
export function stageElapsedMs(timing: StageTiming, now: number): number {
  const started = Date.parse(timing.startedAt);
  if (!Number.isFinite(started)) return 0;
  const ended = timing.endedAt ? Date.parse(timing.endedAt) : now;
  return Math.max(0, ended - started);
}

/**
 * Compact elapsed time: "8s", "4m 12s", "1h 05m". Rounds to whole seconds and
 * never reports a negative span.
 */
export function formatElapsed(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${String(seconds % 60).padStart(2, "0")}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}
