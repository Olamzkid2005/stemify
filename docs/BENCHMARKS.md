# Performance Benchmarks (plan Task 16 / Section 18)

Local performance expectations for the default `demucs_default` profile
(htdemucs via demucs 4.0.1). All numbers below were produced by the reproducible
method in the next section; rerun it on any machine to regenerate.

## Environment measured

| Item | Value |
|---|---|
| CPU | Intel Core i5-3210M-class (Ivy Bridge, Family 6 Model 58), 4 threads |
| RAM | 17.9 GB |
| CUDA | not available (CPU-only build) |
| Python | 3.13.12 |
| torch | 2.6.0+cpu, 4 intra-op threads |
| demucs | 4.0.1, htdemucs checkpoint (84.1 MB, sha256 prefix 8726e21a) |

## Results

### Model load

| Phase | Time |
|---|---|
| Cold load (checkpoint read + checksum verify) | ~1.5 s |
| Warm load (in-process cache hit) | < 0.01 s |

### Separation (vocals_instrumental, stereo 44.1 kHz)

| Fixture | Wall time | Realtime factor | Peak RSS | MP3 output |
|---|---|---|---|---|
| 5 s | 23.5 s | 4.70x slower than realtime | 888 MB | 0.4 MB |
| 10 s | 43.3 s | 4.33x | 964 MB | 0.8 MB |
| 20 s | 62.9 s | 3.15x | 886 MB | 1.6 MB |
| 30 s | 112.8 s | 3.76x | 930 MB | — |
| Mono 5 s | 17.5 s | 3.5x | — | — (upmixed to stereo) |

Peak RSS plateaus around 0.9–1.0 GB regardless of fixture length because the
model, not the audio buffer, dominates memory. Python-level peak allocations
stay under 90 MB.

### Encoding (20 s fixture, per format)

| Format | Encode time | Output size |
|---|---|---|
| mp3 | 1.5 s | 1.6 MB |
| wav | 0.8 s | 7.1 MB |
| flac | 0.8 s | 6.1 MB |
| ogg | 1.6 s | 0.9 MB |
| m4a | 3.2 s | 1.1 MB |

Encoding is negligible next to separation for every format.

## Reproducing

From `worker/` (adjust `PYTHONPATH` to your environment; see the README's
Store-Python fallback):

```bash
PYTHONPATH="$PWD/.runtime" STEMIFY_MODEL_DIR="$PWD/data/models" \
  python -m worker.benchmark --durations 10 20 --formats mp3 wav flac ogg m4a
```

- `--durations` accepts any list of fixture lengths in seconds.
- `--quick` runs a single 5 s fixture.
- Fixtures are deterministic synthesized tones (seeded RNG), so runs are
  comparable across machines; no copyrighted material is needed.
- The report is a single JSON document: environment, model load, separation
  rows, encoding rows, totals.

## Decisions from evidence

1. **Duration limit stays 480 s (8 minutes).** At ~3.2–4.7x slower than
   realtime on this 4-thread CPU, a 480 s track takes roughly 26–38 minutes of
   separation. That is slow but workable for a local tool; the memory profile
   is flat, so duration is bounded by patience, not RAM. Machines at least
   ~4x faster (any modern 8-core desktop) process a 480 s track in under
   10 minutes.
2. **`full_stems` stays disabled.** htdemucs always computes all 4 sources
   (a 4-source `apply_model` call costs the same ~4x realtime as the 2-stem
   flow), so the extra cost of full-stem mode is encoding/validating four
   stems instead of two — but the plan (Section 18) requires listening
   evaluation (vocal bleed, artifacts, transient damage) before enabling it,
   which synthesized-tone benchmarks cannot provide. Keep the gate until
   quality review is done; revisit with real music fixtures (Task 18).
3. **CUDA remains auto-detected, never required.** `resolve_device` falls back
   to CPU cleanly, and a CPU-only build is fully functional (this entire
   benchmark ran without CUDA).
4. **Unsupported/overcommitted hardware messaging.** A CUDA-only request
   without a GPU fails with `MODEL_LOAD_FAILED` and the message "install
   CUDA-enabled torch or set STEMIFY_DEVICE=cpu" (tested). A missing Python
   runtime fails jobs with the MODEL_LOAD_FAILED setup hint. Nothing here
   needs VRAM; CPU memory ~1 GB peak is the practical floor (4 GB RAM
   machines are fine).

## Caveats

- Synthetic tones exercise the full pipeline but say nothing about separation
  *quality* on real music; quality review is Task 18's listening pass.
- `realtime_factor` varies with CPU load; expect ±20% run to run.
- Windows process RSS includes the whole interpreter; the model dominates it.
