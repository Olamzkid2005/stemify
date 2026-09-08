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
mixture minus vocals (fixed per-profile policy).

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
