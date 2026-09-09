# Stemify Roadmap: Deep Stems & Audio Analysis

Long-term improvements from the post-verification notes (2026-09-08 session).
This document is the plan of record; nothing here is implemented yet.

Decisions locked with the product owner on 2026-09-09:

- **D1 — Deeper separation is phased.** Phase A adds the official 6-stem
  model (piano + guitar). Drum subdivision (kick/snare/hats) is deferred to
  Phase B and only pursued if Phase A proves stable on real tracks.
- **D2 — Stem selection happens after processing.** Separation always computes
  every stem the chosen model produces (the model does this regardless), and
  the user selects which stems to include in a custom ZIP on the results
  screen. No pre-selection plumbing in the job request/API.
- **D3 — Analyzer stack.** DeepRhythm for BPM (rides the existing torch
  dependency, best open-source accuracy), librosa chroma + Krumhansl-Schmuckler
  key profiles for key. If the DeepRhythm checkpoint/license proves
  restrictive at implementation time, fall back to librosa for both; the
  analyzer interface must make that a one-file swap.
  **Implementation note (2026-09-09):** DeepRhythm is AGPL-3.0 (PyPI
  classifiers), which triggered the pre-authorized fallback — Phase C shipped
  with librosa for both BPM and key, and `worker/worker/analysis.py` is the
  single swap point if a permissively licensed high-accuracy tempo estimator
  appears later.

Out of scope for both phases (still governed by `STEM_EXTRACTOR_PLAN.md` §3):
cloud execution, real-time/latency-critical analysis, MIDI transcription,
training or fine-tuning models, processing beyond the 8-minute local limit.

---

## Phase A — 6-stem separation (piano + guitar)

### A1. Worker: add the `htdemucs_6s` model profile

The allowlist pattern in `worker/worker/models/profiles.py` already exists; this
is a new entry, not new machinery.

- New profile `demucs_6s`: `model_id="htdemucs_6s"`, the official
  `hybrid_transformer/...6s...th` checkpoint identifier and its recorded
  checksum (verify via `torch.hub.load_state_dict_from_url` hash check on the
  main machine; record the exact values in the profile).
- `model_stems=("drums", "bass", "other", "vocals", "guitar", "piano")`.
- `supported_modes=("vocals_instrumental", "full_stems")` — the 6-stem profile
  also unblocks `full_stems`, which is currently gated on benchmarks
  (`STEM_EXTRACTOR_PLAN.md` §4.3). Benchmarks on the main machine decide if it
  ships enabled.
- `instrumental_policy="direct_model_output"` for this profile? No — keep
  `"mixture_minus_vocals"` so `vocals_instrumental` mode stays
  mixture-consistent on the 6-stem model too.
- Extend the adapter (`worker/worker/models/demucs.py`): `separate()` already
  returns whatever stems the model emits; verify no 4-stem assumptions exist
  (the `vocals_instrumental` path is mixture-minus-vocals and stays correct).

Acceptance:
- [ ] `python -m worker.cli separate --input <clip> --mode full_stems` on the
      main machine produces six encoded stems + manifest + ZIP.
- [ ] Checkpoint integrity failure stops the job with `MODEL_LOAD_FAILED`
      (existing tests cover the mechanism; add one profile-specific case).
- [ ] `validate_profile` rejects any hand-edited 6s profile.

### A2. Web: expose the 6-stem mode

- `apps/web/lib/limits.ts` already lists `full_stems`; the source picker
  (`apps/web/components/source-picker.tsx`) hides it behind a "coming soon"
  note. Enable the option with stem count labels ("4-stem", "6-stem").
- `packages/contracts/schemas/common.schema.json`: `stemKey` enum gains
  `guitar`, `piano` (additive, schema-valid change); `separationMode` already
  contains `full_stems`.
- Worker labels (`worker/worker/packaging.py` `STEM_LABELS`) gain
  `guitar: "Guitar"`, `piano: "Piano"`; web `STEM_KEYS` in
  `apps/web/lib/limits.ts` and the download-name labels in
  `apps/web/lib/downloads.ts` (`STEM_LABELS`) gain
  `guitar: "Extracted Guitar"`, `piano: "Extracted Piano"`.

Acceptance:
- [ ] A 6-stem job shows Guitar/Piano cards with working preview, download
      ("Song - Extracted Guitar.mp3" naming), and a full ZIP.

### A3. Post-processing stem selection (D2)

- On the completed screen (`apps/web/app/jobs/[jobId]/page.tsx`), each stem
  card gets a checkbox (default: all selected). "Download ZIP" builds the ZIP
  from the selection client-side by hitting a new route.
- New route `GET /api/jobs/[jobId]/downloads?kind=zip&stems=vocals,drums`
  (validation: repeated allowlisted keys, deduped, min 1, never `archive`).
  Server rebuilds a per-request ZIP from the stored stem files with the same
  deterministic packaging code path as the worker (`build_zip`), streams it,
  caches nothing.
- Download filename: `"Song - Stems.zip"` unchanged; manifest inside the
  custom ZIP includes only the selected stems.

Acceptance:
- [ ] Unchecking Piano excludes it from the custom ZIP (and its manifest row).
- [ ] Unknown/duplicate stem params fall back to all stems or 400.
- [ ] The default "Download all (ZIP)" still serves the worker-built archive.

### A4. Benchmarks before enabling (do not skip)

- `worker/worker/benchmark.py` already exists; run the 6-stem profile against
  a real track on the main machine. Record results in `docs/BENCHMARKS.md`.
- Gate: CPU full-length-track runtime must be within ~2x of the 4-stem run,
  or 6-stem ships with an explicit "experimental" badge in the UI instead.

---

## Phase B — Drum subdivision (kick/snare/cymbals/toms)

Hard gate: Phase A is completed, benchmarked, and in daily use first.

**Implementation status (2026-09-09): code complete; quality gate pending.**

- **Model**: `drumsep` (inagoy/drumsep, 2022) — MIT-licensed code; a hybrid-
  demucs checkpoint distributed as a single `49469ca8.th` file on Google Drive
  (file id `1-Dm666ScPkg8Gt2-lK3Ua0xOudWHZBGC`, per the project's own install
  script). Not a demucs remote-index model: it loads via demucs' local-repo
  API (`get_model(name, repo=dir)`) through the adapter in
  `worker/worker/models/drumsep.py`. Output: 4 parts — kick, snare, cymbals,
  toms (source-name mapping with positional fallback, locked by tests).
- **Reference-machine setup (one-time)**:
  `pip install gdown; gdown 1-Dm666ScPkg8Gt2-lK3Ua0xOudWHZBGC -O data/models/drumsep/49469ca8.th`
  The first refine run prints the checkpoint's sha256 prefix to stderr; pin it
  as `checkpoint_checksum` in `DRUMSEP_PROFILE` so every later load is
  verified (empty checksum = unpinned, by design until that first run).
- **Interaction model**: new separation mode `drum_breakdown`. The web
  "Refine drums" action on a completed job queues a normal refine job whose
  input is the parent's stored drums output — the standard worker pipeline
  (validate/decode/separate/encode/package) runs unchanged. Outputs are extra
  `job_outputs` rows keyed `drums_kick`/`drums_snare`/`drums_cymbals`/
  `drums_toms`; the parent job keeps its own outputs. BPM/key analysis is
  skipped for refine jobs (meaningless on drum parts). The jobs.mode CHECK
  constraint was widened by a gated table-rebuild migration on both clients
  (SQLite cannot alter a CHECK).
- **Risk register**: experimental SDR quality (noticeably below htdemucs),
  second model download, slower jobs, no publisher-published checkpoint hash
  (pin-on-first-run above). If quality on real tracks disappoints, this phase
  is dropped without further investment.

Acceptance for staying in the roadmap at all:
- [ ] A main-machine quality pass on 3-5 real tracks; product owner listens.
- [x] Schema/labels/UI plumbing mirroring A2/A3 for the drum parts (shipped
      ahead of the quality gate so the listen test exercises the real flow).

---

## Phase C — Key & BPM detection (D3)

### C1. Worker analysis module

- New module `worker/worker/analysis.py`, run in the pipeline after
  separation, before packaging:
  - **BPM**: DeepRhythm `predict_from_audio` on the **drums stem** waveform
    (isolated percussion measurably improves tempo accuracy; the waveform is
    already in memory). Double/half-time sanity clamp against the full-mix
    estimate. Report `{bpm, confidence}`.
  - **Key**: librosa chroma (`chroma_cqt`) on the full mix, correlate against
    Krumhansl-Schmuckler major/minor profiles, report
    `{key: "F# minor", camelot: "11A"}` (Camelot included for DJ use).
- Both wrapped in a graceful-degradation contract: analyzer import failure or
  unexpected audio never fails the job; the manifest fields are simply absent
  and a `job_event` records the degradation. New optional dependency group in
  `requirements.txt` (comment-documented like the existing optional groups).

Acceptance:
- [ ] A completed job's `manifest.json` carries `analysis: {bpm, key, camelot}`
      for real tracks; a simulated analyzer failure leaves the job completed
      without those fields.

### C2. Surface in web + downloads

- `job_outputs`/manifest values surface through the job view
  (`apps/web/lib/job-view.ts`) and render as small badges on the results
  screen ("128 BPM · F# minor (11A)").
- The manifest inside every ZIP includes the same fields (schema addition to
  `output-manifest.schema.json`: optional `analysis` object — additive).

Acceptance:
- [ ] Results screen shows BPM/key for a normal completed job.
- [ ] A ZIP's manifest carries identical values.

### C3. Accuracy validation on the main machine

- Hand-verify against 5-10 tracks with known BPM/key (Rekordbox or Mixed In
  Key as reference). Success bar: BPM within ±1 for steady-tempo tracks, key
  correct ≥ 80%. Below bar → revisit D3 with the accuracy/licensing table
  (DeepRhythm AGPL, Essentia AGPL + no Windows wheels); a swap is confined to
  `worker/worker/analysis.py` by design.

---

## Sequencing

```
A1 → A4 → A2 → A3        (Phase A ships: 6-stem + selectable ZIPs)
      ↘ (daily use) → B gate decision
C1 → C2 → C3             (independent of A/B; can proceed in parallel)
```

Phase C has no dependency on A/B, but lands after A1/A2 in the UI so stem
labels and the results-screen layout only change once.

Every phase ends with a benchmark note in `docs/BENCHMARKS.md` and the usual
local verification (web build, worker pytest/ruff) plus a main-machine listen
before merge to `main`.
