"use client";

/**
 * "Your jobs" list on the home page (concurrency plan C4).
 *
 * Reads the shared poller (jobs-context) so it always agrees with the warning
 * the picker shows. Renders nothing until there is at least one job: a first-run
 * home page must look exactly as it did before the list existed.
 */
import Link from "next/link";

import { useJobs } from "@/components/jobs-context";
import { isActiveStatus, type JobListItem } from "@/lib/job-list-types";
import { modeStemSummary } from "@/lib/limits";

const STATUS_STYLES: Record<string, string> = {
  queued: "border-zinc-700 text-zinc-400",
  processing: "border-purple-700/70 text-purple-300",
  completed: "border-emerald-800/70 text-emerald-300",
  failed: "border-red-900/70 text-red-300",
  canceled: "border-zinc-800 text-zinc-500",
  expired: "border-zinc-800 text-zinc-500",
};

function statusLabel(job: JobListItem): string {
  if (job.status === "queued") return "Queued";
  if (job.status === "processing") return job.userStage;
  if (job.status === "completed") return "Ready";
  if (job.status === "failed") return "Failed";
  if (job.status === "canceled") return "Canceled";
  if (job.status === "expired") return "Expired";
  return job.status;
}

/**
 * Where a waiting job sits. Position 1 means the next one a worker will claim —
 * "next in line" rather than "starting now", because another job may still be
 * running, and it is honest about not having started yet.
 */
function queueText(job: JobListItem): string | null {
  if (job.status !== "queued" || job.queuePosition === undefined) return null;
  const ahead = job.queuePosition - 1;
  if (ahead === 0) return "Queued — next in line";
  return `Queued — ${ahead} ${ahead === 1 ? "job" : "jobs"} ahead`;
}

function detailLine(job: JobListItem): string {
  const stemSummary = modeStemSummary(job.mode);
  if (job.status === "failed") return job.progressMessage || "Something went wrong";
  if (isActiveStatus(job.status)) return `${stemSummary} · ${job.progressMessage}`;
  return `${stemSummary} · ${job.outputFormat.toUpperCase()}`;
}

function JobRow({ job }: { job: JobListItem }) {
  const filename = job.source.filename ?? "Uploaded audio";
  const active = isActiveStatus(job.status);
  const queue = queueText(job);
  return (
    <li className="rounded-xl border border-zinc-800/80 bg-[#121215]/80 transition-colors hover:border-zinc-700">
      <Link className="flex items-center gap-3 p-3" href={`/jobs/${job.jobId}`}>
        <div className="h-10 w-10 shrink-0 overflow-hidden rounded-lg border border-zinc-800 bg-zinc-900">
          {job.source.artworkUrl ? (
            // Local, already-owner-checked route; a plain img keeps it simple
            // (the same choice the job page makes for its cover).
            // eslint-disable-next-line @next/next/no-img-element
            <img alt="" className="h-full w-full object-cover" src={job.source.artworkUrl} />
          ) : (
            <div className="flex h-full w-full items-center justify-center text-[10px] font-bold text-zinc-600">
              {job.outputFormat.toUpperCase()}
            </div>
          )}
        </div>

        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-zinc-200">{filename}</p>
          <p className="mt-0.5 truncate text-[11px] text-zinc-500">
            {queue ?? detailLine(job)}
          </p>
          {active ? (
            <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-zinc-800">
              <div
                className="h-full rounded-full bg-gradient-to-r from-violet-500 to-fuchsia-400 transition-[width] duration-500"
                style={{ width: `${Math.max(2, Math.min(100, job.progress))}%` }}
              />
            </div>
          ) : null}
        </div>

        <span
          className={`shrink-0 rounded-md border px-2 py-1 text-[10px] font-semibold tracking-wide ${
            STATUS_STYLES[job.status] ?? "border-zinc-800 text-zinc-400"
          }`}
        >
          {statusLabel(job)}
        </span>

        {active ? (
          <span className="w-9 shrink-0 text-right text-[11px] font-mono text-zinc-500">
            {job.progress}%
          </span>
        ) : null}
      </Link>
    </li>
  );
}

export function YourJobs() {
  const { jobs, activeCount, error } = useJobs();

  if (jobs.length === 0) {
    // A failed refresh with nothing to show is worth saying out loud; a plain
    // empty list is not (there is simply nothing to display yet).
    if (!error) return null;
    return (
      <section className="mt-10 w-full max-w-2xl">
        <p className="text-xs text-amber-300/80">
          Could not load your jobs. Check that Stemify is still running.
        </p>
      </section>
    );
  }

  return (
    <section className="mt-12 w-full max-w-2xl">
      <div className="mb-3 flex items-baseline justify-between">
        <h2 className="text-[10px] font-bold uppercase tracking-widest text-zinc-500">
          Your jobs
        </h2>
        {activeCount > 0 ? (
          <span className="text-[11px] text-zinc-500">
            {activeCount} in progress
          </span>
        ) : null}
      </div>
      <ul className="flex flex-col gap-2">
        {jobs.map((job) => (
          <JobRow job={job} key={job.jobId} />
        ))}
      </ul>
    </section>
  );
}
