# Stem Separator Product and Implementation Plan

## 1. Product Summary

Build a public web product that lets a user visit one website, upload an MP3, choose a separation mode and output format, wait while the track is processed, preview the resulting stems, and download them.

The primary experience must feel like this:

1. Open the website.
2. Drop an MP3 onto the upload area or choose a file.
3. Choose a simple separation mode and output format.
4. Click `Separate audio`.
5. Watch clear progress.
6. Play or download the generated stems.

The user must not need to know that the system uses Vercel, Modal, Python, PyTorch, GPU workers, object storage, or a database. Those are implementation details.

The original YouTube-link requirement remains supported, but it is a secondary input mode. File upload is the first and most reliable launch workflow.

## 2. Product Name and Positioning

Working name: `Olamzkid Stem Separator`.

The name, domain, logo, and final copy can change later. The implementation should not hard-code a brand name into the architecture.

Positioning:

> Fast, simple music stem separation in your browser.

Avoid promising perfect separation or an absolute five-minute guarantee. The correct product promise is:

> Supported tracks are normally processed within five minutes.

The service must enforce input limits and capacity controls so that it can make this promise honestly.

## 3. Goals

### 3.1 Launch goals

- Make the upload-and-process workflow usable without technical knowledge.
- Provide high-quality vocal/instrumental separation as the default mode.
- Provide a multi-stem mode when the selected model meets the quality and latency bar.
- Support MP3, WAV, FLAC, OGG, and M4A output choices.
- Run the web application on Vercel.
- Run the audio-processing worker independently on Modal GPU infrastructure.
- Keep long-running processing asynchronous so the web request never waits for GPU inference.
- Provide progress, useful errors, retry behavior, and secure downloads.
- Keep generated files private and automatically delete them after a retention period.
- Make the system observable enough to diagnose failed or slow jobs.

### 3.2 Quality goals

- Pin model versions and dependencies for reproducible output.
- Benchmark candidate models before selecting the production default.
- Preserve timing and channel layout correctly.
- Avoid audible clicks, discontinuities, clipping, and truncated output.
- Keep the original input filename available in the result metadata without trusting it as a filesystem path.

### 3.3 Product goals after MVP

- Add user accounts and persistent job history.
- Add paid credits or subscriptions.
- Add a public API.
- Add additional separation models and modes.
- Add batch processing.
- Add optional on-demand conversion to other formats.

## 4. Non-Goals for the First Launch

The first public version should not attempt to do all of the following:

- Run the GPU model inside Vercel.
- Support arbitrary remote URLs beyond explicitly supported YouTube hosts.
- Process full albums, playlists, livestreams, or very long recordings.
- Offer unlimited anonymous usage.
- Guarantee studio-quality stems for every genre or source.
- Train a new separation model from scratch.
- Build a custom audio waveform editor.
- Build a DAW, mixer, mastering tool, or full music-production suite.
- Store user files permanently by default.
- Generate every output format automatically when one selected format is sufficient.
- Bypass private, DRM-protected, age-restricted, or otherwise restricted media.

## 5. Important Product Decisions

These decisions are part of the plan and should not be changed casually during implementation.

### 5.1 Upload-first experience

The home page is the working application, not a marketing landing page. The upload control is the dominant first-viewport element.

YouTube URL input is available through a secondary tab or expandable option labelled `Import from YouTube`. It must not make the upload flow harder to find.

### 5.2 One selected output format per job

The user selects one output format for a job:

- MP3
- WAV
- FLAC
- OGG
- M4A

The result is a ZIP containing every generated stem in that selected format. This satisfies the five-format requirement while keeping processing time, storage, and bandwidth predictable.

An `All formats` export can be added later. If it is added, it must be a separate explicit option because it multiplies output size and encoding time.

### 5.3 Separation modes

The initial mode list should be small:

- `Vocals + instrumental`: the default and fastest mode. Use the best currently validated two-source model available to the project.
- `Full stems`: a multi-source model producing vocals, drums, bass, and other, only if it meets quality and timing benchmarks.

A six-stem mode with guitar and piano can be added after the basic flow is stable. It should not be exposed merely because a model claims to support it; it must pass the benchmark suite and remain within the supported duration limit.

### 5.4 Guest access for the initial product

The first product version should allow a visitor to process a limited number of jobs without creating an account. This minimizes friction and matches the requested experience.

Guest protection must include:

- Per-IP rate limiting.
- Per-browser/session limits.
- Maximum file size and duration.
- Maximum concurrent jobs per fingerprint.
- Short output retention.
- An abuse-reporting path.

Accounts and paid usage should be added before uncontrolled public scale.

### 5.5 Async job model

Every extraction is a job. The browser starts a job and polls its status. It does not hold open a request while the GPU works.

There are two IDs:

- `app_job_id`: generated by the web application and safe to expose to the browser.
- `worker_call_id`: the internal Modal execution reference, never exposed as the public job identifier.

The database maps the two IDs.

## 6. High-Level Architecture

```text
                         User browser
                              |
                              | HTTPS
                              v
                 Next.js application on Vercel
          upload UI, API routes, status, signed downloads
                    |                         |
                    |                         |
                    v                         v
             Neon Postgres              Object storage
             job metadata               private source/results
                    |                         ^
                    |                         |
                    v                         |
             Modal HTTP endpoint ------------+
                    |
                    | spawn/queue async job
                    v
          Modal GPU processing container
       yt-dlp -> decode -> model -> encode -> upload
                    |
                    | signed internal callback
                    v
        Vercel internal worker-event endpoint
```

### 6.1 Web application

Responsibilities:

- Render the upload-first user interface.
- Validate user input before a job is created.
- Create presigned upload URLs for source files.
- Create and persist application jobs.
- Trigger the Modal worker asynchronously.
- Expose job status to the browser.
- Accept authenticated worker progress/completion events.
- Issue short-lived signed download URLs.
- Apply quotas, rate limits, and abuse controls.
- Never run model inference or proxy large audio files through a Vercel function.

### 6.2 Modal worker

Responsibilities:

- Accept a trusted job payload.
- Download the uploaded source from object storage, or download a supported YouTube source.
- Validate the actual audio file with `ffprobe`.
- Decode audio into the format expected by the selected model.
- Run GPU inference.
- Reassemble chunked inference output using the model/library's overlap-add behavior.
- Encode each stem into the selected output format.
- Create a ZIP and a manifest.
- Upload private result objects.
- Report progress and terminal status to the web application.
- Remove temporary local files in all success and failure paths.

### 6.3 Database

Use a managed Postgres database suitable for Vercel serverless access. Supabase is the selected provider; connect through its transaction pooler (port 6543) so serverless functions do not exhaust connections.

The database stores metadata and state, not large audio files.

### 6.4 Object storage

Use private object storage with S3-compatible access and presigned URLs. Cloudflare R2 is the selected provider (zero egress fees for stem downloads; AWS S3 also works through the same adapter).

The storage abstraction must hide the provider from application code. It must support:

- Presigned browser uploads.
- Worker downloads.
- Worker result uploads.
- Short-lived signed downloads.
- Object deletion.
- Lifecycle expiration rules.

Do not make result objects public merely to simplify downloads.

## 7. Repository Layout

Use one repository with independently deployable applications. This keeps contracts and documentation together while preserving separate deployment boundaries.

```text
/
├── apps/
│   └── web/                         # Next.js app deployed to Vercel
│       ├── app/
│       │   ├── page.tsx             # Upload-first application screen
│       │   ├── jobs/[jobId]/page.tsx # Job progress/result screen
│       │   └── api/
│       │       ├── uploads/presign/route.ts
│       │       ├── jobs/route.ts
│       │       ├── jobs/[jobId]/route.ts
│       │       ├── jobs/[jobId]/cancel/route.ts
│       │       ├── jobs/[jobId]/downloads/route.ts
│       │       └── internal/worker-events/route.ts
│       ├── components/
│       │   ├── upload-dropzone.tsx
│       │   ├── source-picker.tsx
│       │   ├── separation-options.tsx
│       │   ├── job-progress.tsx
│       │   ├── stem-result-list.tsx
│       │   ├── audio-preview.tsx
│       │   └── error-state.tsx
│       ├── lib/
│       │   ├── db.ts
│       │   ├── storage.ts
│       │   ├── worker-client.ts
│       │   ├── rate-limit.ts
│       │   ├── validation.ts
│       │   └── jobs.ts
│       ├── drizzle/                  # Database migrations
│       ├── public/
│       ├── package.json
│       └── next.config.ts
├── worker/
│   ├── app.py                        # Modal application entry point
│   ├── pipeline.py                   # Orchestration of one job
│   ├── input_audio.py                # Download, ffprobe, decode
│   ├── separators/
│   │   ├── base.py
│   │   ├── vocal_instrumental.py
│   │   └── full_stems.py
│   ├── encoding.py                   # FFmpeg encoding and ZIP creation
│   ├── storage.py                    # Object storage client
│   ├── callbacks.py                  # Signed web application events
│   ├── progress.py
│   ├── pyproject.toml
│   └── tests/
├── packages/
│   └── contracts/
│       ├── job-events.schema.json
│       ├── job-request.schema.json
│       ├── output-manifest.schema.json
│       └── README.md
├── docs/
│   ├── architecture.md
│   ├── operations.md
│   ├── model-evaluation.md
│   ├── privacy-and-content-policy.md
│   └── launch-checklist.md
├── .env.example
├── README.md
└── STEM_EXTRACTOR_PLAN.md
```

The exact ORM and component filenames may change. The separation between `apps/web`, `worker`, and `packages/contracts` should remain.

## 8. User Experience Specification

### 8.1 Home screen

The initial screen must contain:

- Product name.
- A large drag-and-drop upload control.
- A visible `Choose MP3` or `Choose audio file` action.
- Supported file and size limits.
- A secondary YouTube import option.
- Separation mode control.
- Output format control.
- Primary `Separate audio` button.
- Terms/content-rights acknowledgement near the action if required by legal review.

Do not show advanced infrastructure or model names to normal users. A small `Advanced` disclosure can expose model information later.

### 8.2 File selection states

The upload control must support:

- Empty state.
- Drag-over state.
- Selected file state.
- Uploading state with byte progress.
- Uploaded state.
- Invalid file state.
- File-too-large state.
- Duration-too-long state.
- Unsupported codec state.
- Cancel/remove action before processing.

The UI must validate client-side for fast feedback but treat all server-side validation as authoritative.

### 8.3 Processing screen

After job creation, navigate to `/jobs/{app_job_id}`.

Show:

- Source filename.
- Selected mode.
- Selected output format.
- Current stage.
- Progress indicator.
- Approximate status text.
- Cancel action where cancellation is supported.
- A safe browser-refresh/recovery path.

Stages shown to the user should be simple:

1. Preparing audio.
2. Analyzing track.
3. Separating stems.
4. Encoding files.
5. Preparing downloads.

Do not expose raw stack traces, internal IDs, storage keys, or provider errors.

### 8.4 Results screen

Show:

- Completed status.
- Stem list with human-readable names.
- Native audio preview controls.
- Individual download buttons.
- `Download all` ZIP button.
- Output format and expiration time.
- Retry action that creates a new job.
- Copyable result link if guest job recovery supports it.

The browser must request short-lived signed download URLs only when the user asks to download or preview a file.

### 8.5 Failure screen

Every failure must provide:

- Plain-language explanation.
- Whether retrying is likely to help.
- A retry action when safe.
- A support/reference code.
- No secret values or raw worker output.

Examples:

- The file is not a supported audio file.
- The track is longer than the current limit.
- The source could not be downloaded.
- The processing service is busy.
- The job exceeded the time limit.
- The files expired.

### 8.6 Responsive and accessibility requirements

- Keyboard-accessible file selection and controls.
- Visible focus states.
- Labels associated with every form control.
- Status updates announced through an appropriate live region.
- No color-only status communication.
- Sufficient contrast in light and dark themes if both are offered.
- Touch-friendly controls on mobile.
- Audio controls usable without hover.
- Layout must not shift when progress text or filenames change.

Use shadcn/ui primitives and the existing Tailwind design system once the web app is scaffolded. Keep the visual style restrained and tool-oriented rather than turning the product into a decorative marketing page.

## 9. Supported Inputs and Limits

Initial recommended limits:

- Accepted source types: MP3, WAV, FLAC, OGG, M4A.
- Initial advertised focus: MP3 upload.
- Maximum file size: choose a value based on storage and worker memory testing, initially 100 MB.
- Maximum duration: initially 8 minutes for the default mode and 6 minutes for full stems if benchmarks require it.
- Maximum concurrent guest jobs per IP: 1 or 2.
- Maximum daily guest jobs per IP: define a conservative value before public launch.
- Maximum YouTube source duration: same policy as uploads.
- Maximum source sample rate/channels: normalize supported values in preprocessing.

These are configuration values, not magic constants. Store them in server-side configuration and expose a sanitized subset to the UI.

Before launch, run real benchmarks and adjust the limits. Do not claim the five-minute target until the limit is backed by measured results.

## 10. Detailed End-to-End Flows

### 10.1 Upload flow

1. Browser selects an audio file.
2. Browser checks extension, MIME hint, and local size.
3. Browser calls `POST /api/uploads/presign` with filename, size, MIME type, and a client-generated upload token.
4. Web server validates file size/type policy and returns a presigned object-storage upload URL plus an object key.
5. Browser uploads directly to object storage with a progress bar.
6. Browser calls `POST /api/jobs` with the object key, source metadata, separation mode, output format, and an idempotency key.
7. Web server verifies that the object exists and belongs to the current upload session.
8. Web server creates the application job in Postgres with status `queued`.
9. Web server calls the Modal launch endpoint with the app job ID and signed source reference.
10. Web server stores the returned worker call reference and returns the app job ID.
11. Browser navigates to the job page and begins polling.

The web server must never accept an arbitrary object key from an untrusted request. Object keys should contain a server-generated upload token or random identifier.

### 10.2 YouTube flow

1. User opens the `Import from YouTube` control.
2. Browser sends the URL to the server for validation.
3. Server accepts only supported hostnames such as `youtube.com`, `www.youtube.com`, and `youtu.be` after URL parsing.
4. Server creates a queued job without pretending that the remote media has already been validated.
5. Modal worker downloads the source with `yt-dlp`.
6. Worker validates duration and actual media information with `ffprobe`.
7. If limits are exceeded, the worker reports a structured failure and does not proceed to GPU inference.
8. The rest of the pipeline matches the upload flow.

The service must not bypass DRM, private access controls, age gates, or other access restrictions. Legal review must confirm the YouTube workflow and user-facing terms before enabling it publicly.

### 10.3 Polling flow

1. Browser calls `GET /api/jobs/{app_job_id}` every 2 to 3 seconds while the job is active.
2. Server authenticates the request using the signed guest ID cookie (httpOnly, HMAC) plus the per-job access token; account ownership replaces both once accounts exist.
3. Server returns sanitized status and progress.
4. On completion, server returns output metadata but not permanent public URLs.
5. Browser requests signed downloads when needed.
6. Polling stops on `completed`, `failed`, `canceled`, or `expired`.

Add exponential backoff after repeated unchanged responses and stop polling when the page is hidden if appropriate. Resume polling when the user returns.

### 10.4 Worker callback flow

1. Modal worker emits `started` after accepting the job.
2. Worker emits progress updates at stage boundaries and safe intervals.
3. Worker uploads results.
4. Worker emits `completed` with an output manifest, object keys, sizes, checksums, and expiry.
5. On failure, worker emits `failed` with a stable public error code and an internal diagnostic reference.
6. Vercel validates the callback signature, event ID, job ID, and allowed state transition.
7. Vercel updates the database transactionally.
8. Duplicate callbacks are ignored using the unique event ID.

## 11. API Contract

All web API responses should be JSON with consistent error shapes.

### 11.1 `POST /api/uploads/presign`

Purpose: create a short-lived direct-upload URL.

Request:

```json
{
  "filename": "song.mp3",
  "contentType": "audio/mpeg",
  "sizeBytes": 7340032
}
```

Response:

```json
{
  "uploadId": "upl_...",
  "objectKey": "sources/upl_.../input.mp3",
  "uploadUrl": "https://storage.example/...",
  "expiresInSeconds": 900
}
```

Rules:

- Never trust the client-provided content type.
- Limit URL lifetime.
- Limit object key lifetime and ownership.
- Reject files exceeding configured size limits.
- Rate-limit presign requests.

### 11.2 `POST /api/jobs`

Purpose: create an application job and start worker processing.

Upload request:

```json
{
  "source": {
    "type": "upload",
    "uploadId": "upl_...",
    "objectKey": "sources/upl_.../input.mp3",
    "filename": "song.mp3"
  },
  "mode": "vocals_instrumental",
  "outputFormat": "mp3",
  "idempotencyKey": "client-generated-random-value"
}
```

YouTube request:

```json
{
  "source": {
    "type": "youtube",
    "url": "https://www.youtube.com/watch?v=..."
  },
  "mode": "vocals_instrumental",
  "outputFormat": "mp3",
  "idempotencyKey": "client-generated-random-value"
}
```

Response:

```json
{
  "jobId": "job_...",
  "status": "queued",
  "statusUrl": "/api/jobs/job_..."
}
```

Rules:

- Validate mode and format against allowlists.
- Verify upload ownership and object existence.
- Create or return the existing job for a repeated idempotency key.
- Enforce quota before starting a worker.
- Never expose the Modal call ID.

### 11.3 `GET /api/jobs/{jobId}`

Response while running:

```json
{
  "jobId": "job_...",
  "status": "processing",
  "stage": "separating",
  "progress": 54,
  "source": { "filename": "song.mp3" },
  "mode": "vocals_instrumental",
  "outputFormat": "mp3",
  "createdAt": "2026-09-06T00:00:00.000Z",
  "updatedAt": "2026-09-06T00:01:12.000Z"
}
```

Response when complete:

```json
{
  "jobId": "job_...",
  "status": "completed",
  "stage": "completed",
  "progress": 100,
  "source": { "filename": "song.mp3" },
  "mode": "vocals_instrumental",
  "outputFormat": "mp3",
  "stems": [
    { "id": "vocals", "label": "Vocals", "durationSeconds": 214.2 },
    { "id": "instrumental", "label": "Instrumental", "durationSeconds": 214.2 }
  ],
  "downloadUrl": "/api/jobs/job_.../downloads?kind=zip",
  "expiresAt": "2026-09-07T00:00:00.000Z"
}
```

The response should not include storage credentials, internal object keys, or unrestricted URLs.

### 11.4 `POST /api/jobs/{jobId}/cancel`

Purpose: request cancellation.

Cancellation is cooperative. If the worker cannot cancel a running GPU call immediately, the job is marked `cancel_requested` and the worker stops at the next safe boundary. The result must not be exposed as completed if the user canceled it.

### 11.5 `GET /api/jobs/{jobId}/downloads`

Query parameters:

- `kind=zip`
- `kind=stem&stem=vocals`

The endpoint verifies ownership, checks job state and expiry, then returns a short-lived signed redirect or JSON URL. It must not stream large files through the Vercel function unless there is a specific reason to do so.

### 11.6 `POST /api/internal/worker-events`

This endpoint is not public application functionality.

Headers:

- `X-Worker-Event-Id`
- `X-Worker-Timestamp`
- `X-Worker-Signature`

Payload:

```json
{
  "jobId": "job_...",
  "eventId": "evt_...",
  "status": "processing",
  "stage": "encoding",
  "progress": 82,
  "workerCallId": "internal-reference",
  "outputs": [],
  "error": null
}
```

Validate an HMAC signature over the timestamp and raw request body. Reject stale timestamps, invalid signatures, unknown jobs, and impossible state transitions.

## 12. Database Model

Use migrations from the start. Do not create production tables manually without a migration file.

### 12.1 `jobs`

Suggested fields:

- `id`: public application job ID, random and non-sequential.
- `access_token_hash`: hash of guest access token if guest recovery is supported.
- `owner_user_id`: nullable until accounts exist.
- `source_type`: `upload` or `youtube`.
- `source_filename`: sanitized display name.
- `source_object_key`: private storage key for uploads.
- `source_url`: YouTube URL metadata, stored only as required for support and covered by the privacy policy.
- `source_duration_seconds`.
- `source_size_bytes`.
- `source_sha256`.
- `mode`.
- `output_format`.
- `status`.
- `stage`.
- `progress`.
- `worker_call_id`.
- `idempotency_key_hash`.
- `error_code`.
- `error_message_public`.
- `diagnostic_reference`.
- `created_at`.
- `started_at`.
- `completed_at`.
- `expires_at`.
- `updated_at`.

Indexes:

- Unique owner/session + idempotency key hash where appropriate.
- `status` and `created_at` for operational queries.
- `expires_at` for cleanup.
- `worker_call_id` for diagnostics, never for public lookup.

### 12.2 `job_outputs`

Suggested fields:

- `id`.
- `job_id`.
- `stem_key`: `vocals`, `instrumental`, `drums`, `bass`, `other`, etc.
- `label`.
- `object_key`.
- `mime_type`.
- `size_bytes`.
- `duration_seconds`.
- `sha256`.
- `created_at`.
- `expires_at`.

### 12.3 `worker_events`

Suggested fields:

- `event_id` unique.
- `job_id`.
- `event_type`.
- `payload_hash`.
- `received_at`.

This table makes callbacks idempotent and auditable.

### 12.4 `usage_events`

Use this for rate limits, cost accounting, and future billing:

- `id`.
- `job_id`.
- `actor_key_hash`.
- `event_type`.
- `mode`.
- `duration_seconds`.
- `gpu_seconds` if available.
- `created_at`.

Do not store raw IP addresses longer than privacy requirements allow. Hash or rotate identifiers according to the privacy policy.

## 13. Worker Implementation

### 13.1 Modal application

The worker is a separate Python application deployed with the Modal CLI.

It should include:

- A pinned Python version.
- A pinned Modal SDK version.
- A Debian/Ubuntu-based image with FFmpeg including `libmp3lame` and `libvorbis`; use the native AAC encoder and avoid nonfree `libfdk_aac`.
- PyTorch and the selected CUDA-compatible runtime.
- The separation libraries and model adapters.
- `yt-dlp` only if YouTube mode is enabled.
- An S3-compatible storage client.
- HTTP client support for callbacks.
- A persistent Modal volume for validated model weights.

Do not copy model weights into the Git repository.

### 13.2 Worker entrypoint behavior

The public Modal endpoint should accept a short request and return an accepted response quickly. It must not keep the Vercel request open for the full extraction.

The endpoint should:

1. Authenticate the launch request.
2. Validate the job payload.
3. Start or enqueue the background Modal function.
4. Return the internal worker call ID and accepted status to the web app.

The background function should perform the long-running work.

Use the current Modal SDK's documented async invocation mechanism. Do not rely on an unverified assumption about a particular SDK method or exception name; pin and test the SDK version in CI.

### 13.3 Model loading and caching

Load models once per warm GPU container where supported:

- Store model checkpoints in a persistent Modal volume.
- Load the selected model during container initialization.
- Move it to the GPU once.
- Set evaluation mode.
- Use inference-only execution.
- Record model name, version, commit/checksum, and runtime settings in job diagnostics.

If multiple models consume too much GPU memory, use separate worker classes or separate model images so one container does not load every model simultaneously.

### 13.4 Separation adapter contract

Each model adapter should implement the same conceptual interface:

```text
validate_mode(mode)
load_model()
separate(input_waveform) -> map of stem name to waveform
model_metadata() -> name, version, license, expected stems
```

The rest of the worker must not know model-specific tensor details.

### 13.5 Model candidates

Evaluate current open-source candidates before selecting production defaults. The initial shortlist should include:

- Meta Demucs / HTDemucs family for multi-source separation.
- BS-RoFormer or a current, maintained RoFormer implementation for vocal/instrumental separation.
- Other actively maintained source-separation implementations only if their licenses, weights, and runtime behavior are suitable.

The plan must not assume that the newest repository, model name, or online demo is automatically the best production choice.

Selection criteria:

- Separation quality on representative genres.
- Vocal bleed and instrumental artifacts.
- Runtime on the target GPU.
- Peak VRAM use.
- Model license and commercial-use compatibility.
- Maintenance and reproducibility.
- Compatibility with Python, CUDA, and FFmpeg pipeline.
- Failure behavior on mono, stereo, clipped, quiet, and corrupted inputs.

### 13.6 Audio preprocessing

The worker must:

1. Download or retrieve the input to a temporary directory.
2. Run `ffprobe` and parse structured output.
3. Verify the file is actually audio.
4. Enforce size, duration, channels, and sample-rate limits.
5. Decode using a trusted library or FFmpeg.
6. Convert to the model's expected sample rate and channel layout.
7. Preserve the source duration and timing metadata.
8. Avoid destructive normalization unless the selected model requires it.
9. Reject empty, silent, corrupt, or unsupported input according to policy.

Use subprocess argument arrays, never shell interpolation of user input.

### 13.7 Inference

The separation stage should use the selected library's tested inference path, including its chunking and overlap-add implementation.

Conceptually:

```text
input audio
  -> waveform tensor
  -> fixed inference windows
  -> GPU model inference
  -> overlapping windows reassembled
  -> one waveform per stem
```

Do not implement custom chunk stitching until there is a measured reason. A bad overlap-add implementation can create audible seams.

Inference requirements:

- Use `no_grad` or the framework equivalent.
- Use mixed precision only after quality tests verify it does not create unacceptable artifacts.
- Track inference duration and peak memory.
- Check tensor shape and sample count for every output stem.
- Detect NaN, infinity, or unexpected clipping before encoding.
- Preserve a deterministic seed only if the selected model uses randomness.

### 13.8 Output encoding

Encode from the separated waveform or a high-quality temporary WAV master.

Initial settings should be documented and tested:

- MP3: LAME, high quality or configured target bitrate.
- WAV: PCM, documented bit depth and sample rate.
- FLAC: lossless, moderate compression level.
- OGG: Vorbis quality setting or a documented Opus policy.
- M4A: AAC with a documented bitrate.

The encoder must preserve stem duration. Verify output with `ffprobe` after encoding.

Do not imply that WAV or FLAC restores quality lost in the original MP3.

### 13.9 Packaging

Create:

- Individual stem objects for preview/download.
- One ZIP containing the selected-format stems.
- A `manifest.json` inside the ZIP containing job metadata, mode, model version, format, durations, and checksums.

Do not include secrets, internal storage keys, or user access tokens in the manifest.

### 13.10 Temporary file cleanup

Use a unique per-job temporary directory. Cleanup must run in a `finally` path even when model inference, encoding, upload, or callback fails.

The worker must never reuse a previous job's temporary directory.

## 14. Progress Reporting

Progress should be stage-based rather than pretending to know exact model progress when it is not available.

Suggested mapping:

- `queued`: 0
- `downloading`: 10 to 20
- `validating`: 20 to 25
- `separating`: 25 to 75
- `encoding`: 75 to 90
- `uploading_results`: 90 to 98
- `completed`: 100

The `downloading` range applies only to YouTube sources; upload jobs begin at `validating`.

If the model library provides reliable chunk progress, use it within the separation range. Otherwise, emit stage progress and an indeterminate visual indicator.

Throttle callbacks so a job does not create excessive database writes. A progress update every 2 to 5 seconds or at meaningful stage transitions is sufficient.

## 15. Security Requirements

### 15.1 Web security

- Keep all secrets server-side.
- Use secure, HTTP-only cookies if sessions are introduced.
- Configure CSRF protection where cookie-authenticated mutation routes need it.
- Validate JSON payload sizes.
- Add security headers.
- Avoid reflecting arbitrary filenames or URLs into HTML without escaping.
- Use randomized public IDs.
- Do not expose database IDs, storage credentials, or Modal references.

### 15.2 Upload security

- Validate size before presigning.
- Validate actual content after upload.
- Use private object storage.
- Generate server-owned object keys.
- Restrict object prefixes and upload expiry.
- Delete abandoned uploads.
- Never execute uploaded files.
- Run `ffprobe` with bounded resource usage.

### 15.3 YouTube security

- Parse URLs with a real URL parser.
- Allow only approved hostnames.
- Do not accept arbitrary downloader options from users.
- Do not support private, DRM-protected, or access-controlled media.
- Apply download timeouts and size limits.
- Keep `yt-dlp` updated and pinned through controlled dependency updates.
- Review terms of service and copyright policy before launch.

### 15.4 Worker callback security

- Use HMAC signatures with timestamp replay protection.
- Use a separate launch secret and callback secret.
- Validate event IDs for idempotency.
- Reject callbacks for expired or terminal jobs unless they are safe no-op duplicates.
- Use TLS-only URLs.

### 15.5 Abuse controls

- Per-IP rate limits.
- Per-session job limits.
- Maximum active jobs per actor.
- Maximum total input duration per day for guests.
- Queue concurrency caps.
- CAPTCHA or account requirement if abuse becomes significant.
- Administrative kill switch to disable YouTube mode or new jobs.

## 16. Privacy and Content Policy

Before public launch, publish:

- Privacy policy.
- Terms of service.
- Content and copyright policy.
- File retention policy.
- Contact/support method.

The product should clearly state:

- Users must have permission to process uploaded or linked audio.
- Uploaded files and generated outputs are private by default.
- Files are deleted automatically after the stated retention window.
- The service may retain minimal diagnostic metadata for reliability and abuse prevention.
- Model providers and infrastructure subprocessors may process data as required to operate the service.

Do not retain audio indefinitely by default.

## 17. Environment Configuration

Use separate values for local, preview/staging, and production.

### 17.1 Web environment variables

```text
DATABASE_URL=
APP_URL=

STORAGE_ENDPOINT=
STORAGE_REGION=
STORAGE_BUCKET=
STORAGE_ACCESS_KEY_ID=
STORAGE_SECRET_ACCESS_KEY=

WORKER_LAUNCH_URL=
WORKER_LAUNCH_SECRET=
WORKER_CALLBACK_SECRET=

JOB_ACCESS_TOKEN_SECRET=
UPLOAD_SIGNING_SECRET=

MAX_UPLOAD_BYTES=104857600
MAX_DURATION_SECONDS=480
JOB_RETENTION_HOURS=24
```

Rate limiting uses Postgres-backed counters at MVP; add a separate provider only if the database becomes a bottleneck.

### 17.2 Modal environment variables

```text
STORAGE_ENDPOINT=
STORAGE_REGION=
STORAGE_BUCKET=
STORAGE_ACCESS_KEY_ID=
STORAGE_SECRET_ACCESS_KEY=

WEB_CALLBACK_URL=
WORKER_CALLBACK_SECRET=
WORKER_LAUNCH_SECRET=

MODEL_CACHE_VOLUME=
MODEL_VERSION=
```

Use Modal Secrets or the platform's secret manager. Never commit these values.

### 17.3 Local environment

Commit `.env.example` with names and descriptions, never values. Add validation that fails clearly when required production variables are missing.

## 18. Deployment Architecture

### 18.1 Vercel deployment

- Connect the repository to a Vercel project.
- Set the web project root to `apps/web` if required by the chosen monorepo configuration.
- Configure build and install commands.
- Add preview and production environment variables separately.
- Run database migrations in a controlled deployment step, not on every request.
- Configure the production domain.
- Confirm that large uploads go directly to object storage rather than through Vercel.

### 18.2 Modal deployment

- Authenticate the Modal CLI locally or through CI.
- Build the pinned worker image.
- Create or reuse the model-cache volume.
- Deploy the Modal app from the `worker` directory.
- Configure secrets in Modal.
- Verify the launch endpoint and callback path in staging.
- Set GPU type, timeout, concurrency, and scaledown behavior based on benchmarks.
- Keep a rollback deployment or previously validated model image available.

The worker and web app are independently deployable. A web UI deployment must not rebuild the GPU image, and a model deployment must not require a web UI deployment unless the API contract changes.

### 18.3 Deployment order

1. Create production object storage bucket and private lifecycle rules.
2. Create staging and production databases.
3. Apply database migrations.
4. Deploy the web app with worker launch disabled or in maintenance mode.
5. Deploy the Modal worker and run a staging smoke test.
6. Set the production worker URL and secrets in Vercel.
7. Enable production processing.
8. Run an end-to-end production smoke test with a non-sensitive fixture.
9. Monitor logs and latency before announcing the URL.

### 18.4 CI/CD

Every pull request should run:

- Web lint.
- Web typecheck.
- Web unit tests.
- Worker formatting/lint/type checks.
- Worker unit tests.
- Contract schema validation.
- Build validation.

Protected branches should require CI success. Production Modal deployment should be a deliberate workflow, not an accidental deploy from every frontend change.

## 19. Testing Strategy

### 19.1 Web unit tests

Cover:

- File policy validation.
- YouTube URL allowlist validation.
- Mode and format validation.
- Job state transition rules.
- Callback signature verification.
- Callback replay/idempotency behavior.
- Public error mapping.
- Signed download authorization.
- Quota calculations.

### 19.2 API integration tests

Cover:

- Presign request creation.
- Job creation for upload source.
- Job creation for YouTube source.
- Repeated idempotency key behavior.
- Missing or unauthorized job access.
- Worker launch failure.
- Callback updates and duplicate callback handling.
- Expired job behavior.
- Cancellation behavior.

Use a test database and fake storage/worker adapters. Do not call a paid GPU for every pull request.

### 19.3 Worker unit tests

Cover:

- Input validation from `ffprobe` output.
- Duration and size rejection.
- Safe filename conversion.
- Mode-to-adapter mapping.
- Output filename generation.
- FFmpeg command construction.
- ZIP manifest construction.
- Callback signing.
- Cleanup on all failure paths.
- Invalid tensor and clipping detection.

### 19.4 Worker integration tests

Use a short fixture track whose redistribution is allowed.

Verify:

- Source download or object retrieval.
- Decode.
- Model loading.
- Separation output stem names.
- Duration alignment.
- Encoding for each supported format.
- Upload and callback.
- Result manifest checksums.

Run GPU integration tests as a scheduled Modal function (GitHub-hosted CI has no GPU) and before worker releases, rather than on every web-only change.

### 19.5 End-to-end tests

Use a staging deployment and fixture audio:

1. Visit the home page.
2. Upload the fixture.
3. Select a mode and format.
4. Start a job.
5. Observe processing status.
6. Wait for completion.
7. Play or download each result.
8. Confirm files can be decoded and have expected duration.
9. Confirm expired links no longer work after the retention policy test.

### 19.6 Performance tests

Benchmark at least:

- 30-second track.
- 3-minute track.
- Maximum supported track.
- Mono and stereo source.
- MP3 and lossless source.
- Cold worker start.
- Warm worker run.
- One job and multiple queued jobs.

Record:

- Upload time.
- Queue delay.
- Worker startup time.
- Download time.
- Decode time.
- Inference time.
- Encoding time.
- Upload time.
- Total user-visible time.
- Peak RAM and VRAM.
- Failure rate.

The launch target should be defined from these measurements, such as p90 completion under five minutes for supported tracks under normal queue capacity.

### 19.7 Accessibility testing

- Keyboard-only flow.
- Screen-reader upload and status flow.
- Reduced-motion mode.
- Mobile viewport.
- High zoom.
- Focus recovery after navigation.
- Error announcement.

## 20. Model Evaluation Plan

Before locking the production model:

1. Select representative, legally usable evaluation tracks across genres.
2. Include dense mixes, sparse mixes, live-like audio, stereo width, backing vocals, and percussion-heavy tracks.
3. Run candidate models with the same input preprocessing.
4. Compare objective metrics where ground truth exists.
5. Perform blind listening tests for vocal bleed, musical artifacts, and transient damage.
6. Record GPU time and memory.
7. Check model and weights licenses for commercial deployment.
8. Pin the selected repository commit, package versions, and checkpoint checksum.
9. Store evaluation results in `docs/model-evaluation.md`.

Production output should include model metadata in the internal job record so quality regressions can be traced to model changes.

## 21. Observability and Operations

### 21.1 Structured logs

Log JSON with:

- `event`.
- `app_job_id`.
- `worker_call_id` only in private logs.
- `stage`.
- `status`.
- `duration_ms`.
- `model_version`.
- `error_code`.
- `environment`.

Do not log raw audio, credentials, signed URLs, or full user-provided URLs where avoidable.

### 21.2 Metrics

Track:

- Jobs created.
- Jobs completed.
- Jobs failed by public error code.
- Jobs canceled.
- Queue delay.
- End-to-end duration.
- GPU inference duration.
- Worker cold-start duration.
- Output upload failures.
- Download requests.
- Rate-limit rejections.
- Storage bytes and deletion success.

### 21.3 Alerts

Alert on:

- Worker launch failures.
- Callback signature failures above baseline.
- Completion failure rate above threshold.
- p90 processing time above the target.
- Storage cleanup failures.
- Database connection errors.
- Unexpected GPU cost or concurrency.
- Repeated input validation abuse.
- Daily GPU spend or GPU-seconds above the configured budget, which triggers the kill switch automatically.

### 21.4 Support diagnostics

A user-facing reference code should map to a private diagnostic record. Support staff should be able to find a job by reference code without seeing file contents unnecessarily.

## 22. Retention and Cleanup

Recommended initial policy:

- Uploaded source files deleted after successful processing or after a short maximum window.
- Generated outputs expire after 24 hours for guests.
- Account retention can be longer only if explicitly selected by the user.
- Abandoned uploads expire automatically.
- Database job metadata is retained only as long as needed for support, usage accounting, and legal obligations.

Implement cleanup as an idempotent scheduled process. It must:

1. Find expired objects.
2. Delete them from storage.
3. Mark database records as expired.
4. Retry transient deletion failures.
5. Emit metrics for failures.

## 23. Product Launch Stages

### Stage 0: Technical spike

Deliver:

- One local worker command that separates a fixture.
- One local web form that submits a fixture job.
- Confirm model quality, VRAM, and timing.
- Confirm output format compatibility.

Exit criteria:

- At least one candidate model produces usable stems.
- Maximum supported track limit is evidence-based.
- Commercial-use license is acceptable or a replacement is identified.

### Stage 1: Private development environment

Deliver:

- Next.js upload UI.
- Direct object-storage upload.
- Database job state.
- Modal worker deployment.
- Signed worker callbacks.
- Polling job page.
- MP3 output.

Exit criteria:

- A new developer can run the complete flow from the README.
- Staging can process a fixture end to end.
- A failed worker job does not leave an indefinite `processing` record.

### Stage 2: MVP beta

Deliver:

- All five selectable output formats.
- Vocals/instrumental mode.
- Full-stems mode if benchmarked.
- Guest quotas.
- Secure downloads.
- Retention cleanup.
- Responsive and accessible UI.
- Error and retry flows.
- Terms, privacy, and content policy pages.

Exit criteria:

- p90 supported-track completion is within the published target.
- No known critical authorization or storage exposure.
- End-to-end tests pass on staging.
- At least several real-world test tracks pass quality review.

### Stage 3: Public launch

Deliver:

- Production Vercel domain.
- Production Modal deployment.
- Monitoring and alerting.
- Abuse controls.
- Support contact.
- Cost limits and emergency kill switch.
- Launch checklist sign-off.

### Stage 4: Paid product

Add:

- Accounts.
- Email verification if needed.
- Job history.
- Credits or subscriptions.
- Payment provider integration after provider selection and review.
- Per-user quotas.
- Receipts and account deletion.

Do not add billing before the anonymous core workflow is reliable and cost-bounded.

## 24. Implementation Task Breakdown

Each task should leave the repository in a buildable state.

### Phase 1: Foundation

#### Task 1: Initialize repository and workspace

Dependencies: none.

Work:

- Create the monorepo layout.
- Scaffold the Next.js app.
- Add TypeScript, Tailwind, shadcn/ui, linting, and formatting.
- Add the Python worker project with pinned dependencies (pip + venv).
- Use npm workspaces for the monorepo and Drizzle ORM against Supabase Postgres (transaction pooler connection).
- Add root documentation and environment templates.

Acceptance criteria:

- Web app starts locally.
- Worker test command starts successfully.
- No secrets are committed.

Verification:

- Web lint and typecheck pass.
- Worker lint/type checks pass.
- README setup instructions work on a clean machine.

#### Task 2: Define shared contracts

Dependencies: Task 1.

Work:

- Add JSON schemas for job requests, status responses, worker events, and manifests.
- Define enum values for statuses, stages, modes, formats, and public errors.
- Add schema validation tests.

Acceptance criteria:

- Web and worker agree on the event shapes.
- Invalid events are rejected in tests.

#### Task 3: Create database schema and migrations

Dependencies: Task 2.

Work:

- Add `jobs`, `job_outputs`, `worker_events`, and `usage_events` tables.
- Add state-transition constraints in application logic.
- Add seed/test helpers.

Acceptance criteria:

- A fresh database can be migrated from zero.
- Job records can be created and updated transactionally.

### Phase 2: Storage and upload

#### Task 4: Implement storage adapter

Dependencies: Tasks 1 and 2.

Work:

- Add presigned upload generation.
- Add object existence checks.
- Add signed download generation.
- Add delete and expiry helpers.
- Implement a fake adapter for tests.
- Configure bucket CORS for the app origin (GET/PUT/HEAD, content-type, etag, range requests) so XHR upload progress and audio previews work.

Acceptance criteria:

- Browser never sends large audio through Vercel.
- Storage objects are private.
- Tests cover unauthorized object access.

#### Task 5: Build the upload UI

Dependencies: Tasks 2 and 4.

Work:

- Add dropzone, file picker, validation, progress, remove, and retry states.
- Add responsive layout and accessibility behavior.

Acceptance criteria:

- MP3 can be selected and uploaded directly.
- Invalid files show useful errors.
- Upload progress is visible.

### Phase 3: Job creation and status

#### Task 6: Implement job creation API

Dependencies: Tasks 2, 3, and 4.

Work:

- Add presign route.
- Add job creation route.
- Add idempotency handling.
- Add guest access authorization.
- Add server-side policy validation.

Acceptance criteria:

- A valid uploaded object creates exactly one job.
- Repeated requests with the same idempotency key do not duplicate work.
- Invalid modes, formats, and objects are rejected.

#### Task 7: Implement job status API and page

Dependencies: Task 6.

Work:

- Add status route.
- Add job page.
- Add polling hook with backoff and terminal-state handling.
- Add progress UI and failure UI.

Acceptance criteria:

- Refreshing a job page recovers the job state.
- Polling stops at terminal states.
- Secrets and internal IDs never appear in responses.

### Phase 4: Worker spike

#### Task 8: Build local audio validation pipeline

Dependencies: Task 2.

Work:

- Implement temporary directory management.
- Add `ffprobe` validation.
- Add duration, channel, sample-rate, and size checks.
- Add safe source retrieval.

Acceptance criteria:

- Valid fixture audio passes.
- Corrupt, non-audio, oversized, and over-duration inputs fail with stable codes.
- Temporary files are removed after failures.

#### Task 9: Evaluate and wrap candidate models

Dependencies: Task 8.

Work:

- Add model adapter interface.
- Integrate the selected vocal/instrumental model.
- Evaluate Demucs or another multi-stem candidate.
- Pin versions and record license/benchmark data.

Acceptance criteria:

- Models produce expected stem names and durations.
- Model loading is cached in warm containers.
- Quality and performance results are documented.

#### Task 10: Implement encoding and packaging

Dependencies: Task 9.

Work:

- Add WAV, MP3, FLAC, OGG, and M4A encoding.
- Add output validation.
- Add ZIP and manifest creation.

Acceptance criteria:

- Every format decodes successfully.
- Stems have aligned duration.
- ZIP contains only expected files.

### Phase 5: Modal integration

#### Task 11: Deploy the Modal worker

Dependencies: Tasks 8, 9, and 10.

Work:

- Create the Modal image.
- Add GPU configuration.
- Add persistent model volume.
- Add launch endpoint.
- Add async execution.

Acceptance criteria:

- A staging request is accepted quickly.
- The long-running function executes independently of Vercel request duration.
- Worker logs include private diagnostic identifiers.

#### Task 12: Add signed progress and completion callbacks

Dependencies: Tasks 2, 3, and 11.

Work:

- Add callback signer in the worker.
- Add callback verifier in the web app.
- Add idempotent event storage.
- Add terminal failure handling.
- Add a scheduled reconcile route (Vercel Cron) that fails or relaunches stuck jobs and marks expired results.

Acceptance criteria:

- Progress reaches the web job page.
- Duplicate callbacks do not corrupt state.
- Invalid signatures are rejected.
- Stuck jobs have timeout recovery.

#### Task 13: Connect web job creation to Modal

Dependencies: Tasks 6, 11, and 12.

Work:

- Add worker launch client.
- Persist worker call ID.
- Handle launch failure transactionally.
- Add retry policy for transient launch failures.

Acceptance criteria:

- A web-created job reaches the worker.
- Launch failures become visible job failures or retry states.
- The Vercel request returns without waiting for inference.

### Phase 6: Results and product hardening

#### Task 14: Build result preview and downloads

Dependencies: Tasks 7, 10, and 12.

Work:

- Add stem result list.
- Add native audio previews.
- Add individual and ZIP downloads.
- Add signed URL expiry messaging.

Acceptance criteria:

- Users can preview and download every produced stem.
- Expired objects are not downloadable.
- Download authorization is tested.

#### Task 15: Add quotas, rate limits, and cleanup

Dependencies: Tasks 6 and 13.

Work:

- Add guest quotas.
- Add active-job limits.
- Add abandoned upload cleanup.
- Add result expiry cleanup.
- Add administrative disable switch.

Acceptance criteria:

- A single visitor cannot create unlimited jobs.
- Cleanup is retryable and observable.
- Costs are bounded under abuse scenarios.

#### Task 16: Add YouTube input mode

Dependencies: Tasks 8, 13, and legal review.

Work:

- Add secondary YouTube input UI.
- Add hostname and URL validation.
- Add worker downloader with strict limits.
- Add content-policy acknowledgement.

Acceptance criteria:

- Valid supported URLs process successfully.
- Unsupported/private/restricted URLs fail safely.
- Downloader cannot access arbitrary hosts.

### Phase 7: Verification and launch

#### Task 17: Add automated test coverage

Dependencies: all previous tasks.

Work:

- Add unit, API integration, worker integration, contract, and E2E suites.
- Add a legal test fixture track.
- Add staging smoke tests.

Acceptance criteria:

- CI passes on a clean checkout.
- Critical failure and authorization paths are covered.

#### Task 18: Run performance and model benchmark suite

Dependencies: Tasks 9 and 17.

Work:

- Measure cold and warm worker runs.
- Measure supported duration limits.
- Measure all output formats.
- Record cost and p90 latency.

Acceptance criteria:

- Published time target is backed by data.
- Input limits are configured from results.
- Model release is reproducible.

#### Task 19: Complete security, privacy, and launch review

Dependencies: Tasks 15, 16, and 17.

Work:

- Review secrets, storage policies, auth, callbacks, SSRF, uploads, and logs.
- Publish policy pages.
- Confirm model and dependency licenses.
- Configure monitoring and support.

Acceptance criteria:

- No known critical security issue remains.
- User deletion and retention behavior is documented.
- Production rollback plan exists.

#### Task 20: Deploy production and perform smoke test

Dependencies: Task 19.

Work:

- Deploy Modal worker.
- Deploy Vercel app.
- Configure domain and production variables.
- Process a legal fixture track.
- Confirm downloads, cleanup, and monitoring.

Acceptance criteria:

- A new visitor can complete the full flow from the public domain.
- Production results decode correctly.
- Logs and alerts are visible.
- Emergency disable control is tested.

## 25. Checkpoints

### Checkpoint A: Foundation

- Repository installs from a clean checkout.
- Web and worker checks pass.
- Shared contracts are documented.
- Database migrations apply cleanly.

### Checkpoint B: Local vertical slice

- A local upload creates a job.
- A fake worker updates the job.
- The browser reaches a result page.
- Tests cover the complete fake flow.

### Checkpoint C: GPU vertical slice

- A staging job reaches Modal.
- The worker separates a legal fixture.
- Results upload and callbacks update the UI.
- Processing time and VRAM are recorded.

### Checkpoint D: Public beta

- Five formats work.
- Quotas and cleanup are active.
- Accessibility and security tests pass.
- Privacy and content policies are published.

### Checkpoint E: Production launch

- End-to-end smoke test passes on the public domain.
- Monitoring and rollback are ready.
- Cost and concurrency limits are configured.
- Support workflow is documented.

## 26. Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Model inference exceeds five minutes | High | Benchmark early, limit duration, cache models, choose GPU size from data, expose honest target |
| GPU cold starts are slow | High | Persistent model volume, warm-container policy, queue and timeout instrumentation |
| Model quality is inconsistent | High | Compare candidates, blind listening tests, pin versions, publish supported modes conservatively |
| YouTube behavior changes | Medium | Keep upload as primary input, pin/update downloader deliberately, support graceful failures |
| Copyright or terms violation | High | Legal review, content policy, no DRM/private bypass, user responsibility acknowledgement |
| Anonymous abuse creates GPU cost | High | IP/session quotas, concurrency caps, CAPTCHA or accounts, emergency disable switch |
| Large files exceed web limits | High | Direct-to-storage upload, never proxy audio through Vercel |
| Storage URLs leak private audio | High | Private bucket, short-lived signatures, authorization before signing |
| Worker callback spoofing | High | HMAC signature, timestamp, replay protection, idempotent event IDs |
| Worker fails after accepting a job | High | Heartbeats/timeouts, stuck-job reaper, public retry path |
| Dependency/model license changes | Medium | Record licenses and checksums, review before upgrades |
| Output encoders produce invalid files | Medium | `ffprobe` validation and fixture tests for every format |
| Database connection exhaustion | Medium | Serverless-compatible driver/pooling, short transactions, retry policy |
| User expects permanent storage | Medium | Show expiry clearly, add accounts/history only with explicit retention policy |

## 27. Open Decisions Before Implementation

These decisions must be answered during the technical spike, not guessed during launch:

1. Which exact vocal/instrumental model passes the quality, latency, memory, and license review?
2. Which exact multi-stem model, if any, is included in the first public release?
3. Which S3-compatible object-storage provider is selected?
4. What maximum duration and file size meet the five-minute target at the target GPU size?
5. Is guest access allowed in the first public release, or is an account required to control cost?
6. What is the initial guest quota?
7. Are all five output formats selectable in the first beta, or is the first private test MP3-only?
8. Is YouTube enabled at public launch after legal review, or released later?
9. What output retention window is acceptable for guests?
10. What is the support and abuse-reporting contact?
11. Will the first paid version use per-job credits or subscriptions?
12. What content is permitted for benchmark fixtures and demonstrations?

## 28. Definition of Done for the Core Product

The core product is done when all of the following are true:

- A visitor can open the production website and immediately find the upload control.
- A valid MP3 can be uploaded directly to private storage.
- The visitor can choose a supported separation mode and one of five output formats.
- A job is created exactly once for a repeated request.
- The Modal worker processes the job asynchronously.
- The model is loaded from a pinned, validated checkpoint.
- Progress is visible and survives a browser refresh.
- Every generated stem has correct timing and a valid audio file.
- The user can preview individual stems and download a ZIP.
- Signed download URLs expire as documented.
- Failures are understandable and retryable.
- Inputs, outputs, and callbacks are authorized.
- Guest abuse cannot create unbounded GPU spend.
- Expired files are deleted automatically.
- Logs and metrics can explain a failed or slow job.
- The measured p90 time for supported tracks meets the published target.
- Production deployment of the web app and worker is independent and documented.
- Privacy, terms, copyright, model-license, and retention requirements are reviewed.

## 29. Recommended First Build Order

For fastest risk reduction, implement in this order:

1. Local worker separation of a short legal fixture.
2. Model benchmark and duration-limit decision.
3. Web upload UI with a fake worker.
4. Direct object storage upload.
5. Real database job state and polling.
6. Modal staging deployment.
7. Real end-to-end job with MP3 output.
8. All five output formats.
9. Result previews and ZIP downloads.
10. Rate limits, cleanup, and failure recovery.
11. Full-stem mode if it passes benchmarks.
12. YouTube mode after legal and operational review.
13. Public beta and monitoring.
14. Accounts and billing after usage and cost data exists.

This order keeps the user experience visible from the beginning while testing the highest-risk part, GPU model execution, before investing in secondary features.

## 30. Detailed System Design and Runtime Flow

### 30.1 What the user sees

The product appears to be one website:

```text
https://your-domain.com
```

The user does not see separate Vercel and Modal applications. The browser communicates with the Next.js application, and the Next.js application communicates with the worker and storage services behind the scenes.

The visible experience is:

```text
Home page
  -> choose audio
  -> upload
  -> choose separation options
  -> start job
  -> processing page
  -> result page
  -> preview or download stems
```

### 30.2 Responsibility boundaries

```text
Browser
- Selects files and options.
- Uploads source directly to object storage.
- Starts a job through the web API.
- Polls the public job status endpoint.
- Requests signed download URLs.

Vercel / Next.js
- Authenticates and validates requests.
- Owns the public job ID and user/session access.
- Stores job metadata and state in Postgres.
- Creates presigned storage URLs.
- Starts the Modal worker.
- Receives signed worker events.
- Authorizes and signs result downloads.

Object storage
- Stores source audio and generated outputs.
- Remains private.
- Serves large files directly to browser and worker through signed URLs.

Modal
- Owns the long-running processing execution.
- Runs yt-dlp when the source is a YouTube URL.
- Runs ffprobe and audio preprocessing.
- Runs GPU model inference.
- Encodes, packages, and uploads results.
- Sends progress and terminal events back to Vercel.

Postgres
- Stores job state, ownership, configuration, output metadata, events, and usage data.
- Never stores the audio bytes themselves.
```

### 30.3 System components

```text
                         +----------------+
                         |  User browser  |
                         +--------+-------+
                                  |
                          HTTPS JSON/API
                                  |
                         +--------v-------+
                         | Next.js/Vercel |
                         |                |
                         | UI + API routes|
                         +---+--------+---+
                             |        |
                 SQL metadata|        |signed URLs
                             |        |
                   +---------v--+  +--v----------------+
                   | Neon        |  | Private object    |
                   | Postgres    |  | storage           |
                   +---------+---+  +---------+----------+
                             ^                ^
                             | callbacks     | source/results
                             |                |
                         +---+----------------+---+
                         | Modal worker            |
                         | CPU + GPU pipeline     |
                         |                         |
                         | download -> validate   |
                         | -> infer -> encode     |
                         +-------------------------+
```

### 30.4 Job state machine

The application should use a finite set of states. Every state transition must be validated on the server.

```text
created
  |
  v
queued -----> failed
  |
  v
starting ----> failed
  |
  v
downloading -+----> failed
  |            |
  v            v
validating --> processing
                 |
                 +----> cancel_requested -> canceled
                 |
                 v
              encoding
                 |
                 v
              uploading
                 |
                 +----> failed
                 |
                 v
             completed
                 |
                 v
              expired
```

Recommended public statuses:

- `queued`: accepted by the web app, waiting for worker execution.
- `processing`: worker is active; the public stage explains what is happening.
- `completed`: all expected outputs passed validation and are available.
- `failed`: processing ended without usable results.
- `canceled`: processing was canceled or stopped after a cancellation request.
- `expired`: source and result objects are no longer available.

Internal stages can be more detailed:

- `starting`.
- `downloading`.
- `validating`.
- `preparing_audio`.
- `separating`.
- `encoding`.
- `uploading_results`.
- `cleanup`.

No job may move backward from a terminal state. Duplicate events for the current state are safe no-ops.

### 30.5 Upload request flow

This is the main product flow for an MP3 upload.

```text
1. Browser -> Vercel: POST /api/uploads/presign
2. Vercel -> Postgres: optionally record upload session
3. Vercel -> Browser: short-lived upload URL + upload ID
4. Browser -> Object storage: PUT audio bytes directly
5. Browser -> Vercel: POST /api/jobs
6. Vercel -> Object storage: verify object exists and size is valid
7. Vercel -> Postgres: create app job as queued
8. Vercel -> Modal: launch worker with app job ID + source reference
9. Modal -> Vercel: accepted response with worker call ID
10. Vercel -> Postgres: save worker call ID
11. Vercel -> Browser: app job ID + status URL
12. Browser -> Vercel: poll GET /api/jobs/{jobId}
13. Modal -> Object storage: read source
14. Modal -> GPU: run separation model
15. Modal -> Object storage: upload stems and ZIP
16. Modal -> Vercel: signed completed event
17. Vercel -> Postgres: save outputs and mark completed
18. Browser -> Vercel: request a download
19. Vercel -> Object storage: create short-lived signed URL
20. Vercel -> Browser: redirect or return signed URL
21. Browser -> Object storage: download the file directly
```

The critical design rule is that Vercel handles control-plane traffic and object storage handles data-plane traffic. Large audio bytes should not travel through a Vercel function.

### 30.6 YouTube request flow

YouTube is a second source type that follows the same job lifecycle.

```text
1. User selects Import from YouTube.
2. Browser -> Vercel: submit URL, mode, and output format.
3. Vercel parses the URL and checks the approved hostname allowlist.
4. Vercel -> Postgres: create queued job with source_type=youtube.
5. Vercel -> Modal: launch worker with the validated URL.
6. Modal -> YouTube: download audio with controlled yt-dlp arguments.
7. Modal -> ffprobe: validate actual duration, size, codec, and channels.
8. Modal -> GPU: run the selected separation model.
9. Modal -> Object storage: upload outputs.
10. Modal -> Vercel: signed progress/completion event.
11. Browser polls the same public job endpoint as upload jobs.
```

The worker must validate the actual downloaded media because a valid-looking URL does not guarantee that the media is available, short enough, or audio-only. If validation fails, the worker stops before GPU inference.

### 30.7 Web-to-worker handoff

The handoff should use a thin authenticated Modal HTTP endpoint:

```text
POST /run
Authorization: Bearer <launch secret>

{
  "appJobId": "job_...",
  "source": {
    "type": "upload",
    "objectKey": "sources/upl_.../input.mp3"
  },
  "mode": "vocals_instrumental",
  "outputFormat": "mp3",
  "callbackUrl": "https://your-domain.com/api/internal/worker-events"
}
```

Modal should immediately acknowledge the request after starting the background execution:

```json
{
  "accepted": true,
  "workerCallId": "internal-worker-reference"
}
```

The web app stores the worker reference but never gives it to the browser.

The long-running function then executes independently:

```text
Modal HTTP endpoint
  -> validate launch secret
  -> validate payload
  -> start background Modal function
  -> return accepted response

Background Modal function
  -> emit started event
  -> retrieve/download source
  -> validate audio
  -> run model
  -> encode and upload outputs
  -> emit completed or failed event
```

The exact Modal SDK invocation method must be confirmed against the pinned SDK version during implementation. The architecture does not depend on exposing Modal's internal call lookup mechanism to the browser.

### 30.8 Why the browser polls Vercel

The browser should poll Vercel rather than Modal directly because Vercel is the public authority for:

- Job ownership.
- Guest access tokens.
- Account authorization.
- Sanitized public errors.
- Output expiry.
- Rate limiting.
- Stable API behavior if the worker provider changes later.

Polling example:

```text
GET /api/jobs/job_123

queued      -> wait 2 seconds
processing  -> wait 2 to 3 seconds
completed   -> stop and show results
failed      -> stop and show retry
expired     -> stop and show expiration message
```

The worker callback updates Postgres. The next browser poll reads the new state. This avoids making the browser aware of Modal credentials or implementation details.

### 30.9 Callback and state update flow

```text
Modal worker
  -> construct event JSON
  -> add event ID and timestamp
  -> HMAC-sign timestamp + raw body
  -> POST to Vercel callback route

Vercel callback route
  -> read raw request body
  -> verify timestamp freshness
  -> verify HMAC signature
  -> validate event schema
  -> insert event ID if new
  -> lock the job row or use a transactional update
  -> verify allowed state transition
  -> update job/output records
  -> return 2xx
```

For a completed event, the transaction should:

1. Verify the job is not canceled or already expired.
2. Verify every output listed in the manifest has expected metadata.
3. Insert output rows.
4. Set `jobs.status=completed`, `progress=100`, and `completed_at`.
5. Set output expiry.
6. Record usage data.
7. Commit once.

For a failed event, the transaction should:

1. Store the stable public error code.
2. Store a private diagnostic reference.
3. Mark the job failed unless it is already terminal.
4. Avoid exposing worker stack traces.
5. Preserve enough metadata for support.

### 30.10 Data lifecycle

```text
Source upload
  -> private source object
  -> worker reads source
  -> source deleted after success or source TTL

Separated stems
  -> private individual stem objects
  -> private ZIP object
  -> signed URL generated on demand
  -> objects deleted at expiry

Job metadata
  -> created and updated during processing
  -> retained according to privacy/support policy
  -> marked expired when files disappear
```

The database is the source of truth for job state. Object storage is the source of truth for binary file availability. The application must handle the case where metadata says `completed` but an object has already expired by returning a clear `expired` result instead of a broken download.

### 30.11 Failure paths

#### Upload failure

```text
Presign succeeds -> storage upload fails
  -> browser displays retry
  -> abandoned upload is eventually deleted
  -> no processing job is created
```

#### Worker launch failure

```text
Job created -> Modal launch fails
  -> transaction or recovery path marks launch_failed
  -> retry launch with bounded attempts
  -> show service-busy error if retries are exhausted
```

#### Invalid audio

```text
Worker starts -> ffprobe rejects source
  -> no model invocation
  -> failed event: INVALID_AUDIO or LIMIT_EXCEEDED
  -> temporary files removed
  -> user can submit another file
```

#### Model failure

```text
Worker starts inference -> model/runtime error
  -> capture private diagnostics
  -> clean temporary files
  -> emit MODEL_FAILED
  -> no partial result is presented as complete
```

#### Output failure

```text
Inference succeeds -> encoding/upload fails
  -> delete partial output objects
  -> emit OUTPUT_FAILED
  -> mark job failed
  -> allow retry
```

#### Callback failure

```text
Worker completes -> callback request fails
  -> retry callback with exponential backoff
  -> keep event ID stable across retries
  -> reconciliation job detects jobs stuck in processing
  -> status is repaired from worker or marked failed
```

#### Browser closes

```text
Browser closes during processing
  -> job continues independently
  -> user returns to job URL
  -> browser reads current state
  -> results remain available until expiry
```

### 30.12 Time budget model

For a supported track, total time is:

```text
T_total = upload/download
        + queue delay
        + worker cold start
        + input validation
        + model inference
        + encoding
        + result upload
```

The five-minute target should be measured as a percentile, not an absolute guarantee. Track each component separately. The system should reject or defer jobs when the current queue or worker capacity cannot meet the configured service target.

Use an internal deadline for every job. For example:

```text
job deadline = created_at + configured maximum processing window
```

A watchdog should mark jobs failed or retryable when they exceed the deadline. A user should never see an infinite spinner.

### 30.13 Scaling behavior

At low traffic:

- Vercel serves the UI and API.
- Modal scales GPU workers toward zero.
- Model weights remain in the persistent cache volume.
- Storage and database usage remain small.

At moderate traffic:

- Jobs queue in Postgres-backed state.
- Modal concurrency is capped to control cost.
- The web app continues responding quickly because it does not wait on inference.
- The UI reports queueing rather than pretending a worker has started.

At high traffic:

- Add a dedicated queue if Postgres polling is no longer sufficient.
- Add account/credit requirements.
- Add worker pools by model mode.
- Add priority and fair-use policies.
- Add cost-based admission control.

Do not scale GPU concurrency without measuring VRAM, queue delay, failure rate, and cost.

### 30.14 Control-plane versus data-plane rule

This distinction should guide implementation:

Control plane:

- JSON requests.
- Job IDs.
- State transitions.
- Progress events.
- Authentication.
- Quotas.
- Signed URL creation.

Data plane:

- Input audio bytes.
- Stem audio bytes.
- ZIP files.
- Direct storage transfer.

Control-plane traffic may pass through Vercel. Data-plane traffic should use direct signed transfers between browser/worker and object storage.

### 30.15 User journey with infrastructure hidden

```text
User opens website
  |
  +--> chooses song
  |      |
  |      +--> direct upload with progress
  |
  +--> selects "Vocals + instrumental"
  |
  +--> selects MP3
  |
  +--> clicks "Separate audio"
  |
  +--> sees "Preparing audio"
  +--> sees "Separating stems"
  +--> sees "Preparing downloads"
  |
  +--> sees Vocals and Instrumental players
  +--> previews a stem
  +--> downloads one stem or all stems
  +--> sees expiration information
```

At no point does the user need to choose a cloud provider, create a Modal account, configure a Python environment, understand model names, or manually move files between services.

## 31. System Design Acceptance Criteria

- The public product is one website with one primary upload flow.
- Vercel handles only short-lived control-plane requests.
- Large audio files bypass Vercel through direct storage transfers.
- The web app owns public job identity and authorization.
- Modal owns long-running audio processing.
- The worker can be redeployed without changing the user-facing URL or API contract.
- The browser never calls Modal directly with credentials.
- Every job has an explicit state and deadline.
- Worker callbacks are signed, idempotent, and schema-validated.
- Failed jobs cannot remain in a permanent processing state.
- Results are private, signed on demand, and automatically expired.
- The system can process both uploaded files and approved YouTube URLs through the same job lifecycle.
- Model, format, duration, and concurrency choices are configuration-driven and benchmarked.

## 32. Separation Engine Design

This section defines the engine that turns one mixed music track into separate audio stems. It is deliberately independent from the web UI, database, and deployment provider. The engine should be callable locally, from a test harness, or from a Modal worker with the same core interface.

### 32.1 Engine objective

Input:

```text
One audio file containing a mixed music track
```

Output:

```text
A named set of time-aligned audio stems
```

For the default mode:

```text
vocals.wav
instrumental.wav
```

For a full-stem mode:

```text
vocals.wav
drums.wav
bass.wav
other.wav
```

The engine must preserve:

- Track duration.
- Start time.
- Channel configuration according to the output policy.
- Sample alignment across all stems.
- A documented sample rate and sample format.

The engine must not promise that the output is a mathematically perfect reconstruction or that it can recover information removed by a lossy source file.

### 32.2 Engine layers

```text
Engine API
  -> request validation
  -> source inspection
  -> audio decode and normalization
  -> model routing
  -> model loading/cache
  -> chunk planning
  -> GPU inference
  -> overlap-add reconstruction
  -> optional mixture consistency
  -> output safety checks
  -> master WAV writing
  -> format encoding
  -> manifest and result validation
```

Each layer must have one responsibility. Model-specific code belongs behind an adapter; the pipeline must not contain Demucs- or RoFormer-specific tensor logic.

### 32.3 Engine API

The core API should look conceptually like this:

```python
result = engine.separate(
    input_path="/tmp/input.wav",
    mode="vocals_instrumental",
    output_format="mp3",
    options=SeparationOptions(
        device="cuda",
        model_policy="production_default",
        keep_master_wav=False,
    ),
    progress_callback=on_progress,
)
```

Suggested result shape:

```python
SeparationResult(
    stems={
        "vocals": StemArtifact(path="...", duration_seconds=214.2),
        "instrumental": StemArtifact(path="...", duration_seconds=214.2),
    },
    archive_path="...",
    manifest_path="...",
    engine_metadata={
        "mode": "vocals_instrumental",
        "model_id": "pinned-model-id",
        "model_revision": "pinned-revision",
        "sample_rate": 44100,
        "duration_seconds": 214.2,
    },
)
```

The public engine API should return artifact metadata, not raw giant tensors. Tensor operations stay inside the worker process and are released before the encoding phase when possible.

### 32.4 Request validation

Before loading a model:

1. Confirm the input path is inside the job's private temporary directory.
2. Confirm the file exists and is below the configured byte limit.
3. Run `ffprobe` and parse JSON output.
4. Confirm at least one audio stream exists.
5. Reject video-only, subtitle-only, image-only, and malformed files.
6. Confirm duration is positive and below the mode-specific limit.
7. Confirm the decoded stream has a supported channel count.
8. Confirm the file is not empty or obviously corrupted.
9. Check for excessive metadata or suspiciously large declared values.
10. Create a source checksum for diagnostics and deduplication.

Do not trust the extension or browser MIME type. The decoded media properties are authoritative.

### 32.5 Audio canonicalization

The model should always receive a canonical tensor representation, regardless of the source format.

Recommended internal representation:

```text
Shape: channels x samples
Type: float32 during baseline implementation
Range: approximately -1.0 to +1.0
Layout: stereo unless the selected model explicitly supports another layout
Sample rate: the model's required sample rate
```

Canonicalization steps:

1. Decode with FFmpeg or a tested audio library.
2. Convert to the model's required sample rate.
3. Convert mono to the model's expected channel layout.
4. Keep stereo channels separate; do not downmix by default.
5. Preserve exact sample count after resampling in metadata.
6. Record the original sample rate, channels, duration, and codec.
7. Avoid normalization that changes the artistic dynamics unless required by the model.
8. Use a high-quality resampler with documented settings.

For very quiet or clipped sources, continue processing but record a warning. Do not silently apply mastering or loudness normalization.

### 32.6 Model router

The router maps a user-facing mode to a validated engine profile:

```text
vocals_instrumental
  -> two-source adapter
  -> selected production vocal/instrumental checkpoint

full_stems
  -> multi-source adapter
  -> selected production multi-stem checkpoint
```

A profile should contain:

```text
model_id
model_revision
checkpoint_uri or cache key
expected_stems
required_sample_rate
required_channels
preferred_device
chunk_length
overlap
batch_size
precision_policy
license_reference
```

The user selects a mode, not a raw checkpoint. Only an allowlisted model profile can be executed.

### 32.7 Candidate model strategy

The engine should support multiple adapters so models can be compared and upgraded safely.

Initial adapter candidates:

- A BS-RoFormer-family adapter for high-quality vocals/instrumental output.
- A Demucs-family adapter for multi-source output.
- A compatibility adapter around a maintained source-separation library if it provides a better validated model or simpler production maintenance.

Model selection must be evidence-based. The engine release process should require:

- A pinned source repository commit or package version.
- A checkpoint checksum.
- A model and weights license record.
- Quality evaluation results.
- Cold-start and warm-run timings.
- Peak VRAM measurement.
- Expected stem-name mapping.
- A rollback profile.

A new model is not production-ready merely because it is newer or has a higher score on one benchmark.

### 32.8 Model loading and cache

Model loading is expensive and must be separated from per-track processing.

At container initialization:

1. Resolve the configured model profile.
2. Check the persistent model volume for the exact checkpoint checksum.
3. Download the checkpoint only if absent and only from an allowlisted source.
4. Verify the checksum before loading.
5. Load the model onto the target device.
6. Switch to evaluation mode.
7. Disable gradients.
8. Run a short warm-up inference with a tiny valid tensor.
9. Record load and warm-up times.

At job completion:

- Release per-job tensors.
- Keep the model resident in the warm container.
- Never let one job's input or output tensor be reused by another job.

If the two modes require too much memory together, create separate Modal worker classes or profiles. Do not load both models into the same GPU process by default.

### 32.9 Chunk planning

A multi-minute track should not be passed as one uncontrolled tensor. The engine must use bounded windows with overlap.

Conceptual algorithm:

```text
full waveform
  -> calculate window positions
  -> pad final window if necessary
  -> infer each window
  -> apply overlap weighting
  -> add predictions into output buffers
  -> divide by accumulated weights
  -> trim padding to original sample count
```

Chunk settings are model-specific and must be read from the model profile. The engine must not blindly use one window length for every model.

Requirements:

- Use overlap sufficient for the selected model's receptive field.
- Use a stable weighting window such as the library-recommended crossfade/window.
- Ensure every input sample receives non-zero accumulated weight.
- Keep output buffers bounded and use disk-backed intermediates only if memory requires it.
- Emit progress based on completed chunks when chunk counts are reliable.
- Validate that the reconstructed output has exactly the expected sample count.

Prefer the official or maintained library's tested `apply_model`/inference path when it already implements chunking and overlap-add correctly. Custom reconstruction is only justified when it is covered by audio regression tests.

### 32.10 GPU inference loop

The baseline inference loop is:

```python
model.eval()
with inference_mode():
    for chunk in planned_chunks:
        input_tensor = chunk.to(device, non_blocking=True)
        with configured_autocast_if_validated():
            prediction = model(input_tensor)
        prediction = prediction.float().cpu()
        accumulator.add(prediction)
        release(input_tensor, prediction)
```

The exact model call is adapter-owned because model output shapes differ.

Inference requirements:

- Use inference-only execution.
- Start with float32 for correctness; enable mixed precision only after regression tests.
- Do not use a batch size that can exceed VRAM under a maximum-length input.
- Keep GPU synchronization points measurable but minimal.
- Catch out-of-memory errors and classify them separately from bad input.
- Never silently retry with a different model or lower quality setting unless the fallback is explicitly configured.
- Clear per-job GPU memory after a failure.

### 32.11 Mixture consistency

Many source-separation models produce stems whose sum is close to, but not exactly, the input mixture. The engine may apply a documented mixture-consistency projection:

```text
residual = mixture - sum(predicted_stems)
corrected_stem_i = predicted_stem_i + allocation_i * residual
```

This must be optional and model-profile-specific. Possible allocations include equal allocation or energy-weighted allocation, but the choice must be evaluated because forcing consistency can increase artifacts in some models.

Rules:

- Never apply it by default without an A/B listening test.
- Preserve the original mixture for comparison.
- Record whether it was applied in the manifest.
- Verify that the correction does not create clipping or loudness jumps.

For a two-source result, the instrumental stem may be computed as the model's direct instrumental prediction or as mixture minus vocals depending on the selected model. That choice must be fixed per model profile and benchmarked.

### 32.12 Postprocessing policy

Postprocessing should be conservative. The engine should not attempt to hide separation artifacts by aggressively denoising or mastering the stems.

Allowed baseline operations:

- Trim only engine-added padding.
- Restore expected sample count.
- Clamp or reject invalid floating-point values.
- Apply a tiny safety limiter only if required to prevent encoder overflow, and document it.
- Preserve channel count and timing.
- Write a high-quality temporary WAV master.

Not enabled by default:

- Loudness normalization.
- Noise reduction.
- Artificial stereo widening.
- EQ or compression.
- Silence removal.
- Automatic fade-in/fade-out.
- Dynamic range processing.

Users should receive separated stems, not altered mixes.

### 32.13 Stem quality gates

Before encoding, each stem must pass:

- Finite-value check: no NaN or infinity.
- Shape check: expected channels and sample count.
- Duration check: within a small tolerance of the source.
- Peak check: no unexpected extreme values.
- Empty/silent check according to mode policy.
- File-write check for the temporary master.

The engine should calculate diagnostics such as:

- Peak amplitude.
- RMS or integrated loudness for internal diagnostics.
- DC offset.
- Duration.
- Number of clipped samples.
- Residual energy if mixture consistency is evaluated.

A silent stem is not always an error, so silence should produce a warning unless the selected mode requires meaningful content.

### 32.14 Encoding pipeline

The engine should separate once into a high-quality master representation, then encode the user's selected format from that master.

```text
model output tensors
  -> validated temporary WAV masters
  -> selected encoder
  -> encoded stem files
  -> ffprobe verification
  -> ZIP packaging
```

This prevents each output format from triggering another model run.

Format policy:

```text
mp3 -> LAME with configured high-quality bitrate
wav  -> PCM with documented bit depth and sample rate
flac -> lossless compression
ogg  -> documented Vorbis or Opus choice
m4a  -> AAC in an M4A container with configured bitrate
```

The exact command arguments belong in one encoder module, not scattered across the pipeline. Use subprocess argument arrays and capture stderr for private diagnostics.

After encoding:

1. Run `ffprobe` on every output.
2. Verify the expected container and codec.
3. Verify nonzero size.
4. Verify duration within tolerance.
5. Verify the expected number of channels.
6. Compute checksum.
7. Only then upload the artifact.

### 32.15 Naming and manifest rules

Use deterministic, sanitized names derived from a server-generated job directory:

```text
song__vocals.mp3
song__instrumental.mp3
song__drums.mp3
song__bass.mp3
song__other.mp3
manifest.json
```

The original filename may be used as a display-name prefix after removing path separators, control characters, and unsupported characters. It must never control a path outside the job directory.

Manifest fields:

```json
{
  "schemaVersion": 1,
  "jobId": "job_...",
  "source": {
    "displayName": "song.mp3",
    "durationSeconds": 214.2,
    "sampleRate": 44100,
    "channels": 2
  },
  "separation": {
    "mode": "vocals_instrumental",
    "modelId": "pinned-model-id",
    "modelRevision": "pinned-revision",
    "mixtureConsistency": false
  },
  "output": {
    "format": "mp3",
    "stems": [
      { "name": "vocals", "durationSeconds": 214.2, "sha256": "..." },
      { "name": "instrumental", "durationSeconds": 214.2, "sha256": "..." }
    ]
  }
}
```

Never put storage credentials, signed URLs, raw source URLs, or callback secrets into the manifest.

### 32.16 Engine failure handling

Classify failures so the web app can show useful messages:

```text
INVALID_AUDIO
LIMIT_EXCEEDED
DOWNLOAD_FAILED
MODEL_LOAD_FAILED
GPU_OUT_OF_MEMORY
INFERENCE_FAILED
OUTPUT_ENCODING_FAILED
OUTPUT_VALIDATION_FAILED
STORAGE_UPLOAD_FAILED
CANCELED
TIMEOUT
UNKNOWN
```

Retry policy:

- Do not retry invalid input.
- Do not retry a limit violation.
- Retry transient source download failures with bounded attempts.
- Retry storage upload failures with bounded attempts.
- Retry worker startup failures outside the engine.
- Do not blindly retry GPU out-of-memory errors with the same settings.
- A failed job must clean local files and delete partial remote outputs.

### 32.17 Cancellation and deadlines

The engine should accept a cancellation token and deadline:

```python
engine.separate(..., cancellation_token=token, deadline=deadline)
```

Check them:

- Before downloading the next source chunk.
- Before starting each inference chunk.
- After each inference chunk.
- Before each encode operation.
- Before uploading each result.

Cancellation may not interrupt a single GPU kernel immediately. It must stop at the next safe boundary and report `CANCELED` rather than presenting partial output as complete.

Every job needs:

- Maximum wall-clock deadline.
- Maximum source duration.
- Maximum byte limit.
- Maximum temporary disk budget.
- Maximum output object budget.

### 32.18 Determinism and reproducibility

Record and pin:

- Python version.
- CUDA and PyTorch versions.
- Separation package versions.
- FFmpeg version.
- Model repository commit.
- Model checkpoint checksum.
- Input checksum.
- Preprocessing settings.
- Chunk and overlap settings.
- Precision policy.
- GPU type.

Where supported, configure deterministic behavior for evaluation. Production may use the fastest validated kernels if output quality remains stable, but the choice must be recorded.

A model update is a new engine release. Never replace a checkpoint in place without changing the model revision and running the benchmark suite.

### 32.19 Engine caching strategy

Cache only data that is safe to share:

Safe to cache:

- Validated model checkpoints.
- Read-only model configuration.
- Static encoder configuration.

Do not share between jobs:

- Input tensors.
- Output tensors.
- Temporary audio files.
- Signed URLs.
- User-specific metadata.
- Partial ZIP archives.

Use a unique job namespace for all temporary and remote objects. Remove job data after the configured lifecycle window.

### 32.20 Local CLI for development

Provide a local command that exercises the same engine:

```text
python -m worker.cli separate \
  --input ./fixtures/song.mp3 \
  --mode vocals_instrumental \
  --format mp3 \
  --output ./artifacts
```

The CLI should support:

- CPU mode for small test fixtures when feasible.
- GPU mode for real performance tests.
- JSON progress output for automation.
- A dry-run validation command.
- Printing model and preprocessing metadata.
- No remote upload by default.

The CLI is essential for debugging model quality without involving Vercel or a browser.

### 32.21 Engine test suite

Unit tests:

- Model profile validation.
- Router allowlist behavior.
- Chunk position calculation.
- Padding and trimming.
- Overlap weighting.
- Sample-count preservation.
- Mixture-consistency math.
- Filename sanitization.
- Encoder argument construction.
- Manifest schema.
- Error classification.

Audio regression tests:

- Run a fixed legal fixture through each production profile.
- Confirm expected stems exist.
- Confirm durations match.
- Confirm output files decode.
- Compare checksums only when deterministic behavior is guaranteed.
- Otherwise compare numerical/audio metrics with tolerances.
- Detect unexpected loudness or peak changes.

Failure-injection tests:

- Corrupt source.
- Download timeout.
- Missing model checkpoint.
- Bad checkpoint checksum.
- GPU out-of-memory.
- FFmpeg failure.
- Storage upload failure.
- Callback failure.
- Cancellation during each major stage.

Performance tests:

- 30-second, 3-minute, and maximum supported fixtures.
- Cold and warm model load.
- Each separation mode.
- Each output format.
- One and multiple concurrent jobs.
- Peak RAM, VRAM, disk, and total time.

### 32.22 Engine release gates

A model or engine release may ship only when:

- The worker image builds from pinned dependencies.
- The model checkpoint checksum matches the release record.
- All unit and integration tests pass.
- Every output format passes decode validation.
- No stem has a sample-count or duration mismatch.
- No new clipping, NaN, or infinity issue is present.
- Quality evaluation is equal to or better than the current profile, or the tradeoff is explicitly approved.
- Warm-run p90 meets the current time budget.
- Cold-start behavior is within the documented operational budget.
- The license and attribution records are complete.
- Rollback to the previous validated profile has been tested.

### 32.23 Engine acceptance criteria

- The engine separates a supported input into the expected named stems.
- The default mode is optimized for vocals/instrumental simplicity and quality.
- The model is loaded once per warm worker container.
- Chunking and overlap-add preserve the full track without audible boundary defects in regression tests.
- Output stems remain aligned and have matching duration.
- All five requested formats can be produced from one separation pass.
- Output artifacts are validated before upload.
- Partial failures do not produce misleading completed jobs.
- Cancellation and deadline checks work at safe boundaries.
- Model revisions and processing parameters are reproducible.
- The engine can be executed locally independently from Vercel.
- A new model can be added behind an adapter and profile without rewriting the API or UI.
