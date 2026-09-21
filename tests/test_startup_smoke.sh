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

# Results are tallied through files, not variables: several cases run inside
# `( ... )` subshells, where an increment to $PASS / $FAIL is lost when the
# subshell exits. The summary used to under-report those checks (25 of 31), so
# run of the suite looked like lost coverage.
counters="$(mktemp -d)"
: > "$counters/pass"
: > "$counters/fail"

check() {
  local name="$1" expected="$2" actual="$3"
  if [ "$actual" = "$expected" ]; then
    echo "ok   - $name"
    echo "pass" >> "$counters/pass"
  else
    echo "FAIL - $name (expected '$expected', got '$actual')"
    echo "fail" >> "$counters/fail"
  fi
}

work="$(mktemp -d)"
trap 'rm -rf "$work" "$counters"' EXIT

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
  # Skipped explicitly: this case is about the preflight and the data dirs, and
  # the model step would otherwise reach the network (or, worse, pass only
  # because an earlier case left a checkpoint file behind).
  STEMIFY_SKIP_PREFLIGHT=1 STEMIFY_SKIP_MODEL_DOWNLOAD=1 timeout 20 bash start.sh >/dev/null 2>&1
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
(
  cd "$work"
  cp "$OLDPWD/start.sh" start.sh
  sed -i 's|npm run dev -w apps/web|echo DEV_SERVER_PLACEHOLDER|; s/^wait "\$WEB_PID"$//' start.sh
  # No checkpoint at all (the first run on a new machine): the size probe must
  # not shout before the script decides to download. Reading a missing file with
  # a shell redirect printed "No such file or directory" whatever 2>/dev/null
  # was attached to wc, so the run looked broken before it started.
  rm -rf data/models
  out="$(STEMIFY_SKIP_PREFLIGHT=1 STEMIFY_SKIP_MODEL_DOWNLOAD=0 timeout 20 bash start.sh 2>&1)"
  # Match the checkpoint's own name: this sandbox has no worker/ directory, so
  # an unrelated "cd: worker: No such file or directory" is expected here.
  echo "$out" | grep -q "checkpoints/955717e8-8726e21a.th: No such file or directory" && r=bad || r=ok
  check "a missing checkpoint is probed without a shell error" "ok" "${r:-bad}"
)

# ---------------------------------------------------------------------------
# 3c. The model pre-download itself (plan 14.3). `curl -o` does not create the
#     file's directory, which is several levels deep here, so a first run used
#     to fail the download outright; and the target must be the data directory
#     the worker actually loads from. A stub curl keeps both offline.
# ---------------------------------------------------------------------------
ENGINE_READY=0
for interp in python3 python; do
  command -v "$interp" >/dev/null 2>&1 || continue
  "$interp" -c "import torch, demucs" >/dev/null 2>&1 && ENGINE_READY=1 && break
done

# A curl that records the -o path it was given and writes there, exactly like
# the real one: a missing parent directory fails here too, which is the bug
# being pinned. Keeps the case offline and independent of the real download.
install_curl_stub() {
  mkdir -p bin
  cat > bin/curl <<'STUB'
#!/usr/bin/env bash
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    *) shift ;;
  esac
done
printf '%s' "$out" > "$STEMIFY_CURL_TARGET"
printf 'stub' > "$out"
STUB
  chmod +x bin/curl
}

if [ "$ENGINE_READY" = "1" ]; then
  (
    cd "$work"
    cp "$OLDPWD/start.sh" start.sh
    sed -i 's|npm run dev -w apps/web|echo DEV_SERVER_PLACEHOLDER|; s/^wait "\$WEB_PID"$//' start.sh
    rm -rf data bin curl-target
    install_curl_stub
    out="$(STEMIFY_CURL_TARGET="$PWD/curl-target" STEMIFY_SKIP_PREFLIGHT=1 STEMIFY_SKIP_MODEL_DOWNLOAD=0 PATH="$PWD/bin:$PATH" timeout 20 bash start.sh 2>&1)"
    [ -f data/models/hub/checkpoints/955717e8-8726e21a.th ] && r=ok || r=bad
    check "a missing checkpoint downloads into its own directory" "ok" "$r"
    echo "$out" | grep -q "model download did not finish" && r=bad || r=ok
    check "...and is not reported as a failed download" "ok" "$r"
  )
  (
    cd "$work"
    cp "$OLDPWD/start.sh" start.sh
    sed -i 's|npm run dev -w apps/web|echo DEV_SERVER_PLACEHOLDER|; s/^wait "\$WEB_PID"$//' start.sh
    custom="$work/custom-data"
    rm -rf custom-data curl-target
    install_curl_stub
    out="$(STEMIFY_DATA_DIR="$custom" STEMIFY_CURL_TARGET="$PWD/curl-target" STEMIFY_SKIP_PREFLIGHT=1 STEMIFY_SKIP_MODEL_DOWNLOAD=0 PATH="$PWD/bin:$PATH" timeout 20 bash start.sh 2>&1)"
    # The worker resolves its model under STEMIFY_DATA_DIR: a checkpoint fetched
    # into ./data anyway would simply be downloaded a second time on first use.
    [ -f "$custom/models/hub/checkpoints/955717e8-8726e21a.th" ] && r=ok || r=bad
    check "the pre-download targets the configured data directory" "ok" "$r"
  )
else
  echo "skip - model pre-download cases need torch and demucs installed"
fi

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
  # The model step runs before the web process starts, so leaving it on would
  # spend this case's whole window on a real download and never start the
  # placeholder this check is about.
  STEMIFY_SKIP_PREFLIGHT=1 STEMIFY_SKIP_MODEL_DOWNLOAD=1 timeout 6 bash start.sh >/dev/null 2>&1
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
    # The dev-server stub also reports the pool size the web process would
    # inherit: the home page's pool summary reads this variable, so what it is
    # told must be what actually runs.
    sed -i \
      -e 's|npm run dev -w apps/web|echo DEV_SERVER_PLACEHOLDER; echo "WEB SEES POOL=$STEMIFY_WORKER_CONCURRENCY"|' \
      -e 's/^wait "\$WEB_PID"$//' \
      start.sh
    env "$@" STEMIFY_SKIP_PREFLIGHT=1 STEMIFY_SKIP_MODEL_DOWNLOAD=1 \
      timeout 20 bash start.sh > pool.log 2>&1
  )
}

run_pool_case STEMIFY_WORKER_CONCURRENCY=3 STEMIFY_WORKER_THREADS=2
check "pool size and thread split are reported" \
  "Worker pool: 3 process(es), 2 thread(s) each" \
  "$(grep -m1 '^Worker pool:' "$work/pool.log")"
check "the web app is told the pool size that runs" "WEB SEES POOL=3" \
  "$(grep -m1 '^WEB SEES POOL=' "$work/pool.log")"

run_pool_case STEMIFY_WORKER_CONCURRENCY=abc
check "a non-numeric pool size falls back to one worker" "ok" \
  "$(grep -q '^Worker pool: 1 process(es)' "$work/pool.log" && echo ok || echo bad)"
check "a non-numeric pool size says why" "ok" \
  "$(grep -q "is not a number" "$work/pool.log" && echo ok || echo bad)"
check "the web app is told the normalized pool size" "WEB SEES POOL=1" \
  "$(grep -m1 '^WEB SEES POOL=' "$work/pool.log")"

run_pool_case STEMIFY_WORKER_CONCURRENCY=0
check "a zero pool size falls back to one worker" "ok" \
  "$(grep -q '^Worker pool: 1 process(es)' "$work/pool.log" && echo ok || echo bad)"
check "a zero pool size does not reach the web app as zero" "WEB SEES POOL=1" \
  "$(grep -m1 '^WEB SEES POOL=' "$work/pool.log")"

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

PASS=$(wc -l < "$counters/pass" | tr -d ' ')
FAIL=$(wc -l < "$counters/fail" | tr -d ' ')

echo
echo "startup smoke: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
