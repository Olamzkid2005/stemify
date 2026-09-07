# Stemify worker (`worker/`)

Python worker that validates audio, runs GPU separation, encodes stems, and reports
progress to the web application. Product plan: `STEM_EXTRACTOR_PLAN.md` (Section 13, 32).

## Setup

```bash
cd worker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

# Separation engine (plan Task 9) — optional for control-plane work:
pip install -r requirements.txt
```

On Windows (bash) use `source .venv/Scripts/activate` instead.

The model stack is pinned: `demucs==4.0.1` with `torch==2.5.1` (torch 2.6+
breaks demucs 4.0.1 checkpoint loading). For an NVIDIA GPU, install torch from
the CUDA index first — see the header of `requirements.txt`.

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

## Verify

```bash
python -m pytest          # tests
ruff check .              # lint
```

## Model adapter (Task 9)

The separation engine lives behind `worker/worker/models/`:
`profiles.py` (allowlisted model profiles), `base.py` (profile dataclass), and
`demucs.py` (adapter). The rest of the worker never touches torch or Demucs
tensors — it passes canonical stereo float32 waveforms in and receives named
numpy stems out. In `vocals_instrumental` mode the instrumental stem is
mixture minus vocals (fixed per-profile policy).
