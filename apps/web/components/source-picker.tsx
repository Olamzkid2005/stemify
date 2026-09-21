"use client";

import { useCallback, useState } from "react";
import { useRouter } from "next/navigation";

import { useJobs } from "@/components/jobs-context";
import { UploadDropzone } from "@/components/upload-dropzone";
import {
  DEFAULT_LIMITS,
  OUTPUT_FORMATS,
  QUALITY_PRESETS,
  STEM_SELECTION_KEYS,
  maxDurationForMode,
  modeFromSelection,
  type EffectiveLimits,
  type StemSelectionKey,
} from "@/lib/limits";

type OutputFormat = (typeof OUTPUT_FORMATS)[number];
type QualityPreset = (typeof QUALITY_PRESETS)[number];

/** Grid labels for the tickable stems (stem-selection plan). */
const STEM_LABELS: Record<StemSelectionKey, string> = {
  vocals: "Vocals",
  drums: "Drums",
  bass: "Bass",
  instrumental: "Instrumental",
};

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
    /**
     * Shown instead of the form when this machine cannot serve the source at
     * all (see lib/capabilities.ts). Absent = always available.
     */
    unavailable?: string;
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
    unavailable:
      "YouTube input is switched off on this machine (STEMIFY_YOUTUBE_ENABLED=0), so a link cannot be fetched. Remove that line from .env (the worker defaults it on) and restart Stemify.",
  },
  spotify: {
    icon: "♫",
    tabLabel: "Spotify Link",
    fieldLabel: "Spotify track link",
    placeholder: "https://open.spotify.com/track/…",
    // The same two shapes the server accepts (lib/jobs.ts, and the worker's
    // own allowlist): the https track link — optionally behind the /intl-xx/
    // locale prefix Spotify itself adds, with a share query string — and the
    // `spotify:track:` URI the desktop app copies. The server accepted the URI
    // while this pattern did not, so a pasted URI was rejected here first.
    pattern:
      /^https:\/\/open\.spotify\.com\/(intl-[a-z]{2}(-[A-Za-z]{2})?\/)?track\/[A-Za-z0-9]{22}(\?[^\s]*)?$|^spotify:track:[A-Za-z0-9]{22}$/,
    unsupported:
      "That link is not supported. Use a single track link — open.spotify.com/track/… (albums and playlists are not supported yet).",
    acknowledgement:
      "I confirm I have the right to use this audio and that my use complies with Spotify's Terms of Service. It is fetched with my own Premium account and processed locally for personal use only.",
    unavailable:
      "Spotify input is switched off on this machine, so a link cannot be fetched. Add STEMIFY_SPOTIFY_ENABLED=1 to .env and restart Stemify, then complete the one-time Premium login — see the Spotify section of worker/README.md.",
  },
};

/**
 * Shown whenever the submit button is disabled for a reason the user can fix.
 * A disabled control with only `opacity-40` behind it is indistinguishable from
 * a broken one — "I pasted the link and nothing happened" — so the reason is
 * always on screen next to the button that will not respond.
 */
const MISSING_LINK_HINT = "Paste a link above to continue.";
const MISSING_ACKNOWLEDGEMENT_HINT =
  "Tick the box above to confirm you have the right to use this audio.";

/**
 * A response the client could not read at all is not a rejected job: the local
 * server is stale (a rebuilt route answers 404) or was stopped mid-request.
 * Saying "try again" there hides the one action that actually helps.
 */
const SERVICE_UNREACHABLE_MESSAGE =
  "Could not reach the local service. Check that Stemify is still running, then refresh this page and try again.";

function sourceErrorMessage(code: string, tab: SourceTab): string {
  if (code === "unsupported_source") {
    if (tab === "spotify") return LINK_TABS.spotify.unsupported;
    if (tab === "youtube") return LINK_TABS.youtube.unsupported;
    return "That source is not supported.";
  }
  if (code === "youtube_unavailable") {
    return LINK_TABS.youtube.unavailable ?? "YouTube input is not available on this machine.";
  }
  if (code === "too_many_active_jobs") {
    // The cap allows a full worker pool plus one waiting job, so reaching it
    // means several are already going. Name the action that helps.
    return "You already have several jobs in progress. Wait for one to finish before starting another.";
  }
  if (code === "spotify_unavailable") {
    return LINK_TABS.spotify.unavailable ?? "Spotify input is not available on this machine.";
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
export function SourcePicker({
  spotifyAvailable,
  youtubeAvailable,
  limits = DEFAULT_LIMITS,
}: {
  spotifyAvailable: boolean;
  youtubeAvailable: boolean;
  /** Effective caps from the server (plan Section 9); shipped defaults otherwise. */
  limits?: EffectiveLimits;
}) {
  const router = useRouter();
  const navigate = useCallback((href: string) => router.push(href), [router]);
  // Live count of this browser's active jobs, from the page's single poller
  // (concurrency plan C4). Used only to warn — never to refuse a submission.
  const { activeCount } = useJobs();
  return (
    <SourcePickerForm
      activeJobs={activeCount}
      navigate={navigate}
      spotifyAvailable={spotifyAvailable}
      youtubeAvailable={youtubeAvailable}
      limits={limits}
    />
  );
}

export function SourcePickerForm({
  navigate,
  spotifyAvailable,
  youtubeAvailable,
  activeJobs = 0,
  limits = DEFAULT_LIMITS,
}: {
  navigate: (href: string) => void;
  /** Server-provided capability; false hides the form for a source this machine cannot fetch. */
  spotifyAvailable: boolean;
  /** Same for YouTube, which the worker defaults on (lib/capabilities.ts). */
  youtubeAvailable: boolean;
  /** This browser's queued+processing job count; 0 when unknown. */
  activeJobs?: number;
  /** Effective caps from the server; shipped defaults when rendered standalone. */
  limits?: EffectiveLimits;
}) {
  const [tab, setTab] = useState<SourceTab>("upload");
  // The stem grid replaces the old two-mode toggle (stem-selection plan): the
  // ticked set IS the request, and modeFromSelection decides which mode value
  // carries it. Default is the old default preset, vocals + instrumental.
  const [selectedStems, setSelectedStems] = useState<StemSelectionKey[]>(["vocals", "instrumental"]);
  const separationMode = modeFromSelection(selectedStems);
  const [outputFormat, setOutputFormat] = useState<OutputFormat>("mp3");
  const [quality, setQuality] = useState<QualityPreset>("balanced");
  const [error, setError] = useState<string | null>(null);

  const toggleStem = useCallback((key: StemSelectionKey) => {
    setSelectedStems((previous) =>
      previous.includes(key) ? previous.filter((item) => item !== key) : [...previous, key],
    );
  }, []);

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
            // Only custom carries the list; the fixed modes forbid it.
            ...(separationMode === "custom" ? { stemSelection: selectedStems } : {}),
            outputFormat,
            quality,
            idempotencyKey: crypto.randomUUID(),
          }),
        });
        // A JSON parse failure is its own case, not a generic rejection: an HTML
        // 404 body from a stale dev server must not read as "try again".
        const body = (await response.json().catch(() => null)) as
          | { jobId?: string; error?: string }
          | null;
        if (!body) {
          setError(SERVICE_UNREACHABLE_MESSAGE);
          return;
        }
        if (!response.ok || !body.jobId) {
          setError(sourceErrorMessage(body.error ?? "job_creation_failed", tab));
          return;
        }
        navigate(`/jobs/${body.jobId}`);
      } catch {
        setError(SERVICE_UNREACHABLE_MESSAGE);
      } finally {
        setSubmitting(false);
      }
    },
    [navigate, outputFormat, quality, selectedStems, separationMode, tab],
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

  const unavailable = useCallback(
    (source: SourceTab): string | null => {
      if (source === "youtube" && !youtubeAvailable) return LINK_TABS.youtube.unavailable ?? null;
      if (source === "spotify" && !spotifyAvailable) return LINK_TABS.spotify.unavailable ?? null;
      return null;
    },
    [spotifyAvailable, youtubeAvailable],
  );

  const handleLinkSubmit = useCallback(() => {
    if (tab === "upload") return;
    const config = LINK_TABS[tab];
    const blocked = unavailable(tab);
    if (blocked) {
      // The form is not rendered for an unavailable source, so this only guards
      // against a future path that submits without one.
      setError(blocked);
      return;
    }
    const trimmed = url.trim();
    if (!trimmed) {
      setError(MISSING_LINK_HINT);
      return;
    }
    if (!config.pattern.test(trimmed)) {
      setError(config.unsupported);
      return;
    }
    if (!acknowledged) {
      setError(MISSING_ACKNOWLEDGEMENT_HINT);
      return;
    }
    void startJob({ type: tab, url: trimmed });
  }, [acknowledged, startJob, tab, unavailable, url]);

  // Why the submit button will not respond, stated before it is clicked: the
  // disabled attribute alone is invisible reasoning.
  const linkHint =
    tab === "upload" || unavailable(tab)
      ? null
      : url.trim().length === 0
        ? MISSING_LINK_HINT
        : acknowledged
          ? null
          : MISSING_ACKNOWLEDGEMENT_HINT;

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
        // The duration cap depends on the mode, so the hint shown before an
        // upload is the cap the worker will enforce for the mode selected now.
        <UploadDropzone
          onUploaded={handleUploaded}
          maxUploadBytes={limits.maxUploadBytes}
          maxDurationSeconds={maxDurationForMode(separationMode, limits)}
        />
      ) : unavailable(tab) ? (
        /*
         * A form here could only produce a job the worker refuses, which is
         * how "paste a link" turned into "nothing happens" (a job that failed
         * milliseconds after being claimed). Say why instead.
         */
        <div
          role="status"
          className="w-full max-w-xl rounded-xl border border-amber-900/50 bg-amber-950/20 px-5 py-4 text-left"
        >
          <p className="text-xs font-semibold uppercase tracking-widest text-amber-400">
            {LINK_TABS[tab].tabLabel} unavailable
          </p>
          <p className="mt-2 text-xs leading-relaxed text-amber-200/80">{unavailable(tab)}</p>
        </div>
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

      {/* Warn but always accept (concurrency plan C4/3.6): several jobs can run
          at once, and more than one browser can submit, so submitting again is
          legitimate — it just shares the CPU. Saying so up front is the whole
          point; refusing the job or hiding the form would not be. */}
      {activeJobs > 0 && !error ? (
        <p
          className="mt-4 w-full max-w-xl text-left text-xs leading-relaxed text-amber-300/80"
          data-testid="pool-warning"
        >
          You already have {activeJobs} {activeJobs === 1 ? "job" : "jobs"} running.
          Starting another shares the same CPU, so all of them will take longer.
        </p>
      ) : null}

      {error ? (
        <p
          role="alert"
          className="mt-4 w-full max-w-xl rounded-xl border border-red-900/60 bg-red-950/30 px-4 py-3 text-left text-xs leading-relaxed text-red-200"
        >
          {error}
        </p>
      ) : linkHint ? (
        <p className="mt-4 w-full max-w-xl text-left text-xs leading-relaxed text-amber-300/80">
          {linkHint}
        </p>
      ) : null}

      <div className="mt-8 flex flex-col items-center">
        <span className="mb-3 text-[10px] font-bold uppercase tracking-widest text-zinc-500">Choose one or more stems</span>
        <div
          role="group"
          aria-label="Stems to extract"
          className="grid w-full max-w-xs grid-cols-2 gap-2"
        >
          {STEM_SELECTION_KEYS.map((key) => {
            const active = selectedStems.includes(key);
            return (
              <button
                key={key}
                type="button"
                aria-pressed={active}
                onClick={() => toggleStem(key)}
                className={`rounded-xl border px-3 py-2.5 text-sm font-semibold transition ${
                  active
                    ? "border-purple-500/70 bg-purple-950/30 text-white shadow-lg shadow-purple-900/20"
                    : "border-zinc-800 bg-[#131317] text-zinc-400 hover:border-zinc-700 hover:text-zinc-200"
                }`}
              >
                <span className="flex items-center justify-center gap-2">
                  {active ? (
                    <svg viewBox="0 0 20 20" className="size-3.5 text-purple-400" fill="currentColor" aria-hidden="true">
                      <path fillRule="evenodd" d="M16.7 5.3a1 1 0 0 1 0 1.4l-7.5 7.5a1 1 0 0 1-1.4 0l-3.5-3.5a1 1 0 1 1 1.4-1.4L8.5 12l6.8-6.7a1 1 0 0 1 1.4 0Z" clipRule="evenodd" />
                    </svg>
                  ) : null}
                  {STEM_LABELS[key]}
                </span>
              </button>
            );
          })}
        </div>
        <p className="mt-2 text-[11px] text-zinc-500" aria-live="polite" data-testid="stem-hint">
          {selectedStems.length === 0
            ? "Tick at least one stem"
            : `You'll get ${selectedStems.length} stem${selectedStems.length === 1 ? "" : "s"}${selectedStems.includes("instrumental") ? " · instrumental is the rest of the mix" : ""}`}
        </p>
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

    </div>
  );
}

function OptionButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return <button type="button" onClick={onClick} aria-pressed={active} className={`rounded-lg px-3.5 py-1.5 text-xs transition ${active ? "border border-zinc-700/60 bg-[#1f1f26] font-semibold text-zinc-100" : "font-medium text-zinc-400 hover:text-zinc-300"}`}>{children}</button>;
}
