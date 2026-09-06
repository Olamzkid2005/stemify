# Stemify contracts (`packages/contracts`)

JSON Schemas shared by the web app and the worker: job requests, status responses,
worker events, and the output manifest. Shared enums live in `common.schema.json`.
Product plan: `STEM_EXTRACTOR_PLAN.md` (Task 2, Section 11).

## Layout

- `schemas/common.schema.json` — enums: source types, modes, formats, statuses, stages, error codes
- `schemas/job-request.schema.json` — `POST /api/jobs`
- `schemas/job-status.schema.json` — `GET /api/jobs/{jobId}`
- `schemas/job-events.schema.json` — `POST /api/internal/worker-events`
- `schemas/output-manifest.schema.json` — `manifest.json` inside the result ZIP

## Test

```bash
python3 -m venv --system-site-packages .venv  # or reuse any Python >= 3.12
pip install -r requirements.txt
python -m pytest
```

The schemas are the contract. When a schema changes, both `apps/web` and `worker`
must be updated in the same change.
