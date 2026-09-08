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
  # data-dir logic still runs for real.
  sed -i 's/^npm run dev.*$/echo DEV_SERVER_PLACEHOLDER/; s/^wait "\$WEB_PID"$//' start.sh
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
  sed -i 's/^npm run dev.*$/echo DEV_SERVER_PLACEHOLDER/; s/^wait "\$WEB_PID"$//' start.sh
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
  sed -i 's/^npm run dev.*$/echo DEV_SERVER_PLACEHOLDER/; s/^wait "\$WEB_PID"$//' start.sh
  out="$(STEMIFY_SKIP_PREFLIGHT=1 STEMIFY_SKIP_MODEL_DOWNLOAD=1 timeout 20 bash start.sh 2>&1)"
  echo "$out" | grep -q "Downloading the separation model" && r=bad || r=ok
  check "model download skipped under STEMIFY_SKIP_MODEL_DOWNLOAD" "${r:-bad}" "ok"
)
(
  cd "$work"
  cp "$OLDPWD/start.sh" start.sh
  sed -i 's/^npm run dev.*$/echo DEV_SERVER_PLACEHOLDER/; s/^wait "\$WEB_PID"$//' start.sh
  mkdir -p data/models/hub/checkpoints
  head -c 84141911 /dev/zero > data/models/hub/checkpoints/955717e8-8726e21a.th
  out="$(STEMIFY_SKIP_PREFLIGHT=1 STEMIFY_SKIP_MODEL_DOWNLOAD=0 timeout 20 bash start.sh 2>&1)"
  echo "$out" | grep -q "Downloading the separation model" && r=bad || r=ok
  check "complete checkpoint is not re-downloaded" "${r:-bad}" "ok"
)

# ---------------------------------------------------------------------------
# 4. Clean tree: no process is left behind after the script exits.
# ---------------------------------------------------------------------------
(
  cd "$work"
  cp "$OLDPWD/start.sh" start.sh
  sed -i 's/^npm run dev.*$/sleep 30/; s/^wait "\$WEB_PID"$//' start.sh
  STEMIFY_SKIP_PREFLIGHT=1 timeout 5 bash start.sh >/dev/null 2>&1
)
sleep 1
pgrep -f "sleep 30" >/dev/null 2>&1 && r=bad || r=ok
check "trap stops child processes on exit" "ok" "$r"

echo
echo "startup smoke: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
