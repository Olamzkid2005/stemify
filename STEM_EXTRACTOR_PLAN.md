# Stemify Local Stem Separator Plan

## 1. Product Summary

Stemify is a local application for separating music into stems. The user runs one
command:

```bash
./start.sh
```

The command verifies the local prerequisites and starts both application processes:

- The Next.js web application and API on `http://localhost:3000`.
- The Python audio worker that processes jobs locally with Demucs.

The user then opens the local website, selects an audio file, chooses a separation
mode and output format, starts a job, watches progress, previews the results, and
downloads the generated stems.

There is no production deployment, cloud worker, managed database, cloud object
storage, or third-party runtime service in this plan. Audio files, job metadata,
model weights, logs, and generated stems remain on the local machine unless the
user explicitly copies them elsewhere.

The primary product promise is:

> Fast, simple music stem separation on your own computer.

Processing time depends on the user's CPU/GPU and track length. Do not promise a
fixed completion time until local benchmarks support a documented target.

## 2. Goals

### 2.1 Core goals

- Make the complete upload-to-results workflow work locally.
- Start the frontend and processing worker with `start.sh`.
- Use a local SQLite database for job state.
- Use local filesystem directories for source files and generated results.
- Run real Demucs separation locally rather than a placeholder implementation.
- Support vocals/instrumental separation as the default mode.
- Support full-stem separation when the selected Demucs profile passes quality and
  performance checks.
- Support MP3, WAV, FLAC, OGG, and M4A output choices.
- Keep the browser responsive while processing runs in the worker process.
- Show useful progress, failure messages, retry behavior, previews, and downloads.
- Keep input and output files private to the local application by default.
- Make the worker independently testable from the web UI.
- Keep model versions, dependencies, and processing settings reproducible.

### 2.2 Local-first goals

- A new developer can install prerequisites, run the setup commands, and use the
  application without creating accounts with infrastructure providers.
- The application works without a network connection after dependencies and model
  weights have been installed, except for optional YouTube importing.
- `start.sh` fails early with actionable setup instructions when a prerequisite is
  missing.
- Stopping `start.sh` stops both child processes cleanly.
- A stale or interrupted job cannot leave the UI spinning forever.
- A local application restart preserves job metadata and completed results.

## 3. Non-Goals

The following are explicitly out of scope for the local product:

- Vercel, Modal, Supabase, Neon, Cloudflare R2, AWS S3, or any other deployment
  provider.
- Cloud-hosted processing or remote GPU execution.
- A managed Postgres requirement.
- Direct browser uploads to remote object storage.
- Signed cloud download URLs.
- Worker callback webhooks, callback secrets, or remote event delivery.
- Production domains, public hosting, autoscaling, cloud queueing, or billing.
- Public anonymous usage controls intended to manage cloud GPU cost.
- Accounts, subscriptions, paid credits, or persistent hosted job history.
- Processing full albums, playlists, livestreams, or unrestricted long recordings.
- DRM, private, age-restricted, or otherwise access-controlled media.
- Training a new separation model.
- A DAW, waveform editor, mixer, mastering tool, or music-production suite.
- A guarantee that lossy source audio can be restored to lossless quality.

Optional YouTube import remains a local feature candidate, but it is secondary to
local file upload and requires network access while downloading the source. It must
be disabled or omitted from the first working local vertical slice if it adds
complexity.

## 4. Product Decisions

### 4.1 One local application

The user should think of Stemify as one local application. Internally it consists
of a web process and a worker process, but the user starts both with one command.

```text
start.sh
  -> Next.js web/API process on localhost:3000
  -> Python worker process using the same local data directory
```

The worker must bind to localhost only if it exposes an HTTP health/control port.
It must never listen on all network interfaces by default.

### 4.2 Upload-first experience

The home page is the working application. The upload control is the dominant first
viewport element.

The initial flow is:

1. Open `http://localhost:3000`.
2. Drop an audio file or choose one from the local filesystem.
3. Choose a separation mode.
4. Choose one output format.
5. Click `Separate audio`.
6. Watch progress.
7. Preview or download the generated stems.

YouTube import is a secondary tab or disclosure and must not obscure file upload.

### 4.3 Separation modes

Expose only validated modes:

- `Vocals + instrumental`: default mode, producing `vocals` and `instrumental`.
- `Full stems`: optional mode, producing `vocals`, `drums`, `bass`, and `other`.

The first real implementation should use a validated Demucs profile. Do not expose
full-stem mode merely because the UI can display it; it must pass the model and
performance acceptance criteria first.

### 4.4 One output format per job

The user selects one output format:

- MP3
- WAV
- FLAC
- OGG
- M4A

The job produces each stem in that format and one ZIP containing the stems plus a
safe `manifest.json`. An all-formats export is out of scope until the single-format
flow is reliable.

### 4.5 Local guest identity

No account is required. The web app uses a signed, HTTP-only local session cookie
to associate jobs with the browser. This is for accidental cross-browser access
protection, not cloud-scale abuse prevention.

The job URL must not be treated as sufficient authorization by itself. Download and
status routes must verify the local session owner or a deliberately generated job
access token.

## 5. Local Architecture

```text
                         Local computer

  +----------------+       HTTP/JSON       +----------------------+
  | Browser        | <-------------------> | Next.js web/API      |
  | localhost      |                       | localhost:3000       |
  +----------------+                       +----------+-----------+
                                                       |
                                           SQLite + local filesystem
                                                       |
                                            +----------v-----------+
                                            | Python worker        |
                                            | Demucs + FFmpeg      |
                                            | long-running process |
                                            +----------------------+
```

### 5.1 Web application responsibilities

The Next.js application is the local control plane. It must:

- Render the upload-first UI.
- Accept local file uploads through a server route.
- Validate user input before creating jobs.
- Store uploaded source files under a server-owned data directory.
- Create SQLite job records.
- Wake or notify the worker when a job is queued, or allow the worker to poll the
  queue at a short interval.
- Expose sanitized job status to the browser.
- Authorize status, preview, download, retry, and cancellation requests.
- Stream local result files only after authorization.
- Never expose absolute filesystem paths, internal database details, or raw worker
  tracebacks to the browser.

The web app does not run Demucs inference. It must remain responsive while the
worker processes a track.

### 5.2 Python worker responsibilities

The Python worker is a long-running local process started by `start.sh`. It must:

- Open the same SQLite database used by the web app.
- Claim queued jobs safely.
- Retrieve the source from the local job directory.
- Validate the actual media with `ffprobe`.
- Decode and canonicalize audio.
- Load the configured Demucs model profile.
- Run local CPU or GPU inference.
- Encode and validate the selected output format.
- Create individual stem files, a ZIP, and a manifest.
- Update job progress and terminal state in SQLite.
- Delete temporary files in success and failure paths.
- Stop cooperatively when requested by `start.sh`.

The worker must not require a web callback to report progress. SQLite is the local
source of truth for job state.

### 5.3 SQLite

SQLite stores metadata and state only. Audio bytes stay in the filesystem.

Use a local database at:

```text
data/stemify.sqlite3
```

The path must be configurable through `STEMIFY_DATA_DIR`, but the default must work
without any external service.

Configure SQLite for concurrent web/worker access:

- Enable WAL mode.
- Set a busy timeout.
- Use short transactions.
- Claim jobs with an atomic state transition.
- Avoid holding a transaction while running model inference or encoding.
- Enable foreign keys.
- Use migrations from the beginning.

### 5.4 Local filesystem storage

Use a server-owned directory layout:

```text
data/
├── stemify.sqlite3
├── uploads/
│   └── <upload-id>/
│       └── input.<safe-extension>
├── jobs/
│   └── <job-id>/
│       ├── temp/
│       ├── masters/
│       ├── outputs/
│       └── manifest.json
├── models/
│   └── <model-profile>/<checkpoint>
└── logs/
    ├── web.log
    └── worker.log
```

The application must create these directories at startup. `data/` must be ignored
by Git and must never contain committed model weights or user audio.

Filesystem rules:

- Resolve every path and confirm it remains inside the expected root.
- Use random server-generated IDs for directory names.
- Never use the raw user filename as a path.
- Sanitize names only for display and output filenames.
- Use a unique temporary directory per job.
- Do not expose absolute paths in API responses or logs visible to the user.
- Delete abandoned temporary data through a local cleanup command.

## 6. Repository Layout

```text
/
├── apps/
│   └── web/
│       ├── app/
│       │   ├── page.tsx
│       │   ├── jobs/[jobId]/page.tsx
│       │   └── api/
│       │       ├── uploads/route.ts
│       │       ├── jobs/route.ts
│       │       ├── jobs/[jobId]/route.ts
│       │       ├── jobs/[jobId]/cancel/route.ts
│       │       ├── jobs/[jobId]/retry/route.ts
│       │       └── jobs/[jobId]/downloads/route.ts
│       ├── components/
│       │   ├── upload-dropzone.tsx
│       │   ├── source-picker.tsx
│       │   ├── separation-options.tsx
│       │   ├── job-progress.tsx
│       │   ├── stem-result-list.tsx
│       │   ├── audio-preview.tsx
│       │   └── error-state.tsx
│       ├── hooks/
│       │   └── use-job-polling.ts
│       ├── lib/
│       │   ├── auth/
│       │   ├── db/
│       │   ├── jobs.ts
│       │   ├── local-storage.ts
│       │   ├── job-view.ts
│       │   ├── limits.ts
│       │   └── validation.ts
│       ├── drizzle/
│       └── package.json
├── worker/
│   ├── worker/
│   │   ├── cli.py
│   │   ├── config.py
│   │   ├── database.py
│   │   ├── errors.py
│   │   ├── input_audio.py
│   │   ├── job_loop.py
│   │   ├── pipeline.py
│   │   ├── stages.py
│   │   ├── encoding.py
│   │   ├── packaging.py
│   │   ├── models/
│   │   │   ├── base.py
│   │   │   ├── demucs.py
│   │   │   └── profiles.py
│   │   └── __init__.py
│   ├── tests/
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   ├── pyproject.toml
│   └── README.md
├── packages/
│   └── contracts/
│       ├── schemas/
│       ├── tests/
│       └── README.md
├── data/                         # local runtime data, never committed
├── .env.example
├── start.sh
├── package.json
├── README.md
└── STEM_EXTRACTOR_PLAN.md
```

The exact module names may change, but the separation between the web app, local
worker, and shared contracts should remain.

## 7. User Experience Specification

### 7.1 Home screen

The initial screen must contain:

- Stemify name and concise local-processing description.
- Large drag-and-drop upload control.
- Visible `Choose audio file` action.
- Supported formats, size limit, and duration limit.
- Separation mode control.
- Output format control.
- Primary `Separate audio` button.
- Optional `Import from YouTube` control after upload is working.
- A local privacy notice explaining that files are processed on this computer.

Do not display cloud providers, deployment platforms, internal model names, or
infrastructure details in the normal user flow.

### 7.2 File states

The upload control must support:

- Empty.
- Drag-over.
- Selected.
- Uploading to the local web process.
- Uploaded and ready to process.
- Invalid type.
- Too large.
- Too long.
- Undecodable.
- Upload failure.
- Remove/cancel.

Client validation is for fast feedback only. Server and worker validation remain
authoritative.

### 7.3 Processing screen

After job creation, navigate to `/jobs/{jobId}` and show:

- Source filename.
- Selected mode and output format.
- Current user-facing stage.
- Progress or an indeterminate state when exact progress is unavailable.
- Cancel action.
- Refresh-safe recovery.
- A clear note that processing continues while the page is open or refreshed, as
  long as the local worker process remains running.

Stages shown to users:

1. Preparing audio.
2. Analyzing track.
3. Separating stems.
4. Encoding files.
5. Preparing downloads.

### 7.4 Results screen

Show:

- Completed status.
- Human-readable stem names.
- Native audio preview controls.
- Individual download buttons.
- `Download all` ZIP button.
- Output format.
- Local file location only if explicitly useful and safe; never expose arbitrary
  filesystem paths by default.
- Retry action that creates a new job.
- Cleanup/retention information.

The download route must verify ownership before reading any result file.

### 7.5 Failure screen

Every failure must provide:

- Plain-language explanation.
- Whether retrying may help.
- Retry action when safe.
- Short reference code.
- No raw traceback, absolute path, secret, or model internals.

Useful error categories include invalid audio, unsupported duration, missing model
weights, insufficient memory, FFmpeg failure, canceled job, and worker unavailable.

### 7.6 Accessibility

- Keyboard-accessible file selection and controls.
- Visible focus states.
- Labels associated with every form control.
- Status updates announced through an appropriate live region.
- No color-only status communication.
- Touch-friendly controls.
- Audio controls usable without hover.
- Reduced-motion support.
- Layout space reserved for changing filenames and status text.

## 8. Supported Inputs and Limits

Initial recommended limits:

- Accepted source types: MP3, WAV, FLAC, OGG, M4A.
- Maximum file size: 100 MB initially.
- Maximum duration: 8 minutes for vocals/instrumental.
- Maximum duration: 6 minutes for full stems until benchmarks justify more.
- Channels: mono or stereo input; normalize to the model's expected layout.
- Maximum temporary disk usage: configurable and checked before processing.
- Maximum output budget: configurable based on selected format and duration.

These values are configuration, not scattered constants. The same effective policy
must be used by the client, web server, and worker, with worker validation as the
final authority.

Use environment variables or a validated local config file:

```text
MAX_UPLOAD_BYTES=104857600
MAX_DURATION_SECONDS=480
MAX_FULL_STEMS_DURATION_SECONDS=360
JOB_RETENTION_HOURS=24
MAX_ACTIVE_JOBS=1
```

The local application may allow a larger limit through configuration, but the UI
must display the active limits and the worker must enforce them.

## 9. Local End-to-End Flows

### 9.1 Startup flow

1. User runs `./start.sh` from the repository root.
2. The script verifies Bash, Node/npm, Python, FFmpeg, and FFprobe.
3. The script verifies that web dependencies are installed.
4. The script verifies that Python runtime dependencies are installed.
5. The script creates the local data directories.
6. The script runs a database migration or confirms the SQLite schema is current.
7. The script checks that the configured model profile and checkpoint are available,
   or prints exact instructions to prepare them.
8. The script starts the Next.js process.
9. The script starts the Python worker process.
10. The script prints the local URL and process/log locations.
11. A trap terminates both child processes when the script exits.

`start.sh` must not silently install packages or modify global environments. If a
prerequisite is missing, it should stop and explain how to install or activate it.

### 9.2 Upload flow

1. Browser selects an audio file.
2. Browser performs extension, MIME-hint, and size checks.
3. Browser sends the file to `POST /api/uploads` as multipart form data.
4. The web server enforces request size and filename limits.
5. The server generates an upload ID and writes bytes to a server-owned upload
   directory.
6. The server returns upload metadata, never an absolute path.
7. The browser selects mode and output format.
8. Browser sends `POST /api/jobs` with the upload ID and options.
9. The server verifies the upload belongs to the local session and still exists.
10. The server creates one queued SQLite job using the idempotency key.
11. The worker notices the queued job and claims it atomically.
12. The browser navigates to the job page and polls local status.

Unlike the old cloud plan, the browser does not receive a presigned storage URL and
large audio does not leave the local machine through a third-party service.

### 9.3 Worker flow

1. Worker claims a queued job.
2. Worker records `processing` and `starting`.
3. Worker creates a unique job temp directory.
4. Worker retrieves the local uploaded source.
5. Worker runs `ffprobe` and validates actual media properties.
6. Worker decodes to the canonical model representation.
7. Worker loads or reuses the configured Demucs model.
8. Worker performs chunked or library-supported inference.
9. Worker validates stem shapes, values, timing, and peaks.
10. Worker writes temporary master files.
11. Worker encodes the requested output format.
12. Worker validates every encoded output with `ffprobe`.
13. Worker writes a manifest and ZIP archive.
14. Worker moves only complete artifacts into the job output directory.
15. Worker updates SQLite with output metadata and marks the job completed.
16. Worker removes temporary files in a `finally` path.

If a failure occurs, the worker records a stable error code, removes partial output,
marks the job failed or canceled, and continues processing future jobs.

### 9.4 Polling flow

1. Browser calls `GET /api/jobs/{jobId}` every 2–3 seconds while active.
2. The server verifies the local session owner.
3. The server returns sanitized status and progress.
4. The browser stops polling at `completed`, `failed`, `canceled`, `expired`, or
   `worker_unavailable`.
5. The browser backs off after repeated unchanged responses.
6. The browser pauses polling while hidden and resumes when visible.
7. Refreshing the job URL recovers the current state from SQLite.

### 9.5 Cancellation flow

1. Browser sends `POST /api/jobs/{jobId}/cancel`.
2. The server verifies ownership.
3. A queued job is marked canceled immediately.
4. A processing job receives a cancellation request in SQLite.
5. The worker checks cancellation between stages and inference chunks.
6. The worker stops at the next safe boundary.
7. Partial outputs are removed.
8. The final state is `canceled`, never `completed`.

### 9.6 Optional YouTube flow

YouTube support is local but not offline:

1. User opens the secondary YouTube input.
2. The web server parses the URL with a real URL parser.
3. Only approved YouTube hostnames are accepted.
4. A queued job stores the validated source metadata.
5. The local worker downloads the source with controlled `yt-dlp` arguments.
6. The worker applies download timeout, size, duration, and media validation.
7. The worker continues through the same local Demucs pipeline.

Do not accept arbitrary downloader options or arbitrary remote URLs. Do not bypass
DRM, private access controls, age gates, or other restrictions. Keep this feature
disabled until the local upload flow is stable and policy review is complete.

## 10. Local API Contract

All API responses use JSON except authorized download responses. Errors use one
consistent shape:

```json
{
  "error": "invalid_request",
  "message": "The selected file is not supported.",
  "reference": "ref_..."
}
```

The `message` is optional and must never contain secrets, stack traces, or absolute
paths.

### 10.1 `POST /api/uploads`

Accept a multipart form upload named `file`.

Response:

```json
{
  "uploadId": "upl_...",
  "filename": "song.mp3",
  "sizeBytes": 7340032,
  "status": "uploaded"
}
```

Rules:

- Enforce request size before writing unbounded data.
- Validate the filename length and extension.
- Treat MIME type as a hint only.
- Generate the server-owned upload ID.
- Write only under `data/uploads/{uploadId}/`.
- Do not return the local filesystem path.
- Bind the upload to the signed local session.
- Delete abandoned uploads during cleanup.

### 10.2 `POST /api/jobs`

Request:

```json
{
  "source": {
    "type": "upload",
    "uploadId": "upl_...",
    "filename": "song.mp3"
  },
  "mode": "vocals_instrumental",
  "outputFormat": "mp3",
  "idempotencyKey": "client-generated-random-value"
}
```

The server validates mode and format allowlists, upload ownership, source
existence, active-job limits, and idempotency. It creates a queued SQLite record.

Response:

```json
{
  "jobId": "job_...",
  "status": "queued",
  "statusUrl": "/api/jobs/job_..."
}
```

The route must not wait for Demucs inference.

### 10.3 `GET /api/jobs/{jobId}`

Running response:

```json
{
  "jobId": "job_...",
  "status": "processing",
  "stage": "separating",
  "userStage": "Separating stems",
  "progress": 54,
  "source": { "filename": "song.mp3" },
  "mode": "vocals_instrumental",
  "outputFormat": "mp3",
  "createdAt": "2026-09-06T00:00:00.000Z",
  "updatedAt": "2026-09-06T00:01:12.000Z"
}
```

Completed response adds:

```json
{
  "stems": [
    { "id": "vocals", "label": "Vocals", "durationSeconds": 214.2 },
    { "id": "instrumental", "label": "Instrumental", "durationSeconds": 214.2 }
  ],
  "downloadUrl": "/api/jobs/job_.../downloads?kind=zip",
  "expiresAt": "2026-09-07T00:00:00.000Z"
}
```

Failed response adds a stable `errorCode` and sanitized public error message.
Never include object keys, absolute paths, worker process IDs, or tracebacks.

### 10.4 `POST /api/jobs/{jobId}/cancel`

Request cancellation as described in the cancellation flow. The endpoint is
idempotent for already canceled jobs and rejects cancellation of completed or
expired jobs with a clear response.

### 10.5 `POST /api/jobs/{jobId}/retry`

Create a new job from the same source and options after verifying ownership and
that the source is still available. Do not mutate the completed or failed job into
a new attempt; each retry receives a new job ID and idempotency key.

### 10.6 `GET /api/jobs/{jobId}/downloads`

Query parameters:

- `kind=zip`
- `kind=stem&stem=vocals`

The endpoint verifies session ownership, terminal state, output existence, and
expiry before streaming the file or returning a local redirect. It must reject
path-like user input and resolve files from stored output metadata rather than
constructing paths directly from a request string.

## 11. Local Database Model

Use migrations from the beginning. The initial SQLite schema should contain:

### 11.1 `jobs`

- `id`: random public job ID.
- `owner_key`: hash or signed-session owner identifier.
- `source_type`: `upload` or `youtube`.
- `source_filename`: sanitized display name.
- `source_path`: server-owned relative path, never an absolute path.
- `source_url`: optional YouTube metadata, only when enabled.
- `source_duration_seconds`.
- `source_size_bytes`.
- `source_sha256`.
- `mode`.
- `output_format`.
- `status`.
- `stage`.
- `progress`.
- `cancel_requested`.
- `attempt_count`.
- `error_code`.
- `error_message_public`.
- `diagnostic_reference`.
- `created_at`.
- `started_at`.
- `completed_at`.
- `expires_at`.
- `updated_at`.

Recommended statuses:

```text
queued
processing
completed
failed
canceled
expired
```

### 11.2 `job_outputs`

- `id`.
- `job_id`.
- `stem_key`.
- `label`.
- `relative_path`.
- `mime_type`.
- `size_bytes`.
- `duration_seconds`.
- `sha256`.
- `created_at`.
- `expires_at`.

Store the ZIP as a job output with a reserved kind, or add a dedicated `job_archives`
table if that is clearer. The web API must use stored metadata for file lookup.

### 11.3 `job_events`

A local audit table is optional but recommended:

- `id`.
- `job_id`.
- `event_type`.
- `stage`.
- `progress`.
- `payload_hash` or sanitized detail.
- `created_at`.

This is a local replacement for remote worker callback event storage. It is not a
webhook receiver and must not contain raw audio or secrets.

### 11.4 Constraints and indexes

Add:

- Unique owner + idempotency key hash.
- Index on status and created time.
- Index on expiry time.
- Foreign keys from outputs and events to jobs.
- Checks for progress between 0 and 100.
- Application-level transition validation.

Use an atomic claim operation so two worker loops cannot process the same job.

## 12. Worker Implementation

### 12.1 Worker process

The worker must be runnable directly:

```bash
python -m worker.job_loop
```

It must also support a one-shot command for debugging:

```bash
python -m worker.cli separate \
  --input ./fixtures/song.mp3 \
  --mode vocals_instrumental \
  --format mp3 \
  --output ./artifacts
```

The long-running loop should:

1. Open the local database.
2. Confirm model/configuration readiness.
3. Find the oldest queued job.
4. Atomically change it to processing.
5. Run exactly one pipeline attempt.
6. Persist progress and terminal state.
7. Sleep briefly when no job exists.
8. Handle SIGINT/SIGTERM by stopping after a safe boundary.

A local health command should report whether FFmpeg, SQLite, Python packages, and
model weights are usable.

### 12.2 Demucs model integration

Use a pinned Demucs-compatible dependency and an explicit model profile. The first
profile should be selected based on local quality, runtime, memory, and license
checks.

The adapter contract is:

```text
validate_profile(profile)
load_model(profile, device)
separate(input_waveform, profile, progress_callback, cancellation_checker)
model_metadata(profile)
```

The rest of the pipeline must not depend on Demucs tensor details.

Recommended profile data:

```text
profile_id
model_id
model_revision
checkpoint_identifier
checkpoint_checksum
expected_stems
sample_rate
channels
device_policy
chunk_length
overlap
batch_size
precision_policy
license_reference
```

Do not silently download arbitrary model weights. A model checkpoint must be
allowlisted and checksum-verified. Cache it under the local models directory.

### 12.3 Device selection

Support explicit device selection:

```text
STEMIFY_DEVICE=auto
STEMIFY_DEVICE=cpu
STEMIFY_DEVICE=cuda
```

`auto` should select CUDA when a validated CUDA/PyTorch environment is available,
otherwise CPU. Startup must clearly report the selected device.

Do not claim CPU performance is suitable for maximum-length tracks until measured.
A CPU mode is useful for short fixtures and development.

### 12.4 Audio preprocessing

The worker must:

1. Resolve the source path inside its expected local root.
2. Check file size before invoking tools.
3. Run `ffprobe` with structured JSON output.
4. Confirm an audio stream exists.
5. Reject video-only, image-only, subtitle-only, empty, and corrupt inputs.
6. Enforce duration, channel, and sample-rate policy.
7. Decode using FFmpeg argument arrays, never shell interpolation.
8. Convert to the model's required sample rate and channel layout.
9. Preserve source duration and timing metadata.
10. Calculate an input checksum for diagnostics.

Use a unique per-job temporary directory and clean it in `finally` handling.

### 12.5 Inference

Prefer the selected Demucs library's maintained inference path, including its
recommended chunking and overlap behavior. Do not implement custom stitching until
there is a measured need.

Inference requirements:

- Evaluation mode.
- Inference-only execution.
- Float32 baseline before mixed precision.
- Bounded memory use.
- Shape and sample-count validation for every stem.
- NaN, infinity, and unexpected clipping detection.
- Progress updates at meaningful chunk/stage boundaries.
- Cancellation checks before and after each chunk.
- Per-job tensor cleanup after success and failure.

For two-source mode, the adapter must document whether instrumental is a direct
model output or mixture minus vocals. Use one fixed policy per profile.

### 12.6 Output encoding

Encode from validated high-quality temporary WAV masters:

```text
model output
  -> validated WAV masters
  -> selected encoder
  -> ffprobe validation
  -> checksum
  -> final output files
```

Initial format policy:

```text
mp3 -> LAME, documented high-quality bitrate
wav  -> PCM, documented bit depth and sample rate
flac -> lossless compression
ogg  -> documented Vorbis or Opus policy
m4a  -> AAC in an M4A container, documented bitrate
```

Keep all encoder commands in one module. Use subprocess argument arrays and capture
stderr only for private diagnostics.

### 12.7 Packaging

Create:

- One output file per stem.
- One ZIP containing the selected-format stems.
- One `manifest.json` inside the ZIP.

The manifest must contain:

- Schema version.
- Job ID.
- Sanitized source display name.
- Source duration, sample rate, and channels.
- Separation mode.
- Model ID and revision.
- Selected format.
- Stem names, durations, and checksums.
- Mixture-consistency setting if applicable.

Never include absolute paths, session tokens, local secrets, raw source URLs, or
internal diagnostic details in the manifest.

Only move artifacts from a temporary directory to the final output directory after
all validation succeeds. A partial output must never make a job look completed.

## 13. Progress and State Management

### 13.1 Stage mapping

Internal stages:

```text
starting
validating
audio_preparation
separating
encoding
packaging
cleanup
completed
```

YouTube jobs may include `downloading` before validation.

Suggested progress mapping:

```text
queued: 0
starting: 5
validating: 15
audio_preparation: 25
separating: 30-75
encoding: 75-90
packaging: 90-98
completed: 100
```

If reliable chunk progress is unavailable, show stage progress or an indeterminate
indicator instead of pretending to know an exact percentage.

### 13.2 State transitions

```text
queued
  -> processing
  -> completed
  -> expired

queued
  -> canceled

processing
  -> completed
  -> failed
  -> canceled
  -> expired
```

No terminal state may move backward. Repeated writes for the current state must be
safe. The worker must recover jobs left in `processing` after an abnormal shutdown:

- On startup, find stale processing jobs.
- Determine whether their process heartbeat is still valid.
- Mark stale jobs failed with a worker-restarted code or requeue them once.
- Never leave an indefinite spinner.

### 13.3 Worker coordination

Use SQLite as the queue. The simplest acceptable design is a polling worker with a
short interval. An optional local notification mechanism may reduce polling, but it
must not be required for correctness.

The web app creates jobs; the worker claims them. The web app must not spawn a new
Demucs process for every browser request.

## 14. Security and Privacy

### 14.1 Local network security

- Bind the web app to localhost by default.
- Bind any worker HTTP endpoint to localhost only.
- Do not expose the app to a LAN without an explicit opt-in.
- Keep all secrets in environment variables or ignored local files.
- Use secure HTTP-only cookies where applicable.
- Add CSRF protection for cookie-authenticated mutations if the app is ever exposed
  beyond localhost.
- Limit JSON and multipart request sizes.
- Do not log audio contents, credentials, signed values, or unnecessary URLs.

### 14.2 Filesystem security

- Use server-generated IDs for all storage directories.
- Resolve and validate every path against its expected root.
- Strip path separators and control characters from display names.
- Never execute uploaded files.
- Use subprocess argument arrays.
- Do not construct shell commands using filenames or URLs.
- Keep runtime data outside source-controlled directories when possible.
- Do not expose absolute local paths in browser responses.

### 14.3 Model and dependency security

- Pin Python, PyTorch, Demucs-compatible libraries, and FFmpeg expectations.
- Verify model checkpoint checksums.
- Keep model weights outside Git.
- Record model and dependency versions in diagnostics and manifests where safe.
- Review model and weight licenses before redistribution or commercial use.

### 14.4 Privacy

The local UI should clearly state:

- Files are processed on this computer.
- Files are stored under the configured local data directory.
- Generated files remain until the local retention cleanup removes them or the user
  deletes them.
- Optional YouTube importing requires network access.
- The application does not upload files to a remote processing service in the local
  mode.

Provide a local cleanup command or UI action that removes source files, outputs,
and job metadata according to the configured policy.

## 15. Environment and Configuration

Use `.env.example` with local-only values:

```text
# Local paths
STEMIFY_DATA_DIR=./data
STEMIFY_LOG_DIR=./data/logs

# Local web server
PORT=3000
HOSTNAME=127.0.0.1

# Local processing
STEMIFY_DEVICE=auto
STEMIFY_MODEL_PROFILE=demucs_default
STEMIFY_MODEL_DIR=./data/models
STEMIFY_WORKER_POLL_MS=500
STEMIFY_WORKER_MAX_JOBS=1

# Product limits
MAX_UPLOAD_BYTES=104857600
MAX_DURATION_SECONDS=480
MAX_FULL_STEMS_DURATION_SECONDS=360
JOB_RETENTION_HOURS=24
MAX_ACTIVE_JOBS=1

# Local session signing
JOB_ACCESS_TOKEN_SECRET=replace-with-a-long-random-local-value
```

No cloud provider credentials, database URLs, worker launch URLs, callback secrets,
or storage access keys belong in the local configuration.

If a value is required, startup should validate it and explain the problem. Never
use an empty production-style secret silently.

## 16. `start.sh` Requirements

`start.sh` is the primary entrypoint and must be treated as part of the product.

### 16.1 Startup checks

It must verify:

- It is being run from or can locate the repository root.
- `node` and `npm` exist.
- Python 3.12 or the supported local version exists.
- `ffmpeg` and `ffprobe` exist on PATH or in the documented local binary path.
- `node_modules` exists and includes the web dependencies.
- The Python environment can import the worker runtime dependencies.
- The configured Demucs model profile is valid.
- The model checkpoint is available or the setup message explains how to prepare it.
- The configured data directory is writable.
- The web port is available.

Checks must fail with concise, actionable instructions. Do not automatically install
packages, modify global Python environments, or download model weights without an
explicit setup command.

### 16.2 Process management

The script must:

1. Create local runtime directories.
2. Run or verify local database migrations.
3. Start the worker in the background.
4. Start the Next.js development server.
5. Print the URL and log paths.
6. Trap `INT`, `TERM`, and normal exit.
7. Terminate the worker and web child processes on shutdown.
8. Return a failure exit code if either required process exits unexpectedly.

Use portable POSIX shell syntax compatible with the repository's supported shell
environment. Keep process IDs private to the script and logs.

### 16.3 Developer commands

These commands should remain available in addition to `start.sh`:

```bash
./start.sh
npm run dev -w apps/web
python -m worker.job_loop
python -m worker.cli health
python -m worker.cli separate --input ./fixtures/song.mp3 --mode vocals_instrumental --format mp3 --output ./artifacts
python -m worker.cli cleanup
```

`start.sh` is the normal path. The separate commands are for diagnosis and tests.

## 17. Testing Strategy

### 17.1 Web tests

Cover:

- Upload request validation.
- Filename and size policy.
- Mode and output-format validation.
- Guest/session ownership.
- Job idempotency.
- Local path authorization.
- Job state transition mapping.
- Public error mapping.
- Download authorization.
- Retry and cancellation behavior.

### 17.2 Worker unit tests

Cover:

- `ffprobe` parsing.
- Size, duration, channel, and codec rejection.
- Source path containment.
- Temporary directory cleanup.
- Model profile allowlisting.
- Device selection.
- Demucs adapter output mapping.
- Chunk planning and sample-count preservation.
- Encoder command construction.
- Output validation.
- ZIP and manifest construction.
- Cancellation and deadline checks.
- Stable error classification.

### 17.3 Local integration tests

Use a temporary data directory and SQLite database. Do not require a cloud service.

Verify:

1. A fixture uploads through the web API.
2. A job is created.
3. The worker claims the job.
4. Progress is persisted.
5. A legal fixture produces expected stems.
6. Encoded results decode successfully.
7. The ZIP contains only expected files and a manifest.
8. The result page can preview and download outputs.
9. Cancellation removes partial output.
10. A worker restart repairs stale jobs.
11. Unauthorized sessions cannot read another job's outputs.

Use a fake or stub model for fast control-plane tests and a short real Demucs
fixture for scheduled/local engine tests.

### 17.4 Startup smoke test

On a clean local setup:

1. Run `./start.sh`.
2. Confirm both processes start.
3. Open `http://localhost:3000`.
4. Upload a short legal fixture.
5. Create a vocals/instrumental MP3 job.
6. Observe progress.
7. Preview both stems.
8. Download one stem and the ZIP.
9. Stop the script.
10. Confirm both processes stop.
11. Start again and confirm the job metadata/results remain readable.

### 17.5 Accessibility tests

Verify keyboard-only upload, focus recovery, screen-reader status announcements,
error announcements, reduced motion, mobile viewport behavior, and audio control
accessibility.

## 18. Model Evaluation and Performance

Before enabling a profile by default:

1. Select legally usable local fixtures across representative genres.
2. Include dense mixes, sparse mixes, stereo-width variation, backing vocals, and
   percussion-heavy material.
3. Run the same preprocessing and output settings for every candidate profile.
4. Evaluate vocal bleed, musical artifacts, transient damage, and residuals.
5. Measure CPU time, GPU time, peak RAM, peak VRAM, and disk use.
6. Check model and weight licenses.
7. Pin the selected profile and checkpoint checksum.
8. Record results in local documentation.

Benchmark at least:

- 30-second fixture.
- 3-minute fixture.
- Maximum supported fixture.
- Mono and stereo input.
- MP3 and lossless input.
- CPU mode.
- CUDA mode when available.
- Cold model load.
- Warm model run.
- Every enabled output format.

Use measurements to set duration limits and progress expectations. Do not enable
full-stem mode if it is not reliable on the supported local hardware profile.

## 19. Observability and Operations

Observability is local and diagnostic rather than cloud-based.

### 19.1 Logs

Write structured or consistently formatted logs to `data/logs/` with:

- Event.
- Public job ID.
- Stage.
- Status.
- Duration.
- Device.
- Model profile and revision.
- Error code.
- Diagnostic reference.

Never log raw audio, credentials, session cookies, absolute paths, or full private
source URLs unless explicitly needed for local debugging.

### 19.2 Health and diagnostics

`python -m worker.cli health` should report:

- Python version.
- FFmpeg/FFprobe availability and versions.
- PyTorch version.
- Demucs package availability.
- CUDA availability and device name.
- Model profile readiness and checkpoint checksum.
- Data-directory writability.
- SQLite connectivity.

The web app may show a non-sensitive worker-unavailable state when the worker is
not running.

### 19.3 Cleanup

Provide an idempotent local cleanup operation that:

1. Finds expired jobs and abandoned uploads.
2. Deletes source files and result files.
3. Removes stale temporary directories.
4. Marks metadata expired or deleted.
5. Reports failures in local logs.

Do not delete model checkpoints as part of ordinary job cleanup.

## 20. Implementation Task Breakdown

The repository currently has Tasks 1–8 substantially represented, with Task 8 as
the latest completed worker foundation. **Task 9 is the next major implementation
step.** The following task list replaces the old cloud deployment sequence.

### Phase 1: Local foundation

#### Task 1: Local repository and startup foundation

Work:

- Keep the npm workspace and Python worker.
- Update root README and `.env.example` for local-only operation.
- Rewrite `start.sh` to validate prerequisites and launch web plus worker.
- Add local data-directory initialization.
- Remove cloud deployment assumptions from setup documentation.

Acceptance:

- `./start.sh` starts both required processes after prerequisites are present.
- Missing prerequisites produce actionable errors.
- Stopping the script stops both processes.

#### Task 2: Define shared contracts

Work:

- Keep JSON schemas for job requests, status, and manifests.
- Remove remote callback-specific requirements from the primary local flow.
- Add local worker/job state fields where needed.
- Keep web and worker enum parity tests.

Acceptance:

- Web and worker agree on status, stage, mode, format, stem, and error values.
- Invalid requests and manifests are rejected.

#### Task 3: Replace hosted database assumptions with local SQLite

Work:

- Use SQLite and local migrations.
- Configure WAL mode, foreign keys, busy timeout, and short transactions.
- Add jobs, outputs, and optional local job-events tables.
- Add atomic queued-job claiming.
- Remove the required `DATABASE_URL` dependency.

Acceptance:

- A clean local data directory initializes without an external database.
- Web and worker can safely read and update the same database.
- A concurrent claim cannot process one job twice.

### Phase 2: Local storage and web flow

#### Task 4: Implement local filesystem storage

Work:

- Add server-owned upload, job, output, model, and log directories.
- Add path containment helpers.
- Replace presigned upload assumptions with a local multipart upload route.
- Add local authorized file streaming.
- Add cleanup helpers.

Acceptance:

- Files remain on the local filesystem.
- No absolute paths reach the browser.
- Path traversal and unauthorized output access are rejected.

#### Task 5: Complete the upload UI

Work:

- Connect the dropzone to the local upload route.
- Start job creation after upload completion.
- Add real separation mode state.
- Add output format state.
- Add upload progress, retry, remove, and error behavior.

Acceptance:

- A valid local file can be uploaded and turned into a queued job.
- Invalid files show useful errors.
- The UI navigates to the job page after creation.

#### Task 6: Implement local job creation and queueing

Work:

- Validate source ownership and metadata.
- Add idempotency handling with a database-safe insert strategy.
- Enforce active-job limits.
- Create queued jobs without waiting for inference.
- Add cancellation and retry request handling.

Acceptance:

- Repeated requests do not create duplicate jobs.
- A queued job is visible to the worker.
- Jobs cannot be created from arbitrary local paths.

#### Task 7: Complete status, progress, and result pages

Work:

- Keep refresh-safe polling.
- Align API responses with the shared status schema.
- Add cancellation UI.
- Add result previews and download controls once outputs exist.
- Map worker errors to safe user messages.

Acceptance:

- The page reflects SQLite state changes.
- Polling stops for every terminal state.
- Failure and cancellation states are understandable.

### Phase 3: Real local separation engine

#### Task 8: Complete local audio validation pipeline

Status: substantially complete in the current repository.

Keep and finish:

- FFmpeg/FFprobe discovery.
- Actual media validation.
- Size, duration, channel, and extension checks.
- Canonical decoding.
- Unique temp directories.
- Cleanup on success and failure.

Acceptance:

- Valid fixtures pass.
- Corrupt, empty, oversized, over-duration, and unsupported files fail with stable
  codes.

#### Task 9: Integrate and validate Demucs

This is the next major task.

Work:

- Add pinned runtime dependencies and a supported Python environment.
- Add a Demucs adapter behind the separation interface.
- Add a validated default model profile.
- Add local model cache and checksum verification.
- Add CPU and CUDA device selection.
- Run a short legal fixture through real inference.
- Record model, license, quality, memory, and timing results.

Acceptance:

- A supported input produces correctly named stems.
- The model profile is allowlisted and reproducible.
- Warm model reuse works within the worker process.
- Full-stem mode is only enabled if it passes benchmarks.

#### Task 10: Implement encoding and packaging

Work:

- Implement MP3, WAV, FLAC, OGG, and M4A encoders.
- Validate encoded output with FFprobe.
- Create individual stem artifacts.
- Create ZIP archives and schema-valid manifests.
- Remove partial artifacts on failures.

Acceptance:

- Every enabled format decodes successfully.
- Stem durations remain aligned.
- ZIP contents are deterministic and contain only expected files.

#### Task 11: Implement the local worker loop

Work:

- Add SQLite queue polling and atomic job claiming.
- Connect validation, Demucs, encoding, and packaging into one pipeline.
- Persist progress and terminal state.
- Add cancellation checks.
- Recover stale jobs after worker restart.
- Add direct CLI health and one-shot processing commands.

Acceptance:

- The worker processes a queued job without a remote service.
- Progress appears on the web page.
- A failed job does not remain indefinitely processing.
- `python -m worker.job_loop` can run independently.

### Phase 4: Results and hardening

#### Task 12: Build local previews and downloads

Work:

- Add authorized individual stem streaming.
- Add authorized ZIP download.
- Add native audio previews.
- Add expiry and missing-file handling.
- Add cleanup of partial outputs.

Acceptance:

- Users can preview and download every completed stem.
- Unauthorized sessions cannot read outputs.
- Missing files result in a clear expired/unavailable state.

#### Task 13: Add local cleanup and limits

Work:

- Add active-job limits.
- Add abandoned upload cleanup.
- Add result retention cleanup.
- Add stale-job recovery.
- Add local worker unavailable messaging.
- Add a manual cleanup command.

Acceptance:

- The local data directory does not grow without bounds during normal use.
- Cleanup is retryable and safe to run repeatedly.

#### Task 14: Add optional YouTube input

Work:

- Add secondary URL input.
- Validate approved hostnames.
- Add controlled local `yt-dlp` download.
- Apply strict size, duration, timeout, and media checks.
- Add policy acknowledgement.

Acceptance:

- Valid supported URLs work when network access is available.
- Unsupported or restricted sources fail safely.
- The upload flow remains the default and does not depend on YouTube.

### Phase 5: Verification and local release

#### Task 15: Add local integration and startup tests

Work:

- Add temporary-data integration tests.
- Add fake-model control-plane tests.
- Add short real-engine fixture tests.
- Add startup smoke tests.
- Add authorization, cancellation, restart, and cleanup tests.

Acceptance:

- A clean local setup passes the complete upload-to-download flow.
- Tests do not require cloud credentials or paid infrastructure.

#### Task 16: Run performance and model benchmarks

Work:

- Measure CPU and CUDA processing.
- Measure cold and warm model runs.
- Measure all enabled formats.
- Record RAM, VRAM, disk, and total time.
- Set supported duration limits from evidence.

Acceptance:

- Local performance expectations are documented.
- Default model and device policy are reproducible.
- Unsupported hardware receives a clear setup or limit message.

#### Task 17: Complete local security and privacy review

Work:

- Review path containment and upload handling.
- Review cookie/session authorization.
- Review localhost binding.
- Review subprocess arguments and URL handling.
- Review logs and runtime data exclusions.
- Confirm model and dependency licenses.
- Document local data deletion behavior.

Acceptance:

- No known critical local file disclosure or command-injection issue remains.
- Runtime data and model weights are not committed.
- Privacy and content-policy behavior is documented.

## 21. Checkpoints

### Checkpoint A: Local foundation

- Repository installs without cloud credentials.
- Web lint and typecheck pass.
- Worker tests and lint pass.
- SQLite initializes locally.
- `start.sh` validates and starts both processes.

### Checkpoint B: Local control-plane vertical slice

- A local upload creates a queued job.
- The worker can claim a job.
- Polling shows persisted progress.
- Cancellation and restart behavior work.
- Fake or stub processing reaches a result page.

### Checkpoint C: Real Demucs vertical slice

- A legal fixture runs through Demucs locally.
- Vocals and instrumental outputs are produced.
- Output files decode and have aligned durations.
- Results are visible through the local UI.
- CPU/CUDA behavior and model metadata are recorded.

### Checkpoint D: Local MVP

- All enabled output formats work.
- Full-stem mode is enabled only if benchmarked.
- Previews and ZIP downloads work.
- Cleanup and stale-job recovery work.
- Access control and path traversal tests pass.
- `start.sh` is the documented normal launch path.

### Checkpoint E: Optional feature completion

- YouTube mode passes URL, media, timeout, and policy checks.
- Local performance limits are documented.
- Accessibility and startup smoke tests pass.
- The application remains usable without YouTube or cloud services.

## 22. Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Demucs is too slow on local CPU | High | Prefer CUDA when available, benchmark early, keep duration limits conservative |
| GPU/PyTorch environment is difficult to install | High | `start.sh` verifies prerequisites and prints exact setup guidance; provide CPU fixtures |
| Model weights are missing | High | Health check, explicit cache path, checksum verification, clear preparation command |
| Local disk fills with audio/results | High | Size limits, retention cleanup, cleanup command, disk-budget checks |
| Worker crashes during inference | High | Stale-job recovery, structured error state, unique temp directories |
| Web and worker race for SQLite jobs | High | WAL mode, busy timeout, atomic claim transaction, short transactions |
| User accesses another local job URL | Medium | Signed session ownership and authorization on status/download routes |
| Path traversal through filenames or download parameters | High | Server-owned IDs, containment checks, stored output metadata, sanitized names |
| FFmpeg or codec differences across machines | Medium | Health check, pinned/documented expectations, output FFprobe validation |
| Model quality varies by genre | High | Legal fixture suite, listening review, conservative mode enablement |
| Optional YouTube behavior changes | Medium | Keep upload primary, pin/update downloader deliberately, fail safely |
| User expects hosted access | Medium | Clearly label local-only behavior and local data retention in the UI |
| Localhost is exposed unintentionally | Medium | Bind to `127.0.0.1` by default and document any LAN opt-in separately |

## 23. Definition of Done for the Local Core Product

The local core product is done when:

- A user can run `./start.sh` after installing documented prerequisites.
- The script starts both the Next.js web app and the Python worker.
- Missing prerequisites produce actionable errors instead of mysterious failures.
- The home page immediately presents local audio upload.
- A valid source is stored locally and never requires cloud storage.
- The user can choose a validated separation mode and one output format.
- A job is created exactly once for a repeated request.
- The Python worker claims and processes the job locally with Demucs.
- Progress is persisted and survives browser refresh.
- Every generated stem has valid timing and decodes successfully.
- The user can preview individual stems and download a ZIP.
- Cancellation and worker restart do not create misleading completed jobs.
- Failures are understandable and do not expose tracebacks or local secrets.
- Local filesystem paths and job outputs are authorized.
- Temporary files and expired results can be cleaned up.
- Model versions, dependencies, and processing settings are recorded.
- Performance limits are based on measurements for supported hardware.
- The entire core workflow works without Vercel, Modal, Supabase, Neon, R2, S3,
  Postgres, webhooks, or hosted infrastructure.

## 24. Recommended Build Order From the Current Repository

The repository is currently strongest around shared contracts, database metadata,
storage abstractions, UI scaffolding, and Task 8 audio validation. The fastest path
to the new goal is:

1. Replace the hosted Postgres assumptions with local SQLite.
2. Replace presigned/object-storage upload with local multipart upload and filesystem
   storage.
3. Finish the home-page state flow so upload completion creates a job and navigates
   to the job page.
4. Implement the SQLite worker queue and a fake processing path.
5. Implement the local `start.sh` prerequisite checks and process management.
6. Implement Task 9: integrate and benchmark Demucs.
7. Implement encoding, packaging, previews, and downloads.
8. Add cancellation, stale-job recovery, cleanup, and local authorization.
9. Enable full-stem mode only after benchmark approval.
10. Add YouTube last, as an optional local feature.

The immediate engineering milestone is therefore:

> **Local Checkpoint B: upload → SQLite job → local worker claim → persisted status**

The next feature milestone after that is:

> **Task 9: real Demucs inference in the local worker**

## 25. Current Repository Status

At the time this plan was changed:

- Task 1 is partially complete.
- Task 2 is substantially complete.
- Task 3 is partially complete but still assumes hosted Postgres and needs the local
  SQLite conversion.
- Task 4 is partially complete but still assumes cloud-style storage APIs and needs
  the local filesystem implementation.
- Task 5 is partial; the upload UI is not fully connected to job creation.
- Task 6 is partial; queued jobs are created but are not processed.
- Task 7 is partial; polling/page scaffolding exists but no worker updates jobs.
- Task 8 is substantially complete and tested.
- **Task 9 is the next major task: Demucs model integration.**
- Tasks 10–17 remain to be implemented or adapted for local execution.

The old deployment-specific tasks are intentionally removed rather than deferred:
there is no Modal integration phase, no hosted-storage phase, no production deploy
phase, and no cloud callback phase in the local plan.
