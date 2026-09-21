/**
 * Client-safe product limits (plan Section 9). Server-side validation in the
 * API routes is authoritative; these exist for fast client feedback only.
 */
export const SEPARATION_MODES = ["vocals_instrumental", "full_stems", "custom"] as const;
export const OUTPUT_FORMATS = ["mp3", "wav", "flac", "ogg", "m4a"] as const;

/**
 * Tickable stems for the custom mode (docs/STEM_SELECTION_PLAN.md). Order is
 * the grid's display order and the worker's canonical stem order; mirrors the
 * contracts stemSelection def and the worker's STEM_SELECTION_KEYS.
 */
export const STEM_SELECTION_KEYS = ["vocals", "drums", "bass", "instrumental"] as const;
export type StemSelectionKey = (typeof STEM_SELECTION_KEYS)[number];

/**
 * True when the value is exactly a stem selection: 1-4 unique known keys.
 * This is the same shape the job-request contract enforces.
 */
export function isStemSelection(value: unknown): value is StemSelectionKey[] {
  return (
    Array.isArray(value) &&
    value.length >= 1 &&
    value.length <= STEM_SELECTION_KEYS.length &&
    value.every((item) => (STEM_SELECTION_MODES as readonly unknown[]).includes(item)) &&
    new Set(value).size === value.length
  );
}
const STEM_SELECTION_MODES = STEM_SELECTION_KEYS;

/**
 * The mode a selection produces: the fixed modes are literal special cases
 * (the old toggle's two options), anything else is custom. Shared by the
 * picker, the job service and the tests so all three agree on one mapping.
 */
export function modeFromSelection(selection: readonly string[]): "vocals_instrumental" | "full_stems" | "custom" {
  const set = [...selection].sort().join(",");
  if (set === "instrumental,vocals") return "vocals_instrumental";
  if (set === "bass,drums,instrumental") return "full_stems";
  return "custom";
}

/**
 * Per-job quality presets (STEMIFY_QUALITY values). The worker resolves them
 * to inference overlap/shifts; omitting quality uses the worker's default.
 */
export const QUALITY_PRESETS = ["fast", "balanced"] as const;
export type QualityPreset = (typeof QUALITY_PRESETS)[number];

/**
 * Stem keys shared with the contracts `stemKey` enum (archive is reserved for
 * the ZIP). Drum-part keys (roadmap Phase B) come from the refine-drums
 * action, never from the upload flow.
 */
export const STEM_KEYS = [
  "vocals",
  "instrumental",
  "drums",
  "bass",
  "other",
  "guitar",
  "piano",
  "drums_kick",
  "drums_snare",
  "drums_cymbals",
  "drums_toms",
] as const;

/** Stem keys a NEW upload job may reference (drum parts only come from refine). */
export const UPLOAD_STEM_KEYS = [
  "vocals",
  "instrumental",
  "drums",
  "bass",
  "other",
  "guitar",
  "piano",
] as const;

/**
 * Short "what you get" summary for a separation mode, shared by the job page
 * and the home-page job list so the two can never describe a mode differently.
 */
export function modeStemSummary(mode: string, stemSelection?: readonly string[] | null): string {
  if (mode === "custom") {
    return stemSelection && stemSelection.length > 0
      ? `${stemSelection.length} stem${stemSelection.length === 1 ? "" : "s"}`
      : "custom stems";
  }
  if (mode === "full_stems") return "3 stems";
  if (mode === "drum_breakdown") return "4 drum parts";
  return "2 stems";
}

/**
 * Product limits (plan Section 9).
 *
 * The worker is the final authority: it re-validates every source after
 * fetching it, which is the only place a link source's duration is knowable.
 * The values here exist so the routes and the job service refuse early with a
 * reason instead of queueing work that is certain to fail.
 *
 * `DEFAULT_LIMITS` are the shipped numbers. `serverEffectiveLimits()` reads the
 * same variables start.sh exports to both processes, so a number set in `.env`
 * is honored on both sides instead of only inside the worker.
 */
export const DEFAULT_LIMITS = {
  maxUploadBytes: 100 * 1024 * 1024, // 100 MB
  maxDurationSeconds: 480, // 8 minutes
  fullStemsMaxDurationSeconds: 360, // 6 minutes: the 3-stem split does ~1.5x the work
} as const;

export type EffectiveLimits = {
  maxUploadBytes: number;
  maxDurationSeconds: number;
  fullStemsMaxDurationSeconds: number;
};

/**
 * One positive number from the environment, or the shipped default.
 *
 * Blank, non-numeric, non-finite and non-positive values all fall back: the
 * only way to change a cap is to state a usable number, so a typo cannot switch
 * a limit off.
 */
function limitFromEnv(name: string, fallback: number): number {
  // Guarded because client components import this module too, even though only
  // server code calls into this function.
  const env = typeof process === "undefined" ? undefined : process.env;
  const raw = env?.[name];
  if (raw === undefined || raw.trim() === "") return fallback;
  const value = Number(raw);
  return Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
}

/** Effective caps for this server process (the routes and the job service). */
export function serverEffectiveLimits(): EffectiveLimits {
  return {
    maxUploadBytes: limitFromEnv("MAX_UPLOAD_BYTES", DEFAULT_LIMITS.maxUploadBytes),
    maxDurationSeconds: limitFromEnv("MAX_DURATION_SECONDS", DEFAULT_LIMITS.maxDurationSeconds),
    fullStemsMaxDurationSeconds: limitFromEnv(
      "MAX_FULL_STEMS_DURATION_SECONDS",
      DEFAULT_LIMITS.fullStemsMaxDurationSeconds,
    ),
  };
}

/**
 * Duration cap for one separation mode — mirrors
 * `worker.input_audio.max_duration_for_mode`, so the hint a user reads before
 * uploading is the cap the worker will enforce. Refine jobs
 * (`drum_breakdown`) work on a worker-produced stem and take the general cap.
 */
export function maxDurationForMode(
  mode: string,
  limits: EffectiveLimits = DEFAULT_LIMITS,
): number {
  // custom is the same single inference pass as the 3-stem mode, so it shares
  // its cap — mirrors worker.input_audio.max_duration_for_mode exactly.
  if (mode === "full_stems" || mode === "custom") return limits.fullStemsMaxDurationSeconds;
  return limits.maxDurationSeconds;
}

export const CLIENT_LIMITS = {
  maxUploadBytes: DEFAULT_LIMITS.maxUploadBytes,
  maxDurationSeconds: DEFAULT_LIMITS.maxDurationSeconds,
  acceptedExtensions: [".mp3", ".wav", ".flac", ".ogg", ".m4a"],
  acceptedMimeHints: [
    "audio/mpeg",
    "audio/wav",
    "audio/x-wav",
    "audio/flac",
    "audio/x-flac",
    "audio/ogg",
    "audio/mp4",
    "audio/x-m4a",
  ],
} as const;

export type FileValidationResult =
  | { ok: true }
  | { ok: false; reason: "invalid-type" | "too-large" };

export function validateFileSelection(
  file: File,
  maxUploadBytes: number = CLIENT_LIMITS.maxUploadBytes,
): FileValidationResult {
  const name = file.name.toLowerCase();
  const hasAcceptedExt = CLIENT_LIMITS.acceptedExtensions.some((ext) =>
    name.endsWith(ext),
  );
  const mimeOk =
    file.type === "" || // some OSes report empty type; extension decides
    (CLIENT_LIMITS.acceptedMimeHints as readonly string[]).includes(file.type);
  if (!hasAcceptedExt || !mimeOk) return { ok: false, reason: "invalid-type" };
  if (file.size > maxUploadBytes) return { ok: false, reason: "too-large" };
  return { ok: true };
}

/**
 * Message for a refused upload (plan Section 9).
 *
 * The route's error code decides, because the server's cap can differ from the
 * one the page rendered with — a bare "Upload failed" would hide the one fact
 * the user can act on. Codes are stable (`file_too_large`), not prose.
 */
export function uploadErrorMessage(status: number, body: unknown): string {
  const record = body !== null && typeof body === "object" ? (body as Record<string, unknown>) : {};
  const error = typeof record.error === "string" ? record.error : "";
  if (error === "file_too_large") {
    return "That file is over the size limit for this machine. Choose a smaller file.";
  }
  if (error === "unsupported_file_type") {
    return "That file type is not supported. Use MP3, WAV, FLAC, OGG, or M4A.";
  }
  if (error === "invalid_multipart" || error === "file_required") {
    return "The upload did not arrive in one piece. Try again.";
  }
  if (error === "upload_failed" || status >= 500) {
    return "The server could not store the upload. Try again.";
  }
  if (status === 401 || status === 403) {
    return "This browser session cannot upload. Reload the page and try again.";
  }
  if (status === 429) {
    return "Too many jobs are already running. Wait for one to finish and try again.";
  }
  return "Upload failed. Try again.";
}

/** Best-effort duration/codec probe via the browser's audio decoder. */
export function probeAudio(
  file: File,
): Promise<{ durationSeconds: number | null; decodable: boolean }> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const audio = new Audio();
    const timer = setTimeout(() => finish({ durationSeconds: null, decodable: true }), 4000);
    function finish(result: { durationSeconds: number | null; decodable: boolean }) {
      clearTimeout(timer);
      URL.revokeObjectURL(url);
      resolve(result);
    }
    audio.onloadedmetadata = () =>
      finish({
        durationSeconds: Number.isFinite(audio.duration) ? audio.duration : null,
        decodable: true,
      });
    audio.onerror = () => finish({ durationSeconds: null, decodable: false });
    audio.src = url;
  });
}
