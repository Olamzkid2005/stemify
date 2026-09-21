"use client";

/**
 * Worker pool strip on the home page (concurrency plan C4).
 *
 * Answers, before anything is submitted, what the machine is about to do with
 * the job: how many workers are live, how many jobs they run at once, and how
 * much RAM that pool needs. Every number comes from the serverside pool status
 * riding on the same poll as the job list (lib/worker-pool.ts), including the
 * RAM figures: per-worker guidance is measured rather than guessed, and the free
 * figure is read off the machine so "needs about 2 GB" can become "only 1.2 GB
 * free" instead of a plan that quietly swaps.
 *
 * Renders nothing until the first successful poll: "no worker running" is a
 * claim, and it must not be made before the app has actually looked.
 */
import { useJobs } from "@/components/jobs-context";

/** Guidance for a pool: rounded, because it is an estimate to plan with. */
function ramText(totalMb: number): string {
  if (totalMb >= 1024) {
    const gb = Math.round(totalMb / 1024);
    return `about ${gb} GB`;
  }
  return `about ${totalMb} MB`;
}

/**
 * Free memory as measured: a decimal, because this one is read, not planned
 * with. Rounding it would either overstate the shortage or hide it.
 */
function freeRamText(mb: number): string {
  if (mb >= 1024) return `${(mb / 1024).toFixed(1).replace(/\.0$/, "")} GB`;
  return `${Math.round(mb)} MB`;
}

function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

export function PoolStatus() {
  const { pool, error } = useJobs();
  if (!pool) return null;

  const { configured, running, threadsPerWorker, ramTotalMb, freeRamMb } = pool;
  const allUp = running >= configured;
  const none = running === 0;

  const headline = none
    ? "No worker running"
    : allUp
      ? `${plural(configured, "worker")} ready`
      : `${running} of ${configured} workers running`;

  // Ordered by what a person is deciding about: how many jobs run at once, then
  // the resources the pool needs. The thread split is omitted, never guessed,
  // when the launcher did not export one.
  const details = [
    none
      ? "jobs stay queued until the worker starts"
      : configured === 1
        ? "one job at a time"
        : `up to ${configured} jobs at once`,
    threadsPerWorker === null ? null : `${plural(threadsPerWorker, "thread")} each`,
  ].filter((part): part is string => part !== null);

  // RAM gets its own element because it is the one detail that can be a warning
  // rather than a fact. The strip's colour already means worker liveness, and a
  // single colour cannot honestly carry two unrelated states — a machine short
  // of memory while both workers run is still "two workers ready".
  const ramDetail =
    freeRamMb !== null && freeRamMb < ramTotalMb ? (
      <span className="text-amber-300/90" data-testid="pool-ram-warning">
        <span aria-hidden="true">⚠ </span>
        only {freeRamText(freeRamMb)} RAM free, but the pool needs {ramText(ramTotalMb)}
      </span>
    ) : (
      <span>{ramText(ramTotalMb)} RAM for the pool</span>
    );

  // A failed refresh keeps the last known numbers; saying so is better than
  // letting a stale count read as current.
  const stale = error ? " (last known)" : "";

  // The headline's three states, three colours: nothing running is a problem to
  // fix, a short pool still works, a full pool is the quiet normal case. The
  // memory warning below has its own colour, so it neither overrides nor is
  // mistaken for any of these.
  const tone = none ? "text-rose-300/90" : allUp ? "text-zinc-500" : "text-amber-300/90";
  const dot = none ? "bg-rose-400/80" : allUp ? "bg-emerald-400" : "bg-amber-400";

  return (
    <div
      className={`mt-6 flex flex-wrap items-center justify-center gap-x-2 gap-y-1 text-[11px] ${tone}`}
      data-testid="pool-status"
      role="status"
    >
      <span aria-hidden="true" className={`inline-block h-1.5 w-1.5 rounded-full ${dot}`} />
      <span className="font-semibold tracking-wide text-zinc-400">
        {headline}
      </span>
      <span className="text-zinc-700" aria-hidden="true">
        ·
      </span>
      <span>
        {details.join(" · ")}
        <span className="text-zinc-700" aria-hidden="true">
          {" · "}
        </span>
        {ramDetail}
        {stale}
      </span>
    </div>
  );
}
