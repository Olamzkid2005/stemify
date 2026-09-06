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

## Verify

```bash
python -m pytest          # tests
ruff check .              # lint
```

The model/GPU stack (PyTorch, separation libraries, FFmpeg) is pinned in the Modal
image at Task 11 — local development stays dependency-light until then.
