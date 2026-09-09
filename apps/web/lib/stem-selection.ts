/**
 * Post-processing stem selection (roadmap A3): the user ticks which stems to
 * include and downloads a custom ZIP. Selection is pure allowlist filtering —
 * separation always computes every stem; selection only changes packaging.
 */
import { STEM_KEYS } from "@/lib/limits";

export type StemSelection =
  | { ok: true; stems: string[] }
  | { ok: false; reason: "missing" | "unknown" };

/**
 * Parse a `stems=` query value ("vocals,drums") into a deduplicated selection
 * in STEM_KEYS order. Unknown keys reject the request (no silent dropping —
 * a typo'd key must not produce a quietly wrong archive).
 */
export function parseStemSelection(raw: string | null): StemSelection {
  if (raw === null || raw.trim() === "") return { ok: false, reason: "missing" };
  const requested = new Set(
    raw
      .split(",")
      .map((key) => key.trim().toLowerCase())
      .filter((key) => key.length > 0),
  );
  if (requested.size === 0) return { ok: false, reason: "missing" };
  const stems = (STEM_KEYS as readonly string[]).filter((key) => requested.has(key));
  if (stems.length !== requested.size) return { ok: false, reason: "unknown" };
  return { ok: true, stems };
}
