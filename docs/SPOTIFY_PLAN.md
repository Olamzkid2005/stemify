# Spotify Input Pipeline — Design Plan (planning only, not implemented)

Status: **IN PROGRESS** — S1 (link policy, kill switch, metadata probe) and S2
(the supervised audio fetch) are shipped in `worker/worker/spotify.py` and
`worker/worker/spotify_fetch.py`; S3 onwards is unbuilt. Everything below
describes the target design, and the deltas S1/S2 made to it are listed under
Section 8.

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
| S3 | `source_type="spotify"` end-to-end through `process_job` | Extend `test_youtube_integration.py` pattern |
| S4 | Web: third tab, contracts, job-view labels | `createJob` allowlist tests + UI |
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
  What the binary **is** good at is the one-time interactive login: complete
  OAuth once with `librespot --cache <dir> --enable-oauth`, and the cached
  credentials are reused.
- **The Python port (`pip install librespot`) is the backend**, because it is
  the only one of the two that can request one specific track's stream
  (`content_feeder().load(TrackId.from_uri(...))`). It reads both the Python and
  the Rust credential formats, which is what lets the CLI-produced login above
  be reused as-is.
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
