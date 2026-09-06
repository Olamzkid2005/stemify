# Stemify

Fast, simple music stem separation in your browser. Supported tracks are normally
processed within five minutes.

One repository, independently deployable parts (product plan: `STEM_EXTRACTOR_PLAN.md`):

- `apps/web` — Next.js (App Router) web application, deployed on Vercel.
- `worker/` — Python audio-separation worker (Modal GPU deployment comes at Task 11).
- `packages/contracts` — shared job/event schemas (added in Task 2).

## Develop

Web:

```bash
npm install
npm run dev -w apps/web        # http://localhost:3000
npm run lint -w apps/web
npm run build -w apps/web
```

Worker:

```bash
cd worker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
ruff check .
```

Working rules for agents and contributors: `AGENTS.md`.
