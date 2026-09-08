"use client";

import { useCallback, useState } from "react";
import { useRouter } from "next/navigation";

import { UploadDropzone } from "@/components/upload-dropzone";
import { OUTPUT_FORMATS, SEPARATION_MODES } from "@/lib/limits";

type SeparationMode = (typeof SEPARATION_MODES)[number];
type OutputFormat = (typeof OUTPUT_FORMATS)[number];

type SourceTab = "upload" | "youtube";

/** Same host allowlist the server enforces (lib/jobs.ts, contracts schema). */
const YOUTUBE_URL_PATTERN = /^https:\/\/(www\.)?(youtube\.com|youtu\.be)\/\S{1,2000}$/;

function youTubeError(code: string): string {
  if (code === "unsupported_source") {
    return "That link is not supported. Use a standard youtube.com or youtu.be link.";
  }
  if (code === "too_many_active_jobs") {
    return "You already have a job running. Wait for it to finish first.";
  }
  if (code === "upload_expired" || code === "object_missing") {
    return "That upload is no longer available. Upload the file again.";
  }
  return "The separation job could not be started. Try again.";
}

export function SourcePicker() {
  const router = useRouter();
  const [tab, setTab] = useState<SourceTab>("upload");
  // full_stems is not offered yet (see the Separation Mode section below);
  // this constant keeps the request payload explicit and mode-typed.
  const separationMode: SeparationMode = "vocals_instrumental";
  const [outputFormat, setOutputFormat] = useState<OutputFormat>("mp3");
  const [error, setError] = useState<string | null>(null);

  // YouTube tab state (secondary feature — upload stays the default tab).
  const [url, setUrl] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const startJob = useCallback(
    async (source: Record<string, unknown>) => {
      setError(null);
      setSubmitting(true);
      try {
        const response = await fetch("/api/jobs", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            source,
            mode: separationMode,
            outputFormat,
            idempotencyKey: crypto.randomUUID(),
          }),
        });
        const body = (await response.json()) as { jobId?: string; error?: string };
        if (!response.ok || !body.jobId) {
          setError(youTubeError(body.error ?? "job_creation_failed"));
          return;
        }
        router.push(`/jobs/${body.jobId}`);
      } catch {
        setError("The separation job could not be started. Try again.");
      } finally {
        setSubmitting(false);
      }
    },
    [outputFormat, router, separationMode],
  );

  const handleUploaded = useCallback(
    ({ uploadId, filename }: { uploadId: string; filename: string }) => {
      void startJob({ type: "upload", uploadId, filename });
    },
    [startJob],
  );

  const handleYouTubeSubmit = useCallback(() => {
    const trimmed = url.trim();
    if (!YOUTUBE_URL_PATTERN.test(trimmed)) {
      setError("That link is not supported. Use a standard youtube.com or youtu.be link.");
      return;
    }
    if (!acknowledged) return;
    void startJob({ type: "youtube", url: trimmed });
  }, [acknowledged, startJob, url]);

  return (
    <div className="flex w-full flex-col items-center">
      <div className="mb-6 flex items-center rounded-full border border-zinc-800/80 bg-[#151518] p-1">
        <button
          type="button"
          onClick={() => {
            setTab("upload");
            setError(null);
          }}
          aria-pressed={tab === "upload"}
          className={`flex items-center gap-2 rounded-full px-5 py-2 text-xs font-semibold shadow-sm transition ${
            tab === "upload"
              ? "bg-[#202025] text-white"
              : "text-zinc-400 hover:text-zinc-200"
          }`}
        >
          <span aria-hidden="true">↑</span><span>Upload File</span>
        </button>
        <button
          type="button"
          onClick={() => {
            setTab("youtube");
            setError(null);
          }}
          aria-pressed={tab === "youtube"}
          className={`flex items-center gap-2 rounded-full px-5 py-2 text-xs font-semibold shadow-sm transition ${
            tab === "youtube"
              ? "bg-[#202025] text-white"
              : "text-zinc-400 hover:text-zinc-200"
          }`}
        >
          <span aria-hidden="true">▶</span><span>YouTube Link</span>
        </button>
      </div>

      {tab === "upload" ? (
        <UploadDropzone onUploaded={handleUploaded} />
      ) : (
        <form
          className="flex w-full max-w-xl flex-col items-stretch"
          onSubmit={(event) => {
            event.preventDefault();
            handleYouTubeSubmit();
          }}
        >
          <label htmlFor="youtube-url" className="mb-2 text-xs text-zinc-400">
            YouTube link
          </label>
          <input
            id="youtube-url"
            type="url"
            inputMode="url"
            autoComplete="off"
            spellCheck={false}
            placeholder="https://www.youtube.com/watch?v=…"
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            className="w-full rounded-xl border border-zinc-800 bg-[#131317] px-4 py-3 text-sm text-zinc-200 placeholder:text-zinc-600 focus:border-zinc-600 focus:outline-none focus:ring-2 focus:ring-purple-400"
          />

          {/* Policy acknowledgement (plan Task 14): required before submission. */}
          <label className="mt-4 flex items-start gap-3 text-xs leading-relaxed text-zinc-400">
            <input
              type="checkbox"
              checked={acknowledged}
              onChange={(event) => setAcknowledged(event.target.checked)}
              className="mt-0.5 size-4 shrink-0 accent-purple-500"
            />
            <span>
              I confirm I have the right to use this audio and that my use complies
              with YouTube&apos;s Terms of Service. Stemify processes the audio
              locally for personal use only.
            </span>
          </label>

          <button
            type="submit"
            disabled={submitting || !acknowledged || url.trim().length === 0}
            className="mt-5 self-center rounded-full bg-gradient-to-tr from-violet-600 to-fuchsia-500 px-8 py-2.5 text-sm font-semibold text-white shadow-lg shadow-purple-900/30 transition enabled:hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {submitting ? "Starting…" : "Start separation"}
          </button>
        </form>
      )}

      <div className="mt-8 flex flex-col items-center">
        <span className="mb-3 text-[10px] font-bold uppercase tracking-widest text-zinc-500">Separation Mode</span>
        <div className="flex items-center gap-1 rounded-xl border border-zinc-800/90 bg-[#121215] p-1">
          <OptionButton active onClick={() => {}}>
            Vocals &amp; Instrumental <span className="font-normal text-zinc-600">(2-stem)</span>
          </OptionButton>
          {/* full_stems stays hidden until the model profile enables it
              (plan Sections 4.3/18: gated on the listening-quality pass). */}
        </div>
        <span className="mt-2 text-[11px] text-zinc-600">Full 4-stem split is coming soon.</span>
      </div>

      <label className="mt-5 flex items-center gap-3 text-xs text-zinc-400">
        Output format
        <select value={outputFormat} onChange={(event) => setOutputFormat(event.target.value as OutputFormat)} className="rounded-lg border border-zinc-800 bg-[#131317] px-3 py-2 text-zinc-200 focus:outline-none focus:ring-2 focus:ring-purple-400">
          {OUTPUT_FORMATS.map((format) => <option key={format} value={format}>{format.toUpperCase()}</option>)}
        </select>
      </label>

      <p aria-live="polite" className="mt-3 min-h-5 text-xs text-red-400">{error}</p>
    </div>
  );
}

function OptionButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return <button type="button" onClick={onClick} aria-pressed={active} className={`rounded-lg px-3.5 py-1.5 text-xs transition ${active ? "border border-zinc-700/60 bg-[#1f1f26] font-semibold text-zinc-100" : "font-medium text-zinc-400 hover:text-zinc-300"}`}>{children}</button>;
}
