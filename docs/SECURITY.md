# Security & Privacy Review (plan Task 17)

Status: reviewed against the plan's Task 17 work items and acceptance criteria.
Each review area names the enforcing code and the regression tests that keep it
true. Rerun the test suites after any change touching these areas.

## 1. Path containment and upload handling

**Enforcement:** `apps/web/lib/storage/local.ts` (`objectPath`), `apps/web/lib/storage/types.ts`
(`sanitizeFilename`, `sourceObjectKey`), `apps/web/lib/storage/signable.ts` (`assertSignableKey`).

- Upload object keys are **server-generated** (`sources/upl_<32hex>/<sanitized name>`); the
  client never dictates a path. Filenames are stripped of path separators, control
  characters, and anything outside `[A-Za-z0-9._-]`, bounded to 128 chars, and dot-only
  names (`..`, `.`) collapse to `file`.
- Object keys are namespace-restricted to `sources/` and `results/` before any storage
  operation; model checkpoints, the SQLite file, and logs are unreachable by key.
- Key resolution additionally rejects any `..` or `.` segment outright and verifies the
  resolved path stays inside the data directory (defense in depth: namespace guard +
  segment check + containment check).
- Uploads are size-capped (100 MB) and extension-checked at the API; the worker
  re-validates with ffprobe as the final authority (extensions and MIME are never trusted).
- Download paths are never built from request strings: the stem parameter must match
  `^[a-z]{1,16}$`, is matched against an allowlist, and the file path comes from stored
  `job_outputs.relative_path` metadata (regex-checked `results/<job>/<file>` shape).

**Tests:** `apps/web/lib/security.test.ts`, `apps/web/lib/downloads.test.ts`.

## 2. Cookie / session authorization

**Enforcement:** `apps/web/lib/auth/guest.ts`, ownership filters in `jobs.ts` / `job-view.ts` / `downloads.ts`.

- The guest cookie is `httpOnly`, `sameSite=lax`, `secure` in production, 30-day max-age,
  and holds `gid_<32hex>.<HMAC-SHA256 signature>` keyed by `JOB_ACCESS_TOKEN_SECRET`.
  Verification uses `timingSafeEqual`; forged or unsigned cookies fail closed and a fresh
  identity is issued.
- Every job view, download, and job-creation query filters by `owner_key`; another
  session's job is indistinguishable from an unknown one (404) — no existence oracle.
- Uploads can only be attached to jobs by their owning session (`upload_forbidden`).

**Tests:** `apps/web/lib/security.test.ts`, `apps/web/lib/jobs.test.ts`, `apps/web/lib/downloads.test.ts`.

## 3. Localhost binding

**Enforcement:** `start.sh` exports `HOSTNAME=127.0.0.1` for the Next.js dev server;
the worker exposes **no** HTTP endpoint at all (it polls SQLite and writes a heartbeat row).

- The local application never listens on a LAN interface by default. Any future opt-in
  for LAN exposure must be an explicit, documented configuration change.

**Tests:** startup smoke tests cover the launch path; binding is asserted by
`.env.example` (`HOSTNAME=127.0.0.1`) and reviewed in this document.

## 4. Subprocess arguments and URL handling

**Enforcement:** `worker/worker/input_audio.py`, `worker/worker/encoding.py`, `worker/worker/youtube.py`.

- Every subprocess invocation (`ffprobe`, `ffmpeg` decode, `ffmpeg` encode, `yt-dlp`) is a
  **fixed argument array** — no `shell=True` anywhere in the worker, and every call has a
  timeout. User-controlled strings (filenames, URLs) are always passed as single argv
  elements, never interpolated into command strings.
- YouTube URLs are allowlisted twice (web at creation, worker before download): https-only,
  approved hosts only, no userinfo, length-capped. The URL is passed to yt-dlp **after
  `--`** (end-of-options marker) so it can never be parsed as a flag. Downloader behavior
  is pinned by fixed arguments (`--no-playlist`, `--extract-audio`, size cap, output
  template); users cannot supply downloader options.
- Downloaded media passes the same validation ladder as uploads before use.

**Tests:** `worker/tests/test_security.py` (AST walk over every `subprocess.run` site,
allowlist injection vectors), `worker/tests/test_youtube.py`.

## 5. Logs and runtime data exclusions

**Enforcement:** `.gitignore` (repo root), `worker/.gitignore`, `apps/web/.gitignore`; log lines in `worker/worker/job_loop.py`.

- Gitignored: `data/` (all runtime data — database, sources, results, models, logs),
  `*.th` / `*.ckpt` (model weights), `.env` and `.env.*` (except `.env.example`),
  media extensions, logs, and the local `.runtime` dependency tree.
- Worker console output is a small set of reviewed status lines (job IDs, stages); public
  error messages are static strings per error code — raw exceptions, paths, and stderr
  never reach the UI. `diagnostic_reference` correlates a job to local logs without
  exposing content.
- A hygiene test walks the tracked file list on every run and fails if any runtime
  artifact, weight, database, media file, or env file is committed.

**Tests:** `worker/tests/test_repo_hygiene.py`, `worker/tests/test_security.py`.

## 6. Licenses

| Component | License | Note |
|---|---|---|
| demucs 4.0.1 (code) | MIT | facebookresearch/demucs |
| htdemucs weights | MIT (research release) | verify before any redistribution or commercial use |
| PyTorch / torchaudio | BSD-3 | runtime only |
| numpy | BSD-3 | runtime only |
| soundfile (libsndfile) | LGPL-2.1 | linked at runtime, not redistributed modified |
| ffmpeg/ffprobe | LGPL-2.1+ (system-provided) | not vendored in the repo; user installs |
| yt-dlp | Unlicense | optional; YouTube feature only |
| psutil | BSD-3 | optional; benchmarking only |

The pinned profile records its license reference in
`worker/worker/models/profiles.py` (`license_reference`), asserted by test.

## 7. Local data deletion behavior

- **Results** live under `data/results/<job_id>/` and are deleted by the retention
  cleanup (`JOB_RETENTION_HOURS`, default 24h) — either the scheduled pass or
  `python -m worker.cli cleanup`. The job row is marked `expired` and its
  `job_outputs` rows are removed; downloads then fail closed (409/410).
- **Sources** live under `data/sources/upl_*/`; uploads carry a 24h `expires_at` and
  abandoned uploads (and crash-window orphans) are deleted by the same cleanup pass.
- **Temporary data**: each job runs in a unique temp directory removed on every path;
  stale `stemify-job-*` dirs older than 6 hours are reaped.
- **Model checkpoints** (`data/models/`) are *not* job data and are never touched by
  cleanup (structurally outside the cleanup namespace and asserted by test).
- **Complete wipe**: delete the `data/` directory (or the whole repository clone);
  no data exists outside it. The SQLite database (`data/stemify.sqlite3`) holds all
  job metadata; deleting it removes every job record permanently.
- Nothing is ever sent off the machine: there are no telemetry, analytics, or cloud
  uploads of any kind (YouTube import *downloads* from the network; nothing uploads).

## Acceptance checklist

- [x] No known critical local file disclosure (namespace guard + segment rejection +
      containment check; download paths server-owned; tested).
- [x] No command injection (argument arrays only, no shell, timeouts, URL passed after
      `--`; AST-level regression test).
- [x] Runtime data and model weights are not committed (gitignore + tracked-tree test).
- [x] Privacy and content-policy behavior documented (this file; see below).

## Content policy

Stemify is a **local, personal-use tool**. Users must only process audio they have the
right to use; the upload flow requires no account, so the policy acknowledgement on the
YouTube tab (Terms-of-Service compliance and rights confirmation) is the recorded user
affirmation for imported sources. The application intentionally does not bypass DRM,
private access controls, age gates, or authentication of any kind, and yt-dlp failures
for restricted sources surface as ordinary `DOWNLOAD_FAILED` errors.
