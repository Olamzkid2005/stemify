#!/usr/bin/env bash
# Task 15: startup smoke tests (plan Sections 9.1, 17.3).
#
# Runs the real start.sh logic in a sandbox: no Node, Python, or FFmpeg in a
# stripped PATH must fail early with actionable errors; the data directories
# must be created; preflight can be skipped explicitly.
#
# Usage: bash tests/test_startup_smoke.sh   (from the repository root)

set -uo pipefail
cd "$(dirname "$0")/.."

PASS=0
FAIL=0

check() {
  local name="$1" expected="$2" actual="$3"
  if [ "$actual" = "$expected" ]; then
    echo "ok   - $name"
    PASS=$((PASS + 1))
  else
    echo "FAIL - $name (expected '$expected', got '$actual')"
    FAIL=$((FAIL + 1))
  fi
}

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Is this PID still alive? The "nothing was left behind" checks ask about a
# specific process the script itself started, never about a pattern in a process
# listing: `pgrep` is not part of Git Bash, and MSYS `ps` cannot see children of
# another shell, so a listing-based check would pass by accident.
is_alive() {
  [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null
}

# The replacement command for the web process in the pool cases: it records its
# own PID (so the check can ask about that exact process) and then stays alive
# long enough to be observed.
WEB_PLACEHOLDER="python -c \"import os,time;open('web.pid','w').write(str(os.getpid()));time.sleep(45)\""

run_start_sh() {
  # Run start.sh in an isolated copy, stopping after the startup banner
  # (never boot the dev server here). Each case gets a fresh environment.
  local path_case="$1"; shift
  (
    cd "$work"
    cp "$OLDPWD/start.sh" start.sh
    chmod +x start.sh
    env PATH="$path_case" HOME="$work" timeout 20 bash start.sh >/dev/null 2>"$work/stderr.log"
    echo "$?"
  )
}

# ---------------------------------------------------------------------------
# 1. Missing prerequisites fail early with actionable errors (plan 2.2).
# ---------------------------------------------------------------------------
out="$(run_start_sh "/usr/bin:/bin")"
check "empty environment exits nonzero" "1" "$out"
grep -q "not installed" "$work/stderr.log" && r=ok || r=bad
check "missing tools produce actionable errors" "ok" "$r"

# ---------------------------------------------------------------------------
# 2. The data directories are created by the startup flow (plan 9.1 step 5).
#    Reuse the case above: preflight fails before mkdir, so assert nothing
#    was created yet, then run the skip-preflight path.
# ---------------------------------------------------------------------------
[ -d "$work/data/sources" ] && r=bad || r=ok
check "failed preflight does not create data dirs" "ok" "$r"

(
  cd "$work"
  cp "$OLDPWD/start.sh" start.sh
  # Cut the script right before the web process starts; the preflight and
  # data-dir logic still runs for real. The web line carries a HOSTNAME=...
  # prefix, so the pattern matches the command rather than the line start.
  sed -i 's|npm run dev -w apps/web|echo DEV_SERVER_PLACEHOLDER|; s/^wait "\$WEB_PID"$//' start.sh
  STEMIFY_SKIP_PREFLIGHT=1 timeout 20 bash start.sh >/dev/null 2>&1
)
for d in sources results models; do
  [ -d "$work/data/$d" ] && r=ok || r=bad
  check "creates data/$d" "ok" "$r"
done
[ -d "$work/data" ] && r=ok || r=bad
check "creates the data root" "ok" "$r"

# ---------------------------------------------------------------------------
# 3. STEMIFY_SKIP_PREFLIGHT=1 bypasses the checks (used by CI smoke runs).
# ---------------------------------------------------------------------------
(
  cd "$work"
  cp "$OLDPWD/start.sh" start.sh
  sed -i 's|npm run dev -w apps/web|echo DEV_SERVER_PLACEHOLDER|; s/^wait "\$WEB_PID"$//' start.sh
  out="$(STEMIFY_SKIP_PREFLIGHT=1 STEMIFY_SKIP_MODEL_DOWNLOAD=1 timeout 20 bash start.sh 2>&1)"
  echo "$out" | grep -q "Stemify dev server" && r=ok || r=bad
)
check "skip-preflight path reaches the startup banner" "${r:-bad}" "ok"

# ---------------------------------------------------------------------------
# 3b. Model checkpoint pre-download step (plan 14.3): skipped under the test
# escape hatch, and a no-op when the checkpoint file is already complete.
# ---------------------------------------------------------------------------
(
  cd "$work"
  cp "$OLDPWD/start.sh" start.sh
  sed -i 's|npm run dev -w apps/web|echo DEV_SERVER_PLACEHOLDER|; s/^wait "\$WEB_PID"$//' start.sh
  out="$(STEMIFY_SKIP_PREFLIGHT=1 STEMIFY_SKIP_MODEL_DOWNLOAD=1 timeout 20 bash start.sh 2>&1)"
  echo "$out" | grep -q "Downloading the separation model" && r=bad || r=ok
  check "model download skipped under STEMIFY_SKIP_MODEL_DOWNLOAD" "${r:-bad}" "ok"
)
(
  cd "$work"
  cp "$OLDPWD/start.sh" start.sh
  sed -i 's|npm run dev -w apps/web|echo DEV_SERVER_PLACEHOLDER|; s/^wait "\$WEB_PID"$//' start.sh
  mkdir -p data/models/hub/checkpoints
  head -c 84141911 /dev/zero > data/models/hub/checkpoints/955717e8-8726e21a.th
  out="$(STEMIFY_SKIP_PREFLIGHT=1 STEMIFY_SKIP_MODEL_DOWNLOAD=0 timeout 20 bash start.sh 2>&1)"
  echo "$out" | grep -q "Downloading the separation model" && r=bad || r=ok
  check "complete checkpoint is not re-downloaded" "${r:-bad}" "ok"
)

# ---------------------------------------------------------------------------
# 4. Clean tree: no process is left behind after the script exits. The child
#    records its own PID, so this asserts about that process, not about a name
#    that might not be listed at all.
# ---------------------------------------------------------------------------
(
  cd "$work"
  cp "$OLDPWD/start.sh" start.sh
  # A real child in place of the dev server; without a live child the check
  # below would prove nothing.
  sed -i "s|npm run dev -w apps/web|$WEB_PLACEHOLDER|; s/^wait \"\$WEB_PID\"\$//" start.sh
  STEMIFY_SKIP_PREFLIGHT=1 timeout 6 bash start.sh >/dev/null 2>&1
)
sleep 1
web_child="$(cat "$work/web.pid" 2>/dev/null)"
if [ -z "$web_child" ]; then
  echo "FAIL - the web placeholder never recorded its pid"
  FAIL=$((FAIL + 1))
else
  is_alive "$web_child" && r=bad || r=ok
  check "trap stops child processes on exit (pid $web_child)" "ok" "$r"
fi

# ---------------------------------------------------------------------------
# 5. Worker pool sizing (concurrency plan C2): the pool is an explicit choice,
#    the thread budget follows an operator override, and nonsense falls back to
#    one worker instead of failing the start.
# ---------------------------------------------------------------------------
run_pool_case() {
  (
    cd "$work"
    cp "$OLDPWD/start.sh" start.sh
    sed -i 's|npm run dev -w apps/web|echo DEV_SERVER_PLACEHOLDER|; s/^wait "\$WEB_PID"$//' start.sh
    env "$@" STEMIFY_SKIP_PREFLIGHT=1 STEMIFY_SKIP_MODEL_DOWNLOAD=1 \
      timeout 20 bash start.sh > pool.log 2>&1
  )
}

run_pool_case STEMIFY_WORKER_CONCURRENCY=3 STEMIFY_WORKER_THREADS=2
check "pool size and thread split are reported" \
  "Worker pool: 3 process(es), 2 thread(s) each" \
  "$(grep -m1 '^Worker pool:' "$work/pool.log")"

run_pool_case STEMIFY_WORKER_CONCURRENCY=abc
check "a non-numeric pool size falls back to one worker" "ok" \
  "$(grep -q '^Worker pool: 1 process(es)' "$work/pool.log" && echo ok || echo bad)"
check "a non-numeric pool size says why" "ok" \
  "$(grep -q "is not a number" "$work/pool.log" && echo ok || echo bad)"

run_pool_case STEMIFY_WORKER_CONCURRENCY=0
check "a zero pool size falls back to one worker" "ok" \
  "$(grep -q '^Worker pool: 1 process(es)' "$work/pool.log" && echo ok || echo bad)"

# ---------------------------------------------------------------------------
# 6. The pool really starts one process per slot, and Ctrl+C (here: the
#    timeout signal) kills every one of them — a leaked slot would keep
#    claiming and processing jobs while the app is closed.
# ---------------------------------------------------------------------------
(
  cd "$work"
  mkdir -p worker  # without it every slot fails at `cd worker` and proves nothing
  cp "$OLDPWD/start.sh" start.sh
  sed -i \
    -e "s|npm run dev -w apps/web|$WEB_PLACEHOLDER|" \
    -e 's/^wait "\$WEB_PID"$//' \
    -e 's|^if "\${STEMIFY_PYTHON_BIN}".*numpy, soundfile.*$|if true; then|' \
    -e "s|-m worker\.job_loop|-c 'import time; time.sleep(45)'|" \
    start.sh
  STEMIFY_SKIP_PREFLIGHT=1 STEMIFY_SKIP_MODEL_DOWNLOAD=1 STEMIFY_WORKER_CONCURRENCY=3 \
    STEMIFY_WORKER_THREADS=1 timeout 8 bash start.sh > pool.log 2>&1
)
# Counted outside the subshell above, so the values survive for the checks.
started="$(grep -c '^Worker slot .* started' "$work/pool.log")"
slot_pids="$(grep -o 'started (pid [0-9]*' "$work/pool.log" | grep -o '[0-9]*' | sort -u)"
distinct="$(echo "$slot_pids" | wc -l | tr -d ' ')"
check "pool starts one worker process per slot" "3" "$started"
check "each pool slot gets its own process" "3" "$distinct"
sleep 1
alive=0
for pid in $slot_pids; do
  is_alive "$pid" && alive=$((alive + 1))
done
check "every pool slot is killed when the app stops" "0" "$alive"

# ---------------------------------------------------------------------------
# 7. Slot supervision (concurrency plan C2): a slot that dies after doing some
#    work is restarted, and one that dies instantly is not — otherwise a missing
#    runtime becomes a crash-loop that floods the log.
# ---------------------------------------------------------------------------
pool_supervision_case() {
  # $1 = the command the slot runs instead of the worker loop
  # $2 = how long to let the slot live before killing it, in seconds
  local slot_command="$1" uptime="$2"
  (
    cd "$work"
    mkdir -p worker
    cp "$OLDPWD/start.sh" start.sh
    sed -i \
      -e "s|npm run dev -w apps/web|$WEB_PLACEHOLDER|" \
      -e 's/^wait "\$WEB_PID"$//' \
      -e 's|^if "\${STEMIFY_PYTHON_BIN}".*numpy, soundfile.*$|if true; then|' \
      -e "s|-m worker\.job_loop|$slot_command|" \
      start.sh
    STEMIFY_SKIP_PREFLIGHT=1 STEMIFY_SKIP_MODEL_DOWNLOAD=1 STEMIFY_WORKER_CONCURRENCY=1 \
      STEMIFY_WORKER_THREADS=1 bash start.sh > supervision.log 2>&1 &
    app_pid=$!
    # Wait for the slot to appear, then let it run for `uptime` so the
    # "exited immediately" guard (5 s) has clearly expired before the kill.
    for _ in $(seq 1 60); do
      grep -q '^Worker slot 1 started' supervision.log && break
      sleep 0.5
    done
    slot_pid="$(grep -m1 -o 'started (pid [0-9]*' supervision.log | grep -o '[0-9]*')"
    sleep "$uptime"
    is_alive "$slot_pid" && kill "$slot_pid" 2>/dev/null
    sleep 8  # > SLOT_CHECK_SECONDS (2) so the monitor has noticed and reacted
    kill "$app_pid" 2>/dev/null
    wait "$app_pid" 2>/dev/null
    echo "$slot_pid" > first-slot.pid
  )
}

# A slot killed after doing real work is replaced...
pool_supervision_case "-c 'import time; time.sleep(45)'" 7
check "a slot that dies after working is restarted" "2" \
  "$(grep -c '^Worker slot 1 started' "$work/supervision.log")"
first_slot="$(cat "$work/first-slot.pid" 2>/dev/null)"
replacement="$(grep -o 'started (pid [0-9]*' "$work/supervision.log" | grep -o '[0-9]*' | tail -1)"
check "the replacement is a new process" "ok" \
  "$([ -n "$replacement" ] && [ "$replacement" != "$first_slot" ] && echo ok || echo bad)"
is_alive "$replacement" && r=bad || r=ok
check "the replacement did not outlive the app" "ok" "$r"

# ...and one that cannot start at all is not, or a broken runtime crash-loops.
pool_supervision_case "-c 'import sys; sys.exit(3)'" 2
check "a slot that cannot start at all is not restarted" "1" \
  "$(grep -c '^Worker slot 1 started' "$work/supervision.log")"
check "...and says so instead of looping silently" "ok" \
  "$(grep -q 'exited immediately; not restarting it' "$work/supervision.log" && echo ok || echo bad)"
check "...and stops trying within the window" "1" \
  "$(grep -c 'started (pid' "$work/supervision.log")"

echo
echo "startup smoke: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
