# Stemify worker (`worker/`)

Python worker that validates audio, runs GPU separation, encodes stems, and reports
progress to the web application. Product plan: `STEM_EXTRACTOR_PLAN.md` (Section 13, 32).

## Setup

```bash
cd worker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

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

## Verify

```bash
python -m pytest          # tests
ruff check .              # lint
```

The model/GPU stack (PyTorch, separation libraries, FFmpeg) is pinned in the Modal
image at Task 11 — local development stays dependency-light until then.
