"use client";

import { useCallback, useState } from "react";
import { useRouter } from "next/navigation";

import { UploadDropzone } from "@/components/upload-dropzone";
import { OUTPUT_FORMATS, SEPARATION_MODES } from "@/lib/limits";

type SeparationMode = (typeof SEPARATION_MODES)[number];
type OutputFormat = (typeof OUTPUT_FORMATS)[number];

export function SourcePicker() {
  const router = useRouter();
  const [separationMode, setSeparationMode] = useState<SeparationMode>("vocals_instrumental");
  const [outputFormat, setOutputFormat] = useState<OutputFormat>("mp3");
  const [error, setError] = useState<string | null>(null);

  const handleUploaded = useCallback(
    async ({ uploadId, filename }: { uploadId: string; filename: string }) => {
      setError(null);
      try {
        const response = await fetch("/api/jobs", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            source: { type: "upload", uploadId, filename },
            mode: separationMode,
            outputFormat,
            idempotencyKey: crypto.randomUUID(),
          }),
        });
        const body = (await response.json()) as { jobId?: string; error?: string };
        if (!response.ok || !body.jobId) throw new Error(body.error ?? "job_creation_failed");
        router.push(`/jobs/${body.jobId}`);
      } catch {
        setError("The file uploaded, but the separation job could not be started. Try again.");
      }
    },
    [outputFormat, router, separationMode],
  );

  return (
    <div className="flex w-full flex-col items-center">
      <div className="mb-6 flex items-center rounded-full border border-zinc-800/80 bg-[#151518] p-1">
        <button type="button" disabled aria-pressed="true" className="flex items-center gap-2 rounded-full bg-[#202025] px-5 py-2 text-xs font-semibold text-white shadow-sm">
          <span aria-hidden="true">↑</span><span>Upload File</span>
        </button>
        <span className="px-5 py-2 text-xs font-medium text-zinc-600" aria-label="YouTube import coming soon">YouTube Link · soon</span>
      </div>

      <UploadDropzone onUploaded={handleUploaded} />

      <div className="mt-8 flex flex-col items-center">
        <span className="mb-3 text-[10px] font-bold uppercase tracking-widest text-zinc-500">Separation Mode</span>
        <div className="flex items-center gap-1 rounded-xl border border-zinc-800/90 bg-[#121215] p-1">
          <OptionButton active={separationMode === "vocals_instrumental"} onClick={() => setSeparationMode("vocals_instrumental")}>
            Vocals &amp; Instrumental <span className="font-normal text-zinc-600">(2-stem)</span>
          </OptionButton>
          <OptionButton active={separationMode === "full_stems"} onClick={() => setSeparationMode("full_stems")}>
            Full Split <span className="font-normal text-zinc-400">(4-stem)</span>
          </OptionButton>
        </div>
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
