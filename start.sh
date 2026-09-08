#!/usr/bin/env bash
# Start the Stemify local application (plan Section 9.1 startup flow).
#
# Verifies prerequisites before starting anything, then launches the Next.js
# web process and the Python worker, and stops both cleanly on exit.
# Set STEMIFY_SKIP_PREFLIGHT=1 to skip the checks (used by the smoke tests).
set -uo pipefail
cd "$(dirname "$0")"

WEB_PID=""
WORKER_PID=""

PREFLIGHT_FAILED=0

fail() {
  echo "ERROR: $1" >&2
  PREFLIGHT_FAILED=1
}

need_command() {
  local name="$1" hint="$2"
  if ! command -v "$name" >/dev/null 2>&1; then
    fail "$name is not installed. $hint"
  fi
}

# Export root .env so both processes see it regardless of workspace cwd.
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

if [ "${STEMIFY_SKIP_PREFLIGHT:-0}" != "1" ]; then
  echo "== Stemify startup checks =="

  need_command "bash" "Run this script with Bash (Git Bash on Windows)."
  need_command "node" "Install Node.js LTS from https://nodejs.org."
  need_command "npm" "npm ships with Node.js: https://nodejs.org."
  need_command "python" "Install Python 3.12+ from https://python.org or the Microsoft Store."

  if command -v python >/dev/null 2>&1; then
    if ! python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >/dev/null 2>&1; then
      fail "Python 3.12 or newer is required."
    fi
  fi

  need_command "ffmpeg" "Install FFmpeg (e.g. winget install Gyan.FFmpeg) and put it on PATH."
  need_command "ffprobe" "ffprobe ships with FFmpeg; reinstall FFmpeg if it is missing."

  if [ ! -d node_modules ]; then
    echo "node_modules missing; run: npm install"
    PREFLIGHT_FAILED=1
  fi

  # Python runtime deps are checked through the worker's own health command,
  # which knows the exact import set (numpy, torch, soundfile, ...).
  if command -v python >/dev/null 2>&1; then
    if ! python -m worker.cli health >/dev/null 2>&1; then
      echo "Python runtime check failed (continuing; the worker will fail jobs"
      echo "with a setup message until 'pip install -r worker/requirements.txt' succeeds)."
    fi
  fi

  if [ "$PREFLIGHT_FAILED" != "0" ]; then
    echo "" >&2
    echo "Startup checks failed. Fix the items above and run ./start.sh again." >&2
    exit 1
  fi
  echo "== Checks passed =="
fi

# Create the local data directories (plan Section 9.1 step 5).
mkdir -p data/sources data/results data/models

echo "Stemify dev server: http://localhost:3000 (Ctrl+C to stop)"

cleanup() {
  local status=$?
  for pid in "${WEB_PID:-}" "${WORKER_PID:-}"; do
    if [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1; then
      kill "$pid" 2>/dev/null
      wait "$pid" 2>/dev/null
    fi
  done
  exit "$status"
}
trap cleanup EXIT INT TERM

# Web process (foreground log). `jobs -p` instead of $!: set -u treats $! as
# unset in some Git Bash (MSYS2) builds once a trap is armed.
npm run dev -w apps/web &
WEB_PID=$(jobs -p | tail -1)

# Python worker process; a missing runtime degrades to web-only operation
# (upload UI works, jobs fail with a clear setup message).
if python -c "import numpy, soundfile" >/dev/null 2>&1; then
  python -m worker.job_loop &
  WORKER_PID=$(jobs -p | tail -1)
  echo "Worker started (pid $WORKER_PID)"
else
  echo "Worker not started: python numpy/soundfile missing (pip install -r worker/requirements.txt)" >&2
fi

wait "$WEB_PID"
