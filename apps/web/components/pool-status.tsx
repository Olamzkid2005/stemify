"use client";

/**
 * Worker pool strip on the home page (concurrency plan C4).
 *
 * Answers, before anything is submitted, what the machine is about to do with
 * the job: how many workers are live, how many jobs they run at once, and how
 * much RAM that pool needs. Every number comes from the serverside pool status
 * riding on the same poll as the job list (lib/worker-pool.ts), including the
 * RAM figure, which is measured per worker rather than guessed per pool.
 *
 * Renders nothing until the first successful poll: "no worker running" is a
 * claim, and it must not be made before the app has actually looked.
 */
import { useJobs } from "@/components/jobs-context";

function ramText(totalMb: number): string {
  if (totalMb >= 1024) {
    const gb = Math.round(totalMb / 1024);
    return `about ${gb} GB`;
  }
  return `about ${totalMb} MB`;
}

function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

export function PoolStatus() {
  const { pool, error } = useJobs();
  if (!pool) return null;

  const { configured, running, threadsPerWorker, ramTotalMb } = pool;
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
    `${ramText(ramTotalMb)} RAM for the pool`,
  ].filter((part): part is string => part !== null);

  // A failed refresh keeps the last known numbers; saying so is better than
  // letting a stale count read as current.
  const stale = error ? " (last known)" : "";

  // Three states, three colours: nothing running is a problem to fix, a short
  // pool still works, a full pool is the quiet normal case.
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
        {stale}
      </span>
    </div>
  );
}
