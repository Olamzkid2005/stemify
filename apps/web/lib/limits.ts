/**
 * Client-safe product limits (plan Section 9). Server-side validation in the
 * API routes is authoritative; these exist for fast client feedback only.
 */
export const SEPARATION_MODES = ["vocals_instrumental", "full_stems"] as const;
export const OUTPUT_FORMATS = ["mp3", "wav", "flac", "ogg", "m4a"] as const;

/** Stem keys shared with the contracts `stemKey` enum (archive is reserved for the ZIP). */
export const STEM_KEYS = ["vocals", "instrumental", "drums", "bass", "other"] as const;

export const CLIENT_LIMITS = {
  maxUploadBytes: 100 * 1024 * 1024, // 100 MB
  maxDurationSeconds: 480, // 8 minutes
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

export function validateFileSelection(file: File): FileValidationResult {
  const name = file.name.toLowerCase();
  const hasAcceptedExt = CLIENT_LIMITS.acceptedExtensions.some((ext) =>
    name.endsWith(ext),
  );
  const mimeOk =
    file.type === "" || // some OSes report empty type; extension decides
    (CLIENT_LIMITS.acceptedMimeHints as readonly string[]).includes(file.type);
  if (!hasAcceptedExt || !mimeOk) return { ok: false, reason: "invalid-type" };
  if (file.size > CLIENT_LIMITS.maxUploadBytes)
    return { ok: false, reason: "too-large" };
  return { ok: true };
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
