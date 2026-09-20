"use client";

import { useCallback, useState } from "react";
import { useRouter } from "next/navigation";

import { UploadDropzone } from "@/components/upload-dropzone";
import { OUTPUT_FORMATS, QUALITY_PRESETS, SEPARATION_MODES } from "@/lib/limits";

type SeparationMode = (typeof SEPARATION_MODES)[number];
type OutputFormat = (typeof OUTPUT_FORMATS)[number];
type QualityPreset = (typeof QUALITY_PRESETS)[number];

/** UI labels + the honest cost of each preset (docs/BENCHMARKS.md). */
const QUALITY_OPTIONS: { value: QualityPreset; label: string; hint: string }[] = [
  { value: "fast", label: "Fast", hint: "quickest, rougher edges" },
  { value: "balanced", label: "Balanced", hint: "recommended" },
];

type SourceTab = "upload" | "youtube" | "spotify";
/** The tabs that take a pasted link instead of a file. */
type LinkTab = Exclude<SourceTab, "upload">;

/**
 * Link-input tabs. `pattern` mirrors the server allowlist (lib/jobs.ts and the
 * contracts schema) for fast client feedback only — the server is
 * authoritative. Both link sources are structurally identical, so one config
 * drives one form; per-source copies are what drift apart.
 */
const LINK_TABS: Record<
  LinkTab,
  {
    icon: string;
    tabLabel: string;
    fieldLabel: string;
    placeholder: string;
    pattern: RegExp;
    unsupported: string;
    acknowledgement: string;
  }
> = {
  youtube: {
    icon: "▶",
    tabLabel: "YouTube Link",
    fieldLabel: "YouTube link",
    placeholder: "https://www.youtube.com/watch?v=…",
    pattern: /^https:\/\/(www\.)?(youtube\.com|youtu\.be)\/\S{1,2000}$/,
    unsupported: "That link is not supported. Use a standard youtube.com or youtu.be link.",
    acknowledgement:
      "I confirm I have the right to use this audio and that my use complies with YouTube's Terms of Service. Stemify processes the audio locally for personal use only.",
  },
  spotify: {
    icon: "♫",
    tabLabel: "Spotify Link",
    fieldLabel: "Spotify track link",
    placeholder: "https://open.spotify.com/track/…",
    pattern:
      /^https:\/\/open\.spotify\.com\/(intl-[a-z]{2}(-[A-Za-z]{2})?\/)?track\/[A-Za-z0-9]{22}(\?[^\s]*)?$/,
    unsupported:
      "That link is not supported. Use a single track link — open.spotify.com/track/… (albums and playlists are not supported yet).",
    acknowledgement:
      "I confirm I have the right to use this audio and that my use complies with Spotify's Terms of Service. It is fetched with my own Premium account and processed locally for personal use only.",
  },
};

function sourceErrorMessage(code: string, tab: SourceTab): string {
  if (code === "unsupported_source") {
    if (tab === "spotify") return LINK_TABS.spotify.unsupported;
    if (tab === "youtube") return LINK_TABS.youtube.unsupported;
    return "That source is not supported.";
  }
  if (code === "too_many_active_jobs") {
    return "You already have a job running. Wait for it to finish first.";
  }
  if (code === "upload_expired" || code === "object_missing") {
    return "That upload is no longer available. Upload the file again.";
  }
  return "The separation job could not be started. Try again.";
}

/**
 * Container: the only piece that touches the Next router, which needs an
 * app-router context to exist (`useRouter` throws without one). Keeping it to
 * this wrapper means `SourcePickerForm` can be rendered directly in a test
 * with a stub `navigate` and no router at all.
 */
export function SourcePicker() {
  const router = useRouter();
  const navigate = useCallback((href: string) => router.push(href), [router]);
  return <SourcePickerForm navigate={navigate} />;
}

export function SourcePickerForm({ navigate }: { navigate: (href: string) => void }) {
  const [tab, setTab] = useState<SourceTab>("upload");
  const [separationMode, setSeparationMode] = useState<SeparationMode>("vocals_instrumental");
  const [outputFormat, setOutputFormat] = useState<OutputFormat>("mp3");
  const [quality, setQuality] = useState<QualityPreset>("balanced");
  const [error, setError] = useState<string | null>(null);

  // Link tab state (secondary features — upload stays the default tab).
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
            quality,
            idempotencyKey: crypto.randomUUID(),
          }),
        });
        const body = (await response.json()) as { jobId?: string; error?: string };
        if (!response.ok || !body.jobId) {
          setError(sourceErrorMessage(body.error ?? "job_creation_failed", tab));
          return;
        }
        navigate(`/jobs/${body.jobId}`);
      } catch {
        setError("The separation job could not be started. Try again.");
      } finally {
        setSubmitting(false);
      }
    },
    [navigate, outputFormat, quality, separationMode, tab],
  );

  const selectTab = useCallback((next: SourceTab) => {
    setTab(next);
    setError(null);
    // The acknowledgement is source-specific and the links are not
    // interchangeable, so neither carries across tabs.
    setAcknowledged(false);
    setUrl("");
  }, []);

  const handleUploaded = useCallback(
    ({ uploadId, filename }: { uploadId: string; filename: string }) => {
      void startJob({ type: "upload", uploadId, filename });
    },
    [startJob],
  );

  const handleLinkSubmit = useCallback(() => {
    if (tab === "upload") return;
    const config = LINK_TABS[tab];
    const trimmed = url.trim();
    if (!config.pattern.test(trimmed)) {
      setError(config.unsupported);
      return;
    }
    if (!acknowledged) return;
    void startJob({ type: tab, url: trimmed });
  }, [acknowledged, startJob, tab, url]);

  return (
    <div className="flex w-full flex-col items-center">
      <div className="mb-6 flex items-center rounded-full border border-zinc-800/80 bg-[#151518] p-1">
        {([
          { value: "upload", icon: "↑", label: "Upload File" },
          { value: "youtube", icon: LINK_TABS.youtube.icon, label: LINK_TABS.youtube.tabLabel },
          { value: "spotify", icon: LINK_TABS.spotify.icon, label: LINK_TABS.spotify.tabLabel },
        ] as const).map((entry) => (
          <button
            key={entry.value}
            type="button"
            onClick={() => selectTab(entry.value)}
            aria-pressed={tab === entry.value}
            className={`flex items-center gap-2 rounded-full px-5 py-2 text-xs font-semibold shadow-sm transition ${
              tab === entry.value
                ? "bg-[#202025] text-white"
                : "text-zinc-400 hover:text-zinc-200"
            }`}
          >
            <span aria-hidden="true">{entry.icon}</span>
            <span>{entry.label}</span>
          </button>
        ))}
      </div>

      {tab === "upload" ? (
        <UploadDropzone onUploaded={handleUploaded} />
      ) : (
        <form
          className="flex w-full max-w-xl flex-col items-stretch"
          onSubmit={(event) => {
            event.preventDefault();
            handleLinkSubmit();
          }}
        >
          <label htmlFor="link-url" className="mb-2 text-xs text-zinc-400">
            {LINK_TABS[tab].fieldLabel}
          </label>
          <input
            id="link-url"
            // `text`, not `url`: the Spotify policy also accepts a
            // `spotify:track:` URI, which native URL validation would block
            // before our own message could explain it.
            type="text"
            inputMode="url"
            autoComplete="off"
            spellCheck={false}
            placeholder={LINK_TABS[tab].placeholder}
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            className="w-full rounded-xl border border-zinc-800 bg-[#131317] px-4 py-3 text-sm text-zinc-200 placeholder:text-zinc-600 focus:border-zinc-600 focus:outline-none focus:ring-2 focus:ring-purple-400"
          />

          {/* Policy acknowledgement (plan Task 14 / Spotify plan Section 7):
              required before submission, and never carried across tabs. */}
          <label className="mt-4 flex items-start gap-3 text-xs leading-relaxed text-zinc-400">
            <input
              type="checkbox"
              checked={acknowledged}
              onChange={(event) => setAcknowledged(event.target.checked)}
              className="mt-0.5 size-4 shrink-0 accent-purple-500"
            />
            <span>{LINK_TABS[tab].acknowledgement}</span>
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
          <OptionButton
            active={separationMode === "vocals_instrumental"}
            onClick={() => setSeparationMode("vocals_instrumental")}
          >
            Vocals &amp; Instrumental <span className="font-normal text-zinc-600">(2-stem)</span>
          </OptionButton>
          <OptionButton
            active={separationMode === "full_stems"}
            onClick={() => setSeparationMode("full_stems")}
          >
            Drums, Bass &amp; Instrumental <span className="font-normal text-zinc-600">(3-stem)</span>
          </OptionButton>
        </div>
      </div>

      <label className="mt-5 flex items-center gap-3 text-xs text-zinc-400">
        Output format
        <select value={outputFormat} onChange={(event) => setOutputFormat(event.target.value as OutputFormat)} className="rounded-lg border border-zinc-800 bg-[#131317] px-3 py-2 text-zinc-200 focus:outline-none focus:ring-2 focus:ring-purple-400">
          {OUTPUT_FORMATS.map((format) => <option key={format} value={format}>{format.toUpperCase()}</option>)}
        </select>
      </label>

      <div className="mt-4 flex flex-col items-center">
        <span className="mb-3 text-[10px] font-bold uppercase tracking-widest text-zinc-500">Quality</span>
        <div className="flex items-center gap-1 rounded-xl border border-zinc-800/90 bg-[#121215] p-1">
          {QUALITY_OPTIONS.map((option) => (
            <OptionButton
              key={option.value}
              active={quality === option.value}
              onClick={() => setQuality(option.value)}
            >
              {option.label} <span className="font-normal text-zinc-600">({option.hint})</span>
            </OptionButton>
          ))}
        </div>
      </div>

      <p aria-live="polite" className="mt-3 min-h-5 text-xs text-red-400">{error}</p>
    </div>
  );
}

function OptionButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return <button type="button" onClick={onClick} aria-pressed={active} className={`rounded-lg px-3.5 py-1.5 text-xs transition ${active ? "border border-zinc-700/60 bg-[#1f1f26] font-semibold text-zinc-100" : "font-medium text-zinc-400 hover:text-zinc-300"}`}>{children}</button>;
}
