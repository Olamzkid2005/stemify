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
 * What the completed-job page should offer as its ZIP download.
 *
 * "Every stem selected" and "no stem selected" are both states the picker can
 * reach, and only one of them is downloadable: `?stems=` with an empty value is
 * a client error at the route, so an empty selection must not produce a link at
 * all. Returning the decision (rather than a URL that may be rejected) is what
 * lets a test pin it without a browser.
 */
export type ZipSelection =
  /** Every available stem is included: the worker's own archive is exactly this. */
  | { kind: "all" }
  /** A subset: the route rebuilds the archive from exactly these stems. */
  | { kind: "partial"; url: string }
  /** Nothing ticked — there is no archive to build. */
  | { kind: "none" };

export function zipSelection(
  jobId: string,
  availableStems: string[],
  selectedStems: string[],
): ZipSelection {
  if (selectedStems.length === 0) return { kind: "none" };
  if (selectedStems.length >= availableStems.length) return { kind: "all" };
  return {
    kind: "partial",
    url: `/api/jobs/${jobId}/downloads?kind=zip&stems=${selectedStems.join(",")}`,
  };
}

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
