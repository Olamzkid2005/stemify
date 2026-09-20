# Stemify worker (`worker/`)

Python worker that validates audio, runs GPU separation, encodes stems, and reports
progress to the web application. Product plan: `STEM_EXTRACTOR_PLAN.md` (Section 13, 32).

## Setup

### Standard: virtual environment

```bash
cd worker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

# Separation engine (plan Task 9) — optional for control-plane work:
pip install -r requirements.txt

# Spotify input (optional, needs a Premium account; see "Spotify input" below):
pip install -r requirements-spotify.txt
```

On Windows (bash) use `source .venv/Scripts/activate` instead.

### Windows with Microsoft Store Python: pip --target fallback

The Store Python cannot create venvs (its `venvlauncher.exe` lives under the
admin-only `C:\Program Files\WindowsApps` tree, so the copy into `.venv` fails
with "Access is denied"), and antivirus real-time scanning frequently
quarantines freshly extracted pip packages installed into `AppData`. If either
applies to your machine, install the dependency tree into the project instead
and put it on `PYTHONPATH`:

```bash
cd worker
py -3.13 -m pip install --target .runtime -r requirements.txt -r requirements-dev.txt

# Then prefix every command with the path, e.g.:
PYTHONPATH="$PWD/.runtime" py -3.13 -m pytest -q
PYTHONPATH="$PWD/.runtime" py -3.13 -m ruff check worker tests
```

`.runtime/` is gitignored. If your antivirus still interferes with the
install, whitelist the repository folder (or at least `worker/.runtime/`)
before retrying — otherwise installs come back silently incomplete.

### Torch version notes

The model stack is pinned: `demucs==4.0.1` with `torch>=2.5.1`.

- torch 2.5.1 works as-is but has no Python 3.13 wheels — use Python 3.12.
- torch >= 2.6 resolves an unset `weights_only` to `True`, which rejects demucs
  4.0.1 checkpoints. The adapter handles this itself:
  `worker/worker/models/demucs.py` wraps the checkpoint load in
  `_weights_only_compat()`, which forces `weights_only=False` for the duration
  of the load only. Checkpoint integrity is enforced independently (torch.hub
  `check_hash=True` plus the profile-checksum re-verification on every load),
  so no manual flags are needed. On Python 3.13 use `torch>=2.6`.
- For an NVIDIA GPU, install torch from the CUDA index first — see the header
  of `requirements.txt`.
- CI runs this matrix automatically (Python 3.12 + torch 2.5.1 and Python 3.13
  + torch 2.6.0, CPU wheels) — see `.github/workflows/ci.yml`.

### Model weights

The default profile (`demucs_default`, model `htdemucs`) downloads its pinned
checkpoint once through torch.hub with hash verification into
`data/models/hub/checkpoints/` (configurable via `STEMIFY_MODEL_DIR`). The
cache checksum is re-verified on every load; a mismatch stops the job with
`MODEL_LOAD_FAILED`. Checkpoint license: see `worker/worker/models/profiles.py`.

### Device selection

`STEMIFY_DEVICE=auto` (default) uses CUDA when a CUDA-enabled torch is
installed, otherwise CPU. Set `STEMIFY_DEVICE=cpu` or `STEMIFY_DEVICE=cuda` to
force one; forcing `cuda` without a CUDA device fails the job with
`MODEL_LOAD_FAILED`.

CPU performance for full-length tracks is not yet benchmarked (plan Task 18);
do not rely on it for production-length material yet.

### Quality vs speed

`STEMIFY_QUALITY=balanced` (default) tunes separation for cleaner stems.
Presets, applied to every mode including drum subdivision:

| Preset | overlap | shifts | Relative time | Use when |
|---|---|---|---|---|
| `fast` | 0.25 | 0 | ~0.35x | Quick previews, debugging |
| `balanced` | 0.4 | 2 | 1x | Everyday use |

A third `best` preset (0.45 overlap, 5 shifts, ~2.5x) was dropped: the extra
passes changed processing time far more than the result, so it was not worth
offering. Real quality gains need a different model, not more passes.

Set it once per worker run (e.g. `STEMIFY_QUALITY=fast python -m worker.job_loop`)
or in `start.sh`; the web UI can also set it per job (the job's preset wins).
An unknown value fails the job with `MODEL_LOAD_FAILED` rather than silently
processing at a surprise quality.

### Progress reporting

Separation reports progress as it works, so the job page moves through the
whole run instead of jumping from the start of inference to the end. Three
things are worth knowing when you touch the model adapters:

- demucs 4.0.1's `apply_model(shifts, split=True)` is one blocking call with no
  per-chunk hook. `worker/models/demucs.py` therefore replicates its two outer
  paths — the shift trick and the chunked split pass — so a callback can fire
  after every finished chunk. The pipeline maps that 0-1 fraction onto the
  separation stage's 30-75 percent window, then encoding runs 75-90 and
  packaging 90-98.
- The replication is guarded. `_apply_path_matches_installed()` compares the
  installed demucs source against the code we replicate; on any mismatch it
  falls back to plain `apply_model`, which is identical audio with coarse
  progress (the bar parks during inference). A coarse bar is recoverable, wrong
  audio is not — so the fallback, not a guess, is the correct default.
- Cost is chunk count times shift passes, not track length alone: a preset with
  `shifts=2` runs two full passes, so a 480 s track emits roughly 80 updates.
  Only whole percents are stored and repeated percentages are dropped, so a job
  adds at most 43 `job_events` progress rows instead of one per chunk.

`tests/test_engine_progress.py` pins this against the real checkpoint (exact
equality with `apply_model`, plus per-chunk and per-pass progress). It runs real
inference and takes ~2 minutes, so deselect it with `-k` while iterating.

### FFmpeg

The input pipeline needs `ffmpeg`/`ffprobe`. It looks for vendored static
binaries in `worker/bin/` first, then `PATH`. To vendor them (macOS Intel
example, from evermeet.cx):

```bash
mkdir -p worker/bin && cd worker/bin
curl -sLO https://evermeet.cx/ffmpeg/getrelease/zip && unzip ffmpeg.zip
curl -sLO https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip && unzip ffprobe.zip
chmod +x ffmpeg ffprobe
```

`worker/bin/` is gitignored — binaries are never committed.

## Run the local worker

The worker polls the shared SQLite queue (`$STEMIFY_DATA_DIR/stemify.sqlite3`, the
same file the web app writes) and processes one job at a time:

```bash
python -m worker.job_loop          # long-running loop (Ctrl+C stops after the current job)
```

Configuration: `STEMIFY_DATA_DIR` (defaults to `./data`) and
`STEMIFY_WORKER_POLL_MS` (default 500). Jobs left in `processing` by a crashed
run are failed safely on the next startup. Without the model stack installed,
jobs that pass validation stop with `MODEL_LOAD_FAILED`.

Warm model reuse: the worker keeps the loaded model in memory across jobs, so
only the first job in a worker run pays model-load cost.

### YouTube downloads

yt-dlp runs from a fixed argument array (no shell) in its own process group
under a hard timeout: `YT_DLP_PROBE_TIMEOUT_SECONDS` (default 60) for the title
lookup and `YT_DLP_TIMEOUT_SECONDS` (default 300) for the download. On expiry
the whole process tree is killed — yt-dlp spawns ffmpeg for the MP3
post-process, and killing only the parent used to leave that child holding the
pipes, which blocked the single-threaded worker indefinitely. Download percent
is reported to the job page, and a failed download prints yt-dlp's stderr to
this console (the browser only ever sees the sanitized message).

### Spotify input (optional; needs a Premium account)

Spotify audio comes from the operator's own **Premium** account through the
open-source client library, so it is opt-in, off by default, and set up once by
hand:

```bash
# 1. Install the optional client. requirements-spotify.txt explains why this is
#    a separate file (it needs a Premium login, and its dependency tree
#    declares a Windows-only wheel), so it stays out of the CI install.
#    With the pip --target fallback add --upgrade:
#      py -3.13 -m pip install --target .runtime --upgrade -r requirements-spotify.txt
pip install -r requirements-spotify.txt

# 2. One-time interactive login. Prints the Spotify approval URL, then waits for
#    the local callback on http://127.0.0.1:5588/login. Add --no-browser to print
#    the URL instead of opening it. Re-running reuses the existing credentials.
python -m worker.cli spotify-login

# 3. Let the worker accept Spotify links.
STEMIFY_SPOTIFY_ENABLED=1 python -m worker.job_loop
```

The login writes exactly one file, `data/spotify-credentials.json`, and nothing
about it reaches the web app or the database. Keep it out of version control (it
is gitignored); the fetch child never rewrites it, so a job can never drop a
stray credentials file next to the source.

| Variable | Default | Purpose |
|---|---|---|
| `STEMIFY_SPOTIFY_ENABLED` | `0` | Kill switch. Spotify input is refused until this is set. |
| `STEMIFY_SPOTIFY_CREDENTIALS_FILE` | `$STEMIFY_DATA_DIR/spotify-credentials.json` | Cached login. The library also reads a Rust `librespot --enable-oauth` credentials file, so an existing one can be pointed at directly. |
| `STEMIFY_SPOTIFY_CLIENT_ID` / `STEMIFY_SPOTIFY_CLIENT_SECRET` | unset | Optional free Web API app, used **only** to name a job after the track title. Without it, jobs are named from the track id. |
| `STEMIFY_SPOTIFY_FETCH_TIMEOUT_SECONDS` | `600` | Hard per-track deadline, enforced by killing the fetch's process tree. |
| `STEMIFY_SPOTIFY_HTTP_TIMEOUT_SECONDS` | `15` | Metadata request timeout. |

`python -m worker.cli health` says which of these is missing: `unavailable`
(client not installed), `disabled` (kill switch off), `signed out` (no
credentials file), or `ready`.

The fetch keeps Spotify's native Ogg Vorbis stream — nothing is re-encoded
before separation — and the child process receives only the validated
22-character track id, never the link that was pasted.

The web app has its **Spotify Link** tab since milestone S4
(`docs/SPOTIFY_PLAN.md`), so a link pasted there becomes a normal job. The tab
is always visible and the kill switch above is what decides whether a job can
run: on a machine that is not set up, a Spotify job fails fast with its own
message (`Spotify input is switched off on this machine.` or `Spotify input is
not set up on this machine.`) instead of a generic download error. Only single
tracks are accepted — album/playlist links and `spotify.link` short links are
rejected at job creation, because a short link's target cannot be verified
without a network redirect.

## Job pipeline (Task 11)

A claimed upload job runs the full local pipeline: ffprobe validation and
canonical decode (Task 8) → Demucs separation (Task 9) → encoding and ZIP
packaging (Task 10) → outputs published under `results/{job_id}/` with rows in
`job_outputs` (the ZIP is recorded with stem key `archive`). Cancellation is
honored at stage boundaries and finalizes the job as `canceled`; every failure
path ends in a terminal state with a stable public error code.

Developer commands:

```bash
python -m worker.cli health     # FFmpeg/SQLite/model-stack readiness report
python -m worker.cli separate --input ./fixtures/song.mp3 \
    --mode vocals_instrumental --format mp3 --output ./artifacts
```

## Verify

```bash
python -m pytest              # tests
ruff check worker tests       # lint (repo-root files are not linted)
```

With the pip --target fallback, prefix both with
`PYTHONPATH="$PWD/.runtime"`.

## Model adapter (Task 9)

The separation engine lives behind `worker/worker/models/`:
`profiles.py` (allowlisted model profiles), `base.py` (profile dataclass), and
`demucs.py` (adapter). The rest of the worker never touches torch or Demucs
tensors — it passes canonical stereo float32 waveforms in and receives named
numpy stems out. In `vocals_instrumental` mode the instrumental stem is
mixture minus vocals (fixed per-profile policy); `full_stems` is the non-vocal
rhythm split — drums, bass, and the residual instrumental bed — from the same
htdemucs checkpoint, so the three outputs never overlap.

## Encoding and packaging (Task 10)

`worker/worker/encoding.py` turns validated float32 WAV masters into the
selected output format (mp3 320k, wav PCM s16, flac, ogg Vorbis q8, m4a AAC
256k) with FFmpeg argument arrays, then re-probes every encoded file with
ffprobe before it may be published. `worker/worker/packaging.py` builds the
schema-valid `manifest.json` (validated against
`packages/contracts/schemas/output-manifest.schema.json` when jsonschema is
installed) and a deterministic ZIP containing the stems plus the manifest.
Partial artifacts are never published; failed writes leave no ZIP behind.

Pipeline wiring (claim → separate → encode → package → outputs in SQLite) is
implemented in `worker/worker/job_loop.py` (Task 11): every failure path ends
in a terminal state, and cancellation is honored at stage boundaries.
