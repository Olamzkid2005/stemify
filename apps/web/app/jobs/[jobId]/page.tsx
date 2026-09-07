"use client";

/**
 * Job page (plan §8.3–§8.5): progress stages, refresh-safe recovery via the
 * URL, failure states with retry, and the completed stem list. Stage text
 * changes are announced via aria-live; layout reserves space to avoid shift.
 */
import Link from "next/link";
import { useParams } from "next/navigation";

import { useJobPolling } from "@/hooks/use-job-polling";

const STAGE_ORDER = [
  "Preparing audio",
  "Analyzing track",
  "Separating stems",
  "Encoding files",
  "Preparing downloads",
];

export default function JobPage() {
  const params = useParams<{ jobId: string }>();
  const { job, error, loading } = useJobPolling(params.jobId);

  if (loading) {
    return (
      <main className="flex flex-grow flex-col items-center justify-center px-4 text-center">
        <p className="text-sm text-zinc-400">Loading job…</p>
      </main>
    );
  }

  if (error === "not_found") {
    return (
      <main className="flex flex-grow flex-col items-center justify-center gap-4 px-4 text-center">
        <h1 className="text-2xl font-bold text-white">Job not found</h1>
        <p className="max-w-md text-sm text-zinc-400">
          This job does not exist, belongs to another browser, or has expired.
        </p>
        <Link
          href="/"
          className="purple-gradient-btn rounded-full px-6 py-2.5 text-sm font-semibold text-white"
        >
          Separate another track
        </Link>
      </main>
    );
  }

  if (!job) {
    return (
      <main className="flex flex-grow flex-col items-center justify-center gap-4 px-4 text-center">
        <h1 className="text-2xl font-bold text-white">Connection lost</h1>
        <p className="max-w-md text-sm text-zinc-400">
          We could not reach the service. Refresh the page to try again.
        </p>
      </main>
    );
  }

  if (job.status === "failed") {
    return (
      <main className="flex flex-grow flex-col items-center justify-center gap-4 px-4 text-center">
        <h1 className="text-2xl font-bold text-white">Processing failed</h1>
        <p className="max-w-md text-sm text-zinc-400">
          {job.errorMessage ?? "Something went wrong while separating this track."}
        </p>
        <p className="text-xs text-zinc-600">Reference: {job.errorCode}</p>
        <Link
          href="/"
          className="purple-gradient-btn rounded-full px-6 py-2.5 text-sm font-semibold text-white"
        >
          Try another track
        </Link>
      </main>
    );
  }

  if (job.status === "canceled" || job.status === "expired") {
    return (
      <main className="flex flex-grow flex-col items-center justify-center gap-4 px-4 text-center">
        <h1 className="text-2xl font-bold text-white">
          {job.status === "canceled" ? "Processing canceled" : "Files expired"}
        </h1>
        <p className="max-w-md text-sm text-zinc-400">
          {job.status === "canceled"
            ? "This job was stopped before completion."
            : "Generated files are deleted automatically after the retention window."}
        </p>
        <Link
          href="/"
          className="purple-gradient-btn rounded-full px-6 py-2.5 text-sm font-semibold text-white"
        >
          Separate another track
        </Link>
      </main>
    );
  }

  if (job.status === "completed") {
    return (
      <main className="flex w-full max-w-2xl flex-grow flex-col items-center gap-6 px-4 py-10 text-center">
        <h1 className="text-3xl font-extrabold text-white">Your stems are ready</h1>
        <p className="text-sm text-zinc-400">
          {job.source.filename} · {job.mode === "full_stems" ? "Full split" : "Vocals & instrumental"}
        </p>
        <a
          href={`${job.downloadUrl ?? `/api/jobs/${job.jobId}/downloads`}?kind=zip`}
          className="purple-gradient-btn rounded-full px-6 py-2.5 text-sm font-semibold text-white"
          download
        >
          Download all (ZIP)
        </a>
        <ul className="w-full space-y-3 text-left">
          {(job.stems ?? []).map((stem) => (
            <li
              key={stem.id}
              className="rounded-xl border border-zinc-800 bg-[#131317] px-4 py-3"
            >
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-sm font-semibold text-zinc-100">{stem.label}</p>
                  <p className="text-xs text-zinc-500">
                    {stem.durationSeconds !== null
                      ? `${Math.floor(stem.durationSeconds / 60)}:${String(Math.round(stem.durationSeconds % 60)).padStart(2, "0")}`
                      : ""}
                  </p>
                </div>
                <a
                  href={`/api/jobs/${job.jobId}/downloads?kind=stem&stem=${stem.id}`}
                  className="shrink-0 rounded-full border border-zinc-700 px-4 py-1.5 text-xs font-semibold text-zinc-200 hover:border-zinc-500 hover:text-white"
                  download
                >
                  Download
                </a>
              </div>
              <audio
                controls
                preload="none"
                src={`/api/jobs/${job.jobId}/downloads?kind=stem&stem=${stem.id}`}
                className="mt-3 h-10 w-full"
                aria-label={`Preview ${stem.label}`}
              />
            </li>
          ))}
        </ul>
        <p className="text-xs text-zinc-600">
          Files expire {job.expiresAt ? new Date(job.expiresAt).toLocaleString() : "soon"}.
        </p>
      </main>
    );
  }

  // Active: queued or processing.
  const activeIndex = Math.max(0, STAGE_ORDER.indexOf(job.userStage));
  return (
    <main className="flex w-full max-w-2xl flex-grow flex-col items-center gap-8 px-4 py-10 text-center">
      <div className="space-y-2">
        <h1 className="text-3xl font-extrabold text-white">Separating your track</h1>
        <p className="text-sm text-zinc-400">{job.source.filename ?? "Audio file"}</p>
      </div>

      <div
        className="purple-gradient-btn flex h-24 w-24 items-center justify-center rounded-full text-xl font-bold text-white"
        role="status"
      >
        {job.progress}%
      </div>

      <p aria-live="polite" className="text-sm font-semibold text-zinc-200">
        {job.userStage}
      </p>

      <ol className="w-full space-y-2 text-left" aria-label="Processing stages">
        {STAGE_ORDER.map((stage, i) => (
          <li
            key={stage}
            className={`flex items-center gap-3 text-sm ${
              i < activeIndex
                ? "text-zinc-500"
                : i === activeIndex
                  ? "font-semibold text-white"
                  : "text-zinc-600"
            }`}
          >
            <span
              className={`inline-block h-2 w-2 rounded-full ${
                i < activeIndex
                  ? "bg-zinc-600"
                  : i === activeIndex
                    ? "purple-gradient-btn"
                    : "bg-zinc-800"
              }`}
            />
            {stage}
          </li>
        ))}
      </ol>

      <p className="text-xs text-zinc-600">
        You can leave this page — the job keeps running. Bookmark this link to come back.
      </p>
    </main>
  );
}
