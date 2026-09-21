#!/usr/bin/env bash
# Start the Stemify local application (plan Section 9.1 startup flow).
#
# Verifies prerequisites before starting anything, then launches the Next.js
# web process and the Python worker, and stops both cleanly on exit.
# Set STEMIFY_SKIP_PREFLIGHT=1 to skip the checks (used by the smoke tests).
set -uo pipefail
cd "$(dirname "$0")"

WEB_PID=""
# One entry per worker slot in the pool (concurrency plan C2). Every slot is
# tracked individually: cleanup() must kill all of them, or Ctrl+C leaves the
# extras running, silently claiming and processing jobs while the app is shut.
WORKER_PIDS=()
WORKER_STARTED=()

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

# Normalize STEMIFY_DATA_DIR to an absolute path: the web process runs with
# apps/web as cwd and the worker with worker/, so a relative value would
# resolve to a different directory per process and the app would be split.
# Default matches the code fallback (./data) when neither .env nor env sets it.
: "${STEMIFY_DATA_DIR:=./data}"
case "$STEMIFY_DATA_DIR" in
  /*|?[A-Za-z]:*) ;; # already absolute (POSIX or drive form)
  *) STEMIFY_DATA_DIR="$(pwd)/${STEMIFY_DATA_DIR#./}" ;;
esac
# MSYS/Git Bash: hand native Windows processes a drive path (C:/...), not /c/...
command -v cygpath >/dev/null 2>&1 && STEMIFY_DATA_DIR="$(cygpath -m "$STEMIFY_DATA_DIR")"
export STEMIFY_DATA_DIR

# Resolve a Python that can actually run the worker. Bare `python` may be an
# interpreter without the project's deps; prefer one that can import them,
# optionally with a project-local pip --target dir prepended to PYTHONPATH
# (worker/.runtime — see worker/README.md).
resolve_worker_python() {
  local target=""
  if [ -d worker/.runtime ]; then
    target="$(cd worker/.runtime && pwd)"
  fi
  # Candidates as "interpreter|version-arg"; empty arg = no selector.
  local cand interp args
  for cand in "python3|" "python|" "py|-3.13" "py|-3.14" "py|-3.12" "py|"; do
    interp="${cand%%|*}"
    args="${cand#*|}"
    command -v "$interp" >/dev/null 2>&1 || continue
    if [ -n "$target" ]; then
      if PYTHONPATH="$target" "$interp" ${args:+"$args"} -c "import numpy, soundfile" >/dev/null 2>&1; then
        STEMIFY_PYTHON_BIN="$interp"
        STEMIFY_PYTHON_ARGS="$args"
        STEMIFY_PYTHON_PATH="$target"
        return 0
      fi
    elif "$interp" ${args:+"$args"} -c "import numpy, soundfile" >/dev/null 2>&1; then
      STEMIFY_PYTHON_BIN="$interp"
      STEMIFY_PYTHON_ARGS="$args"
      STEMIFY_PYTHON_PATH=""
      return 0
    fi
  done
  return 1
}

STEMIFY_PYTHON_BIN="python"
STEMIFY_PYTHON_ARGS=""
STEMIFY_PYTHON_PATH=""
if resolve_worker_python; then
  echo "Worker Python: ${STEMIFY_PYTHON_BIN} ${STEMIFY_PYTHON_ARGS}" \
    "(PYTHONPATH=${STEMIFY_PYTHON_PATH:-system})"
  [ -n "$STEMIFY_PYTHON_PATH" ] && export PYTHONPATH="$STEMIFY_PYTHON_PATH${PYTHONPATH:+:$PYTHONPATH}"
else
  echo "No Python with numpy/soundfile found; worker will not start" \
    "(see worker/README.md)." >&2
fi

# --------------------------------------------------------------- worker pool
# STEMIFY_WORKER_CONCURRENCY is how many jobs run at once (concurrency plan
# C2). The pool is explicit, never auto-sized: each worker holds its own copy
# of the separation model, so the operator decides what the machine can carry.
POOL="${STEMIFY_WORKER_CONCURRENCY:-2}"
case "$POOL" in
  *[!0-9]*|"")
    echo "STEMIFY_WORKER_CONCURRENCY '$POOL' is not a number; using 1." >&2
    POOL=1
    ;;
  0)
    echo "STEMIFY_WORKER_CONCURRENCY must be at least 1; using 1." >&2
    POOL=1
    ;;
esac

# Split the machine's cores across the pool (plan 3.4): N workers each using
# every core is worse than N workers sharing them, because one inference
# already saturates the box. An explicit STEMIFY_WORKER_THREADS wins — the
# operator may know the machine better than this arithmetic does.
if [ -z "${STEMIFY_WORKER_THREADS:-}" ]; then
  CORES="$("${STEMIFY_PYTHON_BIN}" ${STEMIFY_PYTHON_ARGS} -c 'import os; print(os.cpu_count() or 1)' 2>/dev/null)"
  case "$CORES" in *[!0-9]*|"") CORES=1 ;; esac
  STEMIFY_WORKER_THREADS=$((CORES / POOL))
  [ "$STEMIFY_WORKER_THREADS" -lt 1 ] && STEMIFY_WORKER_THREADS=1
fi
export STEMIFY_WORKER_THREADS

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
  # which knows the exact import set (numpy, torch, soundfile, ...). The worker
  # package lives at worker/worker/, so module runs must use worker/ as cwd.
  if ! ( cd worker && "${STEMIFY_PYTHON_BIN}" ${STEMIFY_PYTHON_ARGS} -m worker.cli health ) >/dev/null 2>&1; then
    echo "Python runtime check failed (continuing; the worker will fail jobs"
    echo "with a setup message until 'pip install -r worker/requirements.txt' succeeds)."
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

# Ensure the separation-model checkpoint is present before any job needs it:
# a first-run job would otherwise die at MODEL_LOAD_FAILED while torch.hub
# attempts its own invisible, non-resumable download. This fetch is visible,
# resumable (-C -), and a no-op once the file is complete. The worker still
# verifies the checkpoint hash on every load, so a truncated file can never
# be trusted; it only costs a re-download.
# Size = Content-Length of the pinned htdemucs checkpoint (plan Section 14.3).
ensure_model_checkpoint() {
  # CI/startup smoke tests set this: they run start.sh in sandboxes where a
  # real 84MB download would be wasted work or a timeout.
  [ "${STEMIFY_SKIP_MODEL_DOWNLOAD:-0}" = "1" ] && return 0
  local checkpoint_file="data/models/hub/checkpoints/955717e8-8726e21a.th"
  local checkpoint_url="https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/955717e8-8726e21a.th"
  local expected_size=84141911
  local actual
  actual="$(wc -c < "$checkpoint_file" 2>/dev/null || echo 0)"
  [ "$actual" -ge "$expected_size" ] && return 0
  # Engine not installed -> nothing to pre-warm; jobs fail with the setup
  # message either way (health check already told the user what to install).
  if ! "${STEMIFY_PYTHON_BIN}" ${STEMIFY_PYTHON_ARGS} -c "import torch, demucs" >/dev/null 2>&1; then
    return 0
  fi
  if ! command -v curl >/dev/null 2>&1; then
    echo "NOTE: curl not found; the worker will fetch the model during the first job."
    return 0
  fi
  echo "Downloading the separation model (one-time, ~84MB; resumes if interrupted)..."
  if curl -fL -C - --retry 3 --retry-delay 2 --connect-timeout 15 \
      -o "$checkpoint_file" "$checkpoint_url"; then
    echo "Model checkpoint ready."
  else
    echo "WARNING: model download did not finish. Start Stemify again to resume," >&2
    echo "or let the worker retry during the first job." >&2
  fi
}
ensure_model_checkpoint

echo "Stemify dev server: http://localhost:3000 (Ctrl+C to stop)"

cleanup() {
  local status=$?
  local pid
  for pid in "${WEB_PID:-}" "${WORKER_PIDS[@]:-}"; do
    if [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1; then
      kill "$pid" 2>/dev/null
      wait "$pid" 2>/dev/null
    fi
  done
  exit "$status"
}
trap cleanup EXIT INT TERM

# Web process (foreground log). `jobs -p` instead of $!: set -u treats $! as
# unset in some Git Bash (MSYS2) builds once a trap is armed. Bound to
# 127.0.0.1: the local app must never listen on a LAN interface by default
# (plan Sections 1051, 22).
HOSTNAME=127.0.0.1 npm run dev -w apps/web &
WEB_PID=$(jobs -p | tail -1)

# Python worker pool; a missing runtime degrades to web-only operation (upload
# UI works, jobs fail with a clear setup message). Each slot runs with worker/
# as cwd: the `worker` package is worker/worker/, and `python -m
# worker.job_loop` from the repo root would not resolve it. PYTHONPATH (set
# during resolution) carries the project-local dependency dir when one is in
# use. `jobs -p | tail -1` instead of $! for the same reason as the web process
# above, and no pipe on the command itself: a pipeline would report the PID of
# the last stage, not the worker, and cleanup() would kill the wrong process.
SLOT_CHECK_SECONDS=2
# A slot that dies young is not restarted: an unusable runtime would otherwise
# become a fast crash-loop that floods the log with identical tracebacks.
MIN_SLOT_UPTIME_SECONDS=5

start_worker_slot() {
  local index="$1"
  ( cd worker && exec "${STEMIFY_PYTHON_BIN}" ${STEMIFY_PYTHON_ARGS} -m worker.job_loop ) &
  WORKER_PIDS[$index]=$(jobs -p | tail -1)
  WORKER_STARTED[$index]=$(date +%s)
  echo "Worker slot $((index + 1)) started (pid ${WORKER_PIDS[$index]})"
}

# Replace every slot that has exited, so one crashed job does not silently
# shrink the pool for the rest of the session.
respawn_dead_slots() {
  local index pid now uptime
  for index in "${!WORKER_PIDS[@]}"; do
    pid="${WORKER_PIDS[$index]:-}"
    [ -n "$pid" ] || continue
    kill -0 "$pid" >/dev/null 2>&1 && continue
    now=$(date +%s)
    uptime=$((now - ${WORKER_STARTED[$index]:-0}))
    if [ "$uptime" -ge "$MIN_SLOT_UPTIME_SECONDS" ]; then
      echo "Worker slot $((index + 1)) exited after ${uptime}s; restarting it" >&2
      start_worker_slot "$index"
    else
      echo "Worker slot $((index + 1)) exited immediately; not restarting it" \
        "(see the error above)." >&2
      WORKER_PIDS[$index]=""
    fi
  done
}

echo "Worker pool: ${POOL} process(es), ${STEMIFY_WORKER_THREADS} thread(s) each"
if "${STEMIFY_PYTHON_BIN}" ${STEMIFY_PYTHON_ARGS} -c "import numpy, soundfile" >/dev/null 2>&1; then
  for ((slot = 0; slot < POOL; slot++)); do
    start_worker_slot "$slot"
  done
else
  echo "Worker not started: no Python with numpy/soundfile found (see worker/README.md)" >&2
fi

# The web process's lifetime is the application's lifetime; while it runs, keep
# the pool alive (plan C2). This is the monitor loop the `wait` above used to
# be: a dead slot is noticed and respawned, and Ctrl+C still reaches every
# child through the trap.
if [ -n "${WEB_PID:-}" ]; then
  while kill -0 "$WEB_PID" >/dev/null 2>&1; do
    respawn_dead_slots
    sleep "$SLOT_CHECK_SECONDS"
  done
fi

wait "$WEB_PID"
