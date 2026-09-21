# Stem Selection Grid (custom stems)

Status: approved (product decisions locked with the operator 2026-09-21)

Replace the two fixed separation modes with one grid where the operator ticks
any combination of **Vocals · Drums · Bass · Instrumental**, and the job
produces exactly what was ticked.

## Decisions (locked)

1. **The grid replaces the mode toggle.** One mental model; the two old modes
   remain valid server-side (existing jobs, manifests, contracts) but are no
   longer offered in the picker.
2. **The job produces exactly what was ticked.** No complement bounce is
   added behind the operator's back. Ticking Instrumental *is* the
   rest-of-mix request.
3. Piano/guitar tiles from the original mockup are **not** offered: the
   experimental `htdemucs_6s` variant was dropped earlier after listening
   review (see `worker/worker/models/profiles.py`) — the sources were not
   accurate enough to ship. The grid is four tiles.

## The unifying rule

Both existing modes are special cases of one rule, which is why this needs no
second inference pass:

> outputs = ticked model stems +, if Instrumental is ticked,
> `waveform − (sum of every ticked model stem)`

- `{vocals, instrumental}` → today's `vocals_instrumental` (mix − vocals)
- `{drums, bass, instrumental}` → today's `full_stems`
  (instrumental = mix − vocals − drums − bass)
- anything else → **`custom`** (new mode value)

The model always computes all four htdemucs sources in one pass; selection
decides what is saved and named. Two consequences stated to the operator at
review: job duration does not change with fewer ticks, and Instrumental is
always mixture-consistent (residual), so ticked stems + Instrumental sum to
the original mix exactly.

## Contract changes (packages/contracts)

- `common.schema.json`: `separationMode` enum gains `"custom"`.
- New `$defs.stemSelection`: array of 1–4 unique stemKey values from
  `{vocals, drums, bass, instrumental}`; `instrumental` is allowed only
  alongside at least one model stem (a residual of nothing is meaningless).
- `job-request.schema.json`: `mode: custom` requires `stemSelection`
  (if/then); any other mode forbids it.
- `output-manifest.schema.json` needs no change: it already allows any
  1–6 stem list.

## Worker changes

- `worker/worker/database.py`: the jobs mode CHECK gains `'custom'` via the
  existing rebuild path (which already preserves `quality` and source_type
  through a rebuild); additive `stem_selection` TEXT column (JSON, nullable —
  null for the legacy modes and drum_breakdown).
- `worker/worker/models/profiles.py`: `custom` joins the valid mode set. The
  htdemucs profile does NOT list `custom` in `supported_modes` (the allowlist
  equality is the security gate and must not churn); instead
  `get_profile_for_mode` maps `custom` to the default profile explicitly, and
  `separate()` accepts `stem_selection` alongside `custom`.
- `worker/worker/models/demucs.py`: `custom` computes
  `selected model stems` + residual from the raw model sources and the
  waveform; mixture-consistency validation identical to the other modes.
- `worker/worker/pipeline.py`, `worker/worker/job_loop.py`: carry
  `stem_selection` through claim → separation → packaging unchanged.
- `worker/worker/packaging.py`: manifest validation accepts `custom`.
- `worker/worker/input_audio.py`: duration cap for `custom` follows
  `full_stems` (the 3-stem cap) — same model work, same cap.
- Web mirrors it: `apps/web/lib/limits.ts` (`maxDurationForMode`) and the
  picker hint stay identical for both sides (the invariant the tests pin).

## Web changes (apps/web)

- `lib/db/schema.ts` + additive migration: `jobs.stem_selection` TEXT.
- `lib/limits.ts`: `STEM_SELECTION_KEYS = ["vocals","drums","bass","instrumental"]`,
  `isStemSelection(value)` guard, `modeStemSummary` gains custom labels, and
  one `modeFromSelection(selection)` helper so UI, service and tests share the
  ticked→mode mapping (the old modes map from their exact sets).
- `lib/jobs.ts`: validate a submitted `stemSelection` exactly as the contract
  does; store it; 400 on invalid combinations.
- `components/source-picker.tsx`: the mode toggle becomes a 2×2 tile grid
  (checked: purple border + accent tick) with a live "You'll get N stems"
  hint; the job request sends `mode` + `stemSelection` per the mapping.
- `lib/job-view.ts` + job page labels: `custom` reads "N stems" and the
  completed view already renders whatever stem list the manifest carries, so
  no per-stem changes there.

## What deliberately does not change

- The model, inference quality presets, and per-job timing.
- `drum_breakdown` (refine-drums) — it is a different job type on a different
  checkpoint and keeps its own flow.
- Existing queued/processing jobs in a DB from before the deploy: their
  `vocals_instrumental`/`full_stems` rows keep working; the worker's claim
  path treats a missing `stem_selection` on a legacy mode as empty.

## Test map

| Layer | New tests |
|---|---|
| Contracts | custom in enum; stemSelection shape; require/forbid if/then |
| Worker DB | legacy DB → rebuild keeps rows, accepts custom + selection |
| Worker demucs | every combination maps to the right stems; residual is mixture-consistent; invalid selection refused |
| Worker packaging | manifest with mode custom + 2 stems validates |
| Web service | valid selection accepted and stored; junk rejected 400 |
| Web UI | grid toggles; hint counts; submit payload carries mode+selection |
| Web limits | maxDurationForMode(custom) equals the full-stems cap on both sides |
