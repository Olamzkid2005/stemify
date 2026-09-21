# Parallel Job Processing — Design Plan

Status: **BUILT** (C1–C5). Stemify now accepts several jobs per browser and runs
enough worker processes to work more than one at a time. Measured cost and the
shipped pool size: `docs/BENCHMARKS.md`; operating instructions:
`worker/README.md`, "The worker pool".

What the milestones landed as, and the two places this document was refined by
the implementation:

- **C1** `workers` table + `register_worker`/`unregister_worker`, ownership-based
  `recover_stale_processing_jobs` (a 30 s staleness window), and the same
  recovery on every heartbeat tick. `write_heartbeat` also refreshes the worker's
  own row, so a queue used outside `run()` self-registers instead of leaving a
  NULL owner.
- **C2** `start.sh` starts `STEMIFY_WORKER_CONCURRENCY` processes, tracks their
  PIDs in an array, kills every slot on exit, and respawns a slot only if it had
  been alive for 5 s. `STEMIFY_WORKER_THREADS` is exported as `cores / pool` (an
  explicit `OMP_NUM_THREADS` still wins) and applied at `worker.job_loop` import,
  before anything imports torch. `update_progress` is best-effort; every other
  write stays strict.
- **C3** `MAX_ACTIVE_JOBS` defaults to 3 (pool 2 + 1 waiting job) and the
  refine-drums route shares that bound instead of keeping its own `>= 1`.
- **C4** `GET /api/jobs` + `job-list.schema.json`, queue positions derived from
  the claim's own `ORDER BY created_at, id`, a home-page list that reuses
  `getJobView`, and a warn-but-accept line under the picker fed by one shared
  poller.
- **C5** `python -m worker.pool_benchmark --pool N --jobs M` measures the pool the
  way `start.sh` runs it (N processes, one queue, a split thread budget); the
  numbers and the shipped default are in `docs/BENCHMARKS.md`.

Refinements worth knowing about:

1. **Queue position counts waiting jobs only** (this document's formula), so the
   front of the queue reads "next in line" even while another job is running —
   it is the next *claim*, not the next to start.
2. **`.env` may pin `MAX_ACTIVE_JOBS`.** The default was raised, but an existing
   `MAX_ACTIVE_JOBS=1` in a local `.env` still caps one browser at one job. The
   pool does not read that variable; only the API does.

Decisions already made with the operator:

- **True parallel pool** — separate worker processes, not a queue-only change.
- **Full job list with queue position** on the home page.
- **Warn but always accept** — never refuse a job because others are running.

## 1. Product goal

Opening several tabs (or using several browsers) lets each tab submit its own
job, and more than one separation genuinely runs at the same time. Every job is
accepted and visible, the home page lists them with their queue position, and
submitting a job while others run warns that they will share the CPU.

## 2. Why it does not work today

### 2.1 Recovery would kill a live job (blocker)

`JobQueue.recover_stale_processing_jobs()` fails **every** `processing` row at
startup, on the documented assumption that "the local milestone runs one worker
per data directory, so any row still processing at startup belongs to a dead
process". Start a second worker and it immediately fails the job the first
worker is in the middle of. No pool can exist until recovery knows *which*
worker owns a job and whether that worker is alive. The `jobs.worker_call_id`
column set at claim is already the right hook.

### 2.2 The web refuses the second job

`MAX_ACTIVE_JOBS` (default 1) counts `queued` + `processing` per `owner_key`;
the second submission returns `429 too_many_active_jobs`. Tabs share the guest
cookie, so this is one cap for all of them. `POST /api/jobs/{id}/refine-drums`
applies the same cap.

### 2.3 The worker loop is single-job by construction

`worker.job_loop.run()` claims at most one job per iteration and processes it
synchronously; `start.sh` starts one process.

### 2.4 Two smaller hazards

- **SQLite write contention.** More writers means `update_progress`'s `UPDATE`
  can hit `SQLITE_BUSY` after `busy_timeout`. That call is not best-effort, so a
  locked database would raise inside `process_job` and fail a job over a
  *progress row*. `record_event` already swallows its failures; progress should
  match it. Job state transitions stay strict.
- **Thread oversubscription.** One inference already saturates all cores. N
  workers each using every core is worse than N workers sharing them.

## 3. Design

### 3.1 Two independent knobs

| Knob | Meaning | Governs |
|---|---|---|
| `MAX_ACTIVE_JOBS` | how many jobs one browser may have pending | what the API accepts |
| `STEMIFY_WORKER_CONCURRENCY` | how many jobs run at once | how fast the queue drains |

The pool size is **explicit, not auto-detected**: the operator sets it, the
launcher starts that many processes. Auto-sizing from cores/RAM is a non-goal.

### 3.2 A pool of processes, not threads

- The claim is already atomic (`BEGIN IMMEDIATE` + a conditional `UPDATE` in
  `claim_next_queued_job`), so N processes need **no new locking** — the queue
  was built for it.
- One process per worker gives crash isolation: a worker that runs out of memory
  takes down its own job, not the others.
- Threads inside one process would share the GIL on the Python-side pipeline and
  would share one non-thread-safe model object.

`start.sh` starts the pool. Its `cleanup()` currently kills exactly two named
PIDs (`WEB_PID`, `WORKER_PID`), so the pool must extend it to an array of slot
PIDs — otherwise Ctrl+C leaves the extra workers running, silently claiming and
processing jobs while the app is closed. A dead slot is respawned, but only if it
had been alive for more than a few seconds, so a missing runtime cannot become a
fast crash-loop that floods the log.

### 3.3 Ownership-based recovery and per-worker liveness

New table, additive on both sides (mirrors how `worker_heartbeat` is declared in
the web schema and in `worker/worker/database.py`):

```sql
CREATE TABLE IF NOT EXISTS workers (
  worker_call_id TEXT PRIMARY KEY,
  pid INTEGER,
  started_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
```

- A worker registers on start and its heartbeat thread upserts **its own** row
  every 5 s (same tick that already writes `worker_heartbeat`, which stays as
  the single "is any worker up" signal the UI reads).
- Clean shutdown deletes its row, so a restart does not wait out the window.
- Recovery fails a `processing` job only when its `worker_call_id` is missing
  (legacy rows) or belongs to no live row / a row older than the staleness
  window. A job owned by a *running* peer is never touched.
- Recovery runs at startup and from the heartbeat tick, so a peer that dies is
  cleaned up promptly instead of at the next restart. The statement is
  idempotent, so several workers running it is harmless.
- Staleness window: 30 s (6 heartbeat periods) — long enough that a stalled disk
  write cannot orphan a live job.

### 3.4 Thread budget per worker

The launcher exports `STEMIFY_WORKER_THREADS=max(1, cores // pool)` and the
worker applies it to `OMP_NUM_THREADS` / `MKL_NUM_THREADS` /
`OPENBLAS_NUM_THREADS` / `NUMEXPR_NUM_THREADS` at *import* of `worker.job_loop`
(`_apply_thread_budget`). Those are read by the numeric stack when it loads, and
`worker.job_loop` imports no torch, so the first model import already sees the
budget — no `torch.set_num_threads()` call is needed, and an explicit value of
any of those four variables still wins. This is what separates "two jobs each
~2× slower" from "two jobs each 4× slower".

### 3.5 Memory

Every worker loads and holds its own model. The README must state the arithmetic
(pool × measured RSS must stay well below RAM) and the plan's last milestone
measures the real number with `python -m worker.benchmark` rather than guessing.
On a CUDA box the default pool stays 1 unless VRAM is large: two processes
sharing one GPU can OOM, and `GPU_OUT_OF_MEMORY` is already a mapped public
error.

### 3.6 UI: job list, queue position, warning

- `GET /api/jobs` — owner-scoped list of the most recent jobs (cap 10), built by
  reusing `getJobView` so **one** sanitizer keeps deciding what leaves the
  server. New `job-list.schema.json` contract.
- **Queue position** for a queued job = `1 +` the number of queued jobs created
  before it, ordered exactly as the claim is (`created_at, id`). If the two
  orders ever diverge the position lies, so it is derived from the same
  ordering and tested.
- Home page gains a "Your jobs" list: filename, mode, status, progress, and
  "Queued — N ahead" for waiting jobs, each linking to its job page.
- Submitting while at least one job is active shows a warning and **still
  accepts**: "You already have N jobs running. Adding another shares the same
  CPU, so all of them will take longer."

## 4. Failure modes

| Failure | Behaviour |
|---|---|
| A pool worker dies mid-job | its job is failed as `UNKNOWN` after the staleness window (existing message); peers keep running; the launcher respawns the slot |
| `SQLITE_BUSY` on a progress write | ignored — progress is cosmetic; never fails a job |
| `SQLITE_BUSY` on a state change | retried by `busy_timeout`; failing that the job stays claimable/recoverable |
| Model OOM in one worker | only that job fails (`GPU_OUT_OF_MEMORY` / `UNKNOWN`) |
| Whole pool stopped | home page shows the existing worker-unavailable state |

## 5. Milestones

| # | Deliverable | Tests |
|---|---|---|
| C1 | **prerequisite** ownership-based recovery + per-worker liveness + periodic recovery | done in `tests/test_queue.py` (live peer's job untouched, stale owner's job failed, legacy `NULL` owner still failed, best-effort progress) and `tests/test_heartbeat.py` (per-worker rows, the tick's recovery, `run()` registering and releasing) — not `test_lifecycle.py`, which skips without ffmpeg |
| C2 | the pool: `STEMIFY_WORKER_CONCURRENCY`, thread budget, `start.sh` pool + slot-PID cleanup + guarded respawn, best-effort progress writes | `tests/test_thread_budget.py`, and three new `tests/test_startup_smoke.sh` cases that start a real 3-slot pool and assert every slot dies on exit (the old cases' `sed` never matched the dev-server line, so they proved nothing — fixed) |
| C3 | web capacity: `MAX_ACTIVE_JOBS` raised to pool + headroom, refine-drums shares the bound, 429 kept as backstop | extend `lib/jobs-limit.test.ts` |
| C4 | `GET /api/jobs` + `job-list.schema.json` + queue position + home-page list + warn-but-accept copy | contract test for the list; web test for the queue-position ordering and the warning in the existing picker render test |
| C5 | measure pool 1 vs 2, record in `docs/BENCHMARKS.md`, set the shipped default from the number, README sizing/RAM/CUDA guidance | `tests/test_pool_benchmark.py` (the tool scores throughput, so it needs its own tests); the numbers themselves are in `docs/BENCHMARKS.md` |

C1 changes no behaviour at pool size 1; the existing orphan-recovery tests must
keep passing unchanged, which is the point of doing it first.

## 6. Explicit non-goals (v1)

- **Auto-sizing the pool** from machine specs (the operator chooses).
- **Distributed workers** or more than one data directory.
- **Priority or preemption** — FIFO by `created_at, id`, unchanged.
- **Dynamic pool resizing** while jobs are running.
- **A thread pool inside one process.**
- **Per-job CPU/RAM quotas.**
- Any change to the separation pipeline, progress semantics, storage layout,
  retention/cleanup, or the existing job/status contracts.

## 7. Risks and honest expectations

- On the reference laptop (4 threads) two workers each get 2 threads, so each
  job is roughly 1.5–2× slower than running alone. Two jobs then finish at
  roughly the same time as running them back to back, but *both move at once*
  instead of one waiting behind the other. Real throughput gain needs more
  cores; `STEMIFY_WORKER_CONCURRENCY=1` restores today's exact behaviour and
  still gives the accepted-and-queued UX from C3/C4.
- The default pool size ships from the C5 measurement, not from this document.
- More moving parts means more ways to be slow: the honest metric to watch is
  total time for N jobs, which C5 records.

## 8. Config surface

| Variable | Default | Notes |
|---|---|---|
| `STEMIFY_WORKER_CONCURRENCY` | 2 (C5: 618 s vs 706 s for 4 jobs) | 1 disables parallelism entirely |
| `MAX_ACTIVE_JOBS` | 3 (C3: pool + one queued) | per browser; the API's backstop stays |
| `STEMIFY_WORKER_THREADS` | `max(1, cores // pool)` | exported by the launcher, applied by the worker |
| `STEMIFY_WORKER_POLL_MS` | 500 | unchanged — idle claim polls are one indexed `SELECT` and not worth tuning |
