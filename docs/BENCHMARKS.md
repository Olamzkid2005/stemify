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

### Worker pool: 1 process vs 2 (concurrency plan C5)

Same machine, same fixtures, same pipeline (`vocals_instrumental`, mp3), four
20 s jobs queued up front and left to drain. Pool 1 is today's sequential
behaviour; pool 2 is the shipped default, splitting the box's 4 cores into 2
threads per worker.

| Configuration | Wall time, 4 jobs | Per-job time (median) | Concurrent starts | Workers used |
|---|---|---|---|---|
| `--pool 1` (4 threads each) | **706 s** | 175.7 s | 0 | 1 |
| `--pool 2` (2 threads each) | **618 s** | 312.1 s | 3 | 2 |

Reading it honestly:

- **Throughput: +12.5%.** 88 s saved on four jobs. Real, but not the point — on a
  4-thread CPU there is not much to win; the win needs more cores.
- **Latency: each job is 1.8x slower** (176 s → 312 s). This is the trade the
  pool makes, and it is why pooling threads inside one process would be worse:
  one inference already saturates all four cores, so the split is what keeps
  "two jobs each slower" from becoming "two jobs each four times slower".
- **Both jobs actually move at once.** Three of the four jobs started while
  another was still running (read from the jobs table, not assumed from the pool
  size), so two browser tabs show two moving progress bars instead of one waiting
  behind the other. That is the product change; the throughput number is a bonus.
- **`STEMIFY_WORKER_CONCURRENCY=1` still exists** and restores the left column
  exactly — one job at a time, fastest per-job latency. It remains the right
  setting on a CUDA box (two processes sharing one GPU can exhaust VRAM).
- **Shipped default stays 2**, because the measured total is better *and* the
  concurrency is the feature. On this hardware a 4th job finishes sooner than it
  would have, even though every individual job takes longer.

Memory: each worker holds its own model, so the pool's footprint is
`pool x~1 GB` (measured peak RSS per worker 0.9-1.0 GB, flat in track length).
Two workers on a 4 GB machine is already tight; see `worker/README.md`.

Note the per-job times here are the **whole job** (validate, decode, separate,
analyze, encode, package, ZIP) and so are larger than the separation-only rows
above; the analysis pass (BPM/key) and packaging are a real share of a short
fixture. They are the number a person actually waits for.

Reproduce (from `worker/`; throws away its own data directory and copies the
checkpoint so it never touches the app's queue):

```bash
PYTHONPATH="$PWD/.runtime" python -m worker.pool_benchmark --pool 1 --jobs 4 --duration 20 --out /tmp/pool-1.json
PYTHONPATH="$PWD/.runtime" python -m worker.pool_benchmark --pool 2 --jobs 4 --duration 20 --out /tmp/pool-2.json
```

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
2. **`full_stems` is the 3-stem rhythm split on the default model.** It
   outputs Drums, Bass and the residual instrumental bed from the same
   htdemucs checkpoint, so separation cost equals the 2-stem flow and only
   encoding/validation differs. The experimental `htdemucs_6s` variant was
   dropped after listening review: its Guitar and Piano sources were not
   accurate enough to ship, and the stems it shares with the 4-source model
   sounded the same. The remaining quality question is model-side, not
   pass-count-side — stacking inference passes (`best`: 0.45 overlap / 5
   shifts) cost ~2.5x the time for no audible gain, so it was removed in
   favour of a `fast`/`balanced` choice.
3. **CUDA remains auto-detected, never required.** `resolve_device` falls back
   to CPU cleanly, and a CPU-only build is fully functional (this entire
   benchmark ran without CUDA).
4. **Separation progress is per finished chunk, and costs nothing measurable.**
   demucs 4.0.1's `apply_model(shifts, split=True)` is a single blocking call
   that reports only its start and end, which parked the job progress bar at one
   value for the entire inference (90%+ of wall time). The worker now replicates
   apply_model's two outer paths — the shift trick and the chunked split pass —
   so every finished chunk fires a progress callback; a 480 s track at the 7.8 s
   chunk length therefore emits ~40 updates per shift pass. This changes nothing
   about the audio: the result was verified *bit-identical* to `apply_model` on
   the real htdemucs checkpoint for both `shifts=0` and the default `shifts=2`
   (`worker/tests/test_engine_progress.py`). The replication is guarded by a
   source-marker check, so a demucs upgrade degrades progress to the old
   two-step behaviour rather than risking wrong stems — an acceptable trade,
   and the guard's test fails loudly so the drift gets noticed. Progress writes
   are bounded: only whole percents are persisted (31-74 across the stage) and
   repeated percentages are dropped, so a job adds at most 43 `job_events`
   rows regardless of how many chunks it processes.
5. **Unsupported/overcommitted hardware messaging.** A CUDA-only request
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
