# Spotify Input Pipeline — Design Plan

Status: **S1–S4 SHIPPED** — link policy, kill switch and metadata probe (S1),
the supervised audio fetch (S2), the operator login and packaging (S2.5), the
worker's end-to-end `source_type="spotify"` path (S3), and the web UI, database
migration and contracts (S4). Only S5 (a real-credential verification pass,
which needs a Premium account) is left. Everything below describes the target
design, and the deltas each milestone made to it are listed under Section 8.

## 1. Product goal

A third source tab next to Upload and YouTube: paste a Spotify track/album/
playlist link, Stemify resolves the real audio, and the normal separation job
runs. The job page, ZIP naming, analysis, and downloads all work exactly as
they do today because the pipeline downstream of "we have an audio file" is
source-agnostic.

### 1.1 Landscape note: the SpotSaver/SpotiDownloader pattern (evaluated, rejected for audio)

Browser tools like SpotSaver and spotidownloader.com advertise "Spotify links
to 320kbps MP3" but do NOT download Spotify audio. Confirmed mechanism (from
site disclaimers and independent teardowns): the link is resolved to metadata
via Spotify's public surface, the song title is searched on YouTube, that
audio is downloaded, transcoded, and stamped with the Spotify artwork/tags.
The structural tell: none of these sites ever asks for a Spotify login —
real Spotify audio requires Premium authentication, so without an account
they cannot be fetching Spotify audio. spotidownloader.com's own footer
admits it: "content provided by Third-Party Services outside of Spotify".
Users of these tools routinely receive the wrong master (sped-up, live,
cover, remaster) because the match is by title string, not by recording.
The "320kbps" label is a re-encode of ~128-160kbps source audio.

Why this is the wrong audio path for Stemify specifically:

- Separation quality is bounded by source fidelity. A loudness-normalized,
  lossy-re-encoded YouTube rip (sometimes a sped-up or wrong master) is the
  worst-case input for a separator; bleeds and artifacts amplify.
- Stemify already has first-class YouTube input — a Spotify-link-to-YouTube
  detour adds a failure point and arrives at the same (worse) audio.
- librespot streams the actual Spotify audio from the user's own Premium
  account: the cleanest source available, decoded exactly once.

What IS worth borrowing from that pattern (both go on the roadmap):

1. **Rich metadata embedding**: artist / album / artwork / release date written
   into the output files' tags (ID3/Vorbis) and the manifest — currently we
   only name files. Benefits every source type, not just Spotify.
2. **Playlist/album expansion**: a playlist link fans out into N normal jobs
   (rate-limited); v1 stays single-track (Section 9 non-goals unchanged).

## 2. The two halves: metadata vs audio

Spotify is two completely different problems, and conflating them is the #1
design mistake:

| Concern | Source | Cost / auth | Terms |
|---|---|---|---|
| **Metadata** (title, artist, duration, artwork, ISRC, preview URL) | Official Spotify Web API via `spotipy` | Free developer app; client-credentials token | Public, documented, stable |
| **Audio** (the actual Ogg Vorbis stream) | `librespot` (open-source Spotify client protocol) | Requires a **Premium** account login | Personal use; gray area — same posture as the YouTube feature's acknowledgement checkbox |

Metadata is uncontroversial and free. Audio requires a Premium account and
carries the same personal-use-only policy stance as YouTube input. The UI
acknowledgement checkbox (already built for YouTube) is extended to cover
Spotify.

## 3. Architecture: a source-adapter interface

The worker already branches on `source_type` (`upload` | `youtube`) in exactly
one place — `job_loop.process_job`. Spotify adds `spotify` as a third value
behind the same seam:

```
job.source_type == "spotify"
    → spotify.py: resolve_link() → track IDs
    → spotify.py: fetch_metadata() (spotipy; title/artist for naming)
    → librespot_backend: stream audio to job temp dir (decode on the fly)
    → falls into the existing prepare_source() validation ladder unchanged
```

New worker module `worker/worker/spotify.py`, deliberately shaped like
`worker/worker/youtube.py`:

- `is_allowed_spotify_url(url)` — allowlist: `open.spotify.com`,
  `spotify.link`, `spotify:track:` URIs. Reject everything else.
- `STEMIFY_SPOTIFY_ENABLED` kill switch, default **off** until the user
  configures credentials (mirrors `STEMIFY_YOUTUBE_ENABLED`).
- Fixed-argument subprocess/protocol calls; never shell; user config only from
  the operator's own credentials file.
- `resolve_title(url)`-style best-effort naming probe → `Artist - Title`, fed
  into the existing `record_source_filename` naming path (roadmap A2 — the
  naming work just landed for YouTube generalizes for free).

## 4. Audio acquisition — librespot backend (the hard part)

Spotify serves 96/160/320 kbps Ogg Vorbis. There is no official download API;
the legitimate open-source path is librespot (Rust) or its Python port, which
implement the Spotify client protocol and can render a track to a local file
**when authenticated as a Premium account**.

Options, in preference order:

1. **librespot binary via subprocess** (like yt-dlp today): the worker shells
   out to a `librespot` executable with fixed args
   (`--backend pipe --format vorbis` → decode with ffmpeg). Pros: battle-tested
   Rust implementation, no Python ABI headaches. Cons: user must install one
   binary; we document where.
2. **librespot-python**: pip-installable, but less maintained; keep as
   fallback. Pros: `pip install`. Cons: fewer eyes, occasional protocol drift
   after Spotify server changes.

Either way the worker receives **Ogg Vorbis bytes**, decodes through the
existing ffmpeg ladder (`prepare_source` already accepts whatever ffmpeg can
decode), and everything downstream is identical.

Credential handling (operator-side only, never per-job):
- `STEMIFY_SPOTIFY_USERNAME` / `STEMIFY_SPOTIFY_PASSWORD` env vars, or a
  `data/spotify-credentials.json` (gitignored) holding the librespot cached
  credentials. The web app and database never see them.
- Device name `stemify-worker`; librespot requires a registered "device".

Anti-abuse guardrails (learned from the YouTube rollout):
- One Spotify job at a time per worker (already true — single-job loop).
- Same 100 MB upload-equivalent / 480 s duration caps after decode.
- Rate-limit: minimum gap between Spotify fetches (`STEMIFY_SPOTIFY_COOLDOWN_S`,
  default ~10 s) to avoid tripping account flags.
- Playlist/album links expand to at most N tracks per job — v1 scope is
  **single track**; batch queues are a follow-up that creates N normal jobs.

## 5. Web app changes (small)

- `source-picker.tsx`: third tab "Spotify link"; same acknowledgement checkbox
  pattern as YouTube (personal use, ToS).
- `lib/jobs.ts` + contracts: `source_type` gains `"spotify"`; URL allowlist
  mirrors the worker's (defense in depth, same as YouTube).
- `job-view.ts` / UI strings: "Spotify import" label; everything else
  (progress stages, naming, analysis, ZIP) inherits automatically.
- Status page shows "Resolving Spotify metadata → Streaming audio" stages via
  the existing `progressMessage` mechanism (lands for free with the stage-aware
  progress work).

## 6. Failure modes and public error codes

Reuses `DOWNLOAD_FAILED` with Spotify-specific public messages:

| Failure | Public behavior |
|---|---|
| Feature disabled / no credentials | Job fails fast: "Spotify input is not configured on this machine." |
| Not Premium / auth expired | "Spotify authentication failed. Check the worker's Spotify credentials." |
| Track unavailable in market / removed | "That track is not available." |
| Protocol/parse errors | Generic download-failed message; diagnostics in `job_events` only |

## 7. Legal / policy posture (Section 14 parity)

- Same personal-use-only stance as YouTube; acknowledgement checkbox required.
- Web API metadata usage is fully compliant (documented public API).
- Audio via librespot = the user's own Premium account streaming their own
  library; we do not distribute credentials, bypass DRM beyond what the open
  client library does, or enable redistribution. Keep model/checkpoint license
  discipline unchanged.
- README documents the posture; the kill switch stays default-off.

## 8. Milestones

| # | Deliverable | Tests |
|---|---|---|
| S1 | ✅ **shipped** `spotify.py` URL allowlist + kill switch + metadata probe | `tests/test_spotify.py`, 58 pure unit tests (no network) |
| S2 | ✅ **shipped** supervised fetch child + `download_audio` backend | `tests/test_spotify_fetch.py`, `tests/test_spotify_download.py` (fake client library, no network) |
| S2.5 | ✅ **shipped** operator login (`worker.cli spotify-login`) + packaging | `tests/test_spotify.py` (path resolution, login guard rails, `store_credentials=False`) |
| S3 | ✅ **shipped** `source_type="spotify"` end-to-end through `process_job` | `tests/test_spotify_integration.py`: full pipeline plus every refusal path (no URL, switched off, not signed in, disallowed link) |
| S4 | ✅ **shipped** web third tab, contracts, `source_type` rebuild migration | `jobs.test.ts` allowlist/persistence, `migration.test.ts` (spotify accepted, quality preserved), contracts request/status cases |
| S5 | Real-credential verification pass + README operator guide | Manual, reference machine |

S1–S4 are buildable and testable with zero Spotify access (subprocess stubs
write fixture audio, exactly like the YouTube tests). S5 is the only step that
needs a real Premium account, and it is a verification pass, not development.

### S2 deltas from this plan

- **The Rust `librespot` binary cannot fetch a track**, so it is not the audio
  backend. Its CLI is a Spotify Connect *receiver*: it authenticates, registers
  as a device, and then plays whatever a controller sends it through an output
  backend. There is no "download track X and exit" mode, so using it would mean
  running a receiver plus a Connect control loop racing against playback — a lot
  of machinery for a worse result. (Its `--passthrough` flag with the pipe
  backend gets raw audio *out*, but only while something drives playback.)
  It has no login advantage either — see S2.5, where the Python port does the
  OAuth flow itself — so the binary is not required at all.
- **The Python port (`pip install librespot`) is the backend**, because it is
  the only one of the two that can request one specific track's stream
  (`content_feeder().load(TrackId.from_uri(...))`). It reads both the Python and
  the Rust credential formats, so a login produced by the Rust CLI
  (`librespot --cache <dir> --enable-oauth`) still works if someone has one.
- **The fetch runs in a child process we own** (`python -m worker.spotify_fetch`)
  under the same supervision as yt-dlp: own process group, hard deadline, whole
  tree killed on expiry, stdout streamed for progress. The client library is
  alpha and holds protocol state, so a stalled stream must never be able to
  block the single-threaded worker.
- **The native Ogg Vorbis stream is kept as-is.** `.ogg` is already in the
  upload allowlist, so nothing is re-encoded before separation — the model sees
  exactly what Spotify served.
- **Progress is approximate, and only when it can be.** A stream's length is
  unknown until it ends, so the child reports cumulative bytes and the parent
  converts them with a bitrate estimate from the track duration, capped below
  100% (the child's exit is the real completion). With no duration available,
  no percentage is reported rather than a made-up one.
- **Failure detail for the operator, never the browser**: the child's exit code
  and stderr travel to `job_events` via the existing failure path.
- New env vars: `STEMIFY_SPOTIFY_CREDENTIALS_FILE` (default
  `data/spotify-credentials.json`), `STEMIFY_SPOTIFY_FETCH_TIMEOUT_SECONDS`
  (default 600), `STEMIFY_SPOTIFY_HTTP_TIMEOUT_SECONDS` (metadata, default 15).

### S2.5 deltas: operator login and packaging

- **The login is built into the worker, not delegated to the Rust binary**:
  `python -m worker.cli spotify-login` runs the library's own OAuth flow (PKCE,
  local callback on `http://127.0.0.1:5588/login`), prints and optionally opens
  the approval URL, and then writes the cached credentials. Re-running is a
  no-op that reuses the existing file. The URL is printed with `flush=True`:
  the flow blocks on the callback, so buffered output would show the operator
  nothing at all when stdout is not a terminal.
- **`credentials_path()` is the single source of truth** for that file, so the
  login can never write somewhere the fetch does not read: default
  `STEMIFY_DATA_DIR/spotify-credentials.json`, overridable with
  `STEMIFY_SPOTIFY_CREDENTIALS_FILE`.
- **The fetch child never writes credentials.** The library's default is
  `./credentials.json` under the process cwd with `store_credentials=True`, so
  every job would have dropped a secrets file into the source tree. The child
  now sets `store_credentials=False` explicitly and moves the session cache off
  `<cwd>/cache` to the credentials directory as well.
- **One session cache is shared by every concurrent fetch, and the sharing is
  safe by construction** (this is the follow-up to the bullet above, which
  originally only moved the directory). The location comes from
  `worker.spotify.cache_dir_path(credentials)`, so the login and every fetch
  cannot disagree about it; `ensure_cache_dir()` creates it with
  `exist_ok=True`, so two jobs starting at the same instant converge on one
  directory instead of one of them failing (`test_spotify_fetch.py` drives that
  race with real processes). The library's own clean-up is switched **off**
  (`set_do_cache_clean_up(False)`): it deletes entries older than a fixed
  threshold without knowing that a concurrent fetch is reading one, and one
  fetch has no business deciding what another one's cache may keep. Nothing in
  the worker ever writes to or deletes from the directory.
- **The reliance on the library not caching is pinned by tests, not a
  comment.** In librespot 0.0.10 `CacheManager` is an unimplemented stub and
  `Configuration.cache_dir` is never read, so a fetch leaves the shared
  directory exactly as it found it. The tests assert our side of that contract
  (the configuration handed over, a pre-existing entry surviving both a fetch
  and a re-login, and nothing extra appearing). If a future version starts
  writing, that is where a lock or a per-fetch subdirectory goes.
- **Packaging is a separate file** (`worker/requirements-spotify.txt`) instead
  of `requirements.txt`, for two concrete reasons: the feature cannot be
  exercised without a Premium account and an interactive login, and the client
  library declares `PyOgg` — which publishes Windows wheels only. librespot
  0.0.10 never imports PyOgg (verified: no reference anywhere in the package),
  so a platform without that wheel installs `--no-deps` plus the modules the
  code actually imports (`defusedxml`, `protobuf==3.20.1`, `pycryptodomex`,
  `requests`, `websocket-client`, `zeroconf`). Keeping it out of
  `requirements.txt` also keeps CI, which installs that file, from compiling
  PyOgg from source on Linux.
- **`worker.cli health` reports Spotify readiness** (`unavailable` /
  `disabled` / `signed out` / `ready`), in the same style as the YouTube line,
  so a misconfigured machine is diagnosable without reading logs.
- **Honest status: the UI could not create a Spotify job yet.** The operator
  side was ready and verified, but `source_type="spotify"` was not wired through
  `process_job` until S3, so the web app had no third tab at that point. S3/S4
  closed this; see the next section.

### S3/S4 deltas: the end-to-end path and the UI

- **`source_type` carried a CHECK constraint** (`IN ('upload', 'youtube')`), and
  SQLite cannot alter a CHECK — so accepting a Spotify job needed a table
  rebuild on every existing install, on both the web and worker sides. The
  existing Phase B rebuild was generalized to key on *either* stale enum (mode
  or source_type) rather than only the mode one.
- **The rebuild now projects the old table's actual columns** instead of a
  hand-written list. That list omitted `quality`, and a rebuild triggered by the
  source_type CHECK only ever runs on a database where `quality` already exists
  — so the hand-written version would have silently reset every stored preset to
  NULL. Both sides are pinned by a test that seeds a pre-Spotify database with
  `quality='fast'` and asserts it survives the rebuild.
- **Spotify failures carry their own public message.** The job loop maps error
  codes to user-facing text, so DOWNLOAD_FAILED would have shown "The download
  failed, or the source is not supported." for the two cases a Spotify job hits
  most: the kill switch is off, or there is no Premium login. `SpotifyError` now
  carries an optional public message for exactly those two, and everything else
  keeps the generic text — a distinction the code cannot make is not invented.
- **The tab is always shown; the worker decides.** The kill switch is worker-side
  state the web app cannot see, so gating the tab on a web env var would create a
  second, divergent source of truth. The tab exists, and an unconfigured machine
  fails the job with an explicit message.
- **Both link tabs share one form**, driven by a per-source config block (icon,
  labels, pattern, unsupported-link text, acknowledgement). Per-source copies of
  the same JSX are what drift; the acknowledgement text is source-specific and
  must not follow the user across tabs, so it resets on every tab switch — a
  YouTube acknowledgement can no longer submit a Spotify job.
- **The link field is `type="text"`, not `type="url"`.** The accepted policy
  includes `spotify:track:` URIs, which native URL validation would block before
  our own message could explain it.
- **`job-request.schema.json` also gained the `quality` property**, which the web
  app had been accepting since the quality picker landed. The schema is now
  truthful about the request body rather than rejecting a field the API accepts.
- **Naming reuses roadmap A2 unchanged**: the S1 metadata probe records
  `Artist - Title.ogg` into `jobs.source_filename`, and a failed probe falls back
  to the track id exactly as a failed YouTube title probe does.

### S1 deltas from this plan

- **No `spotipy` dependency.** Metadata is one client-credentials token request
  plus one `GET /v1/tracks/{id}`, so it uses stdlib `urllib` instead of pulling
  in a new dependency for two fixed calls. The seam (`_request_json`) is the
  only place that touches the network, which is what keeps the tests offline.
- **`spotify.link` short links are rejected** for now: resolving one needs a
  network redirect, so the worker cannot verify the target at job-creation
  time. Revisit with the playlist/album fan-out work.
- **v1 accepts `/track/<id>` links and `spotify:track:` URIs**, including the
  `/intl-xx/` locale prefix Spotify itself adds. Album/playlist links are
  rejected outright, per Section 9.
- **Kill switch defaults off**: `STEMIFY_SPOTIFY_ENABLED=1` opts in, because an
  unconfigured machine can serve neither metadata nor audio. Credentials are
  `STEMIFY_SPOTIFY_CLIENT_ID` / `STEMIFY_SPOTIFY_CLIENT_SECRET`.
- **Metadata is strictly best-effort.** Missing credentials, an unreachable
  API, or an unusable name all return `None` and naming falls back to the
  stored source filename — a Spotify job never fails because a *name* lookup
  failed.

## 9. Explicit non-goals (v1)

- Batch playlist separation (defer; create per-track jobs later)
- 320 kbps toggle (librespot default quality is fine; separation resamples to
  44.1 kHz canonical anyway)
- Storing Spotify credentials in the web app or DB
- Any redistribution of fetched audio
