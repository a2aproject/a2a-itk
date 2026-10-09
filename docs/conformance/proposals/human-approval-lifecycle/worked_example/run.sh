#!/usr/bin/env bash
# Starts the real-SDK Expense Agent (Python, a2a-sdk==1.2.2) and Coordinator
# (TypeScript, @a2a-js/sdk@1.3.0), runs all twelve H01-H12 scenarios
# (test_client/scenarios.py's main()) plus the two focused approval-policy
# tests against them, then tears everything down.
#
# BASELINE MODE (default): only the bundled/pinned dependencies are used.
# No reference to py_venv_pr1182 or any PR#1182 material is made at all in
# this mode -- it is safe to run from a distribution that never included
# that material.
#
# OPTIONAL PR#1182 COMPARISON MODE: adds the H04 cross-SDK-version
# comparison against a second Expense Agent instance running under the
# (separately obtained, NOT bundled) PR#1182 editable install. Off by
# default; enable with --with-pr1182-comparison or RUN_PR1182_COMPARISON=1.
#
# Usage:
#   ./run.sh                          # baseline: H01-H12 + 2 focused tests
#   ./run.sh --with-pr1182-comparison # baseline, plus H04 cross-SDK comparison
#   RUN_PR1182_COMPARISON=1 ./run.sh  # same as the flag, via env var
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# --- venv path convention ---------------------------------------------------
# The Python venv is a SIBLING of sdk_integration/ (i.e. created one level up,
# at agent04_integration/py_venv), NOT inside sdk_integration/ itself. This is
# the convention every other script/doc in this tree already assumes
# (test_client/approval_policy_focused_tests.py's own usage comment,
# CLEAN_ROOM_RESULTS.md's reproduction steps) -- this script's own prior
# behavior was already correct; it was README.md that disagreed and has been
# corrected to match, not the other way around.
PY="$HERE/../py_venv/bin/python"

WITH_PR1182=0
for arg in "$@"; do
  case "$arg" in
    --with-pr1182-comparison) WITH_PR1182=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done
if [ "${RUN_PR1182_COMPARISON:-0}" = "1" ]; then
  WITH_PR1182=1
fi

if [ ! -x "$PY" ]; then
  echo "ERROR: Python venv not found at $PY" >&2
  echo "  Set it up with:" >&2
  echo "    cd \"$HERE/..\" && python3 -m venv py_venv" >&2
  echo "    ../py_venv/bin/pip install -r sdk_integration/requirements-official-sdk.txt" >&2
  exit 1
fi

if [ ! -d "$HERE/coordinator/node_modules" ]; then
  echo "ERROR: coordinator/node_modules not found." >&2
  echo "  Set it up with:" >&2
  echo "    cd \"$HERE/coordinator\" && npm ci" >&2
  exit 1
fi

if [ "$WITH_PR1182" = "1" ]; then
  PY_PR1182="$HERE/py_venv_pr1182/bin/python"
  if [ ! -x "$PY_PR1182" ]; then
    echo "ERROR: --with-pr1182-comparison was requested, but no PR#1182 venv was found at:" >&2
    echo "    $PY_PR1182" >&2
    echo "  This venv is NOT bundled in a standard distribution of this fixture (it requires" >&2
    echo "  a separate editable install of the unmerged a2a-python PR #1182 branch)." >&2
    echo "  Skipping the PR#1182 comparison. Baseline H01-H12 + focused tests will still run." >&2
    WITH_PR1182=0
  fi
fi

# --- evidence handling -------------------------------------------------------
# Only clear evidence/logs AFTER all preflight checks above have passed, so a
# failed preflight (missing venv, missing node_modules) never destroys
# packaged/prior evidence. (Previously this ran unconditionally near the top
# of the script, before anything was verified to even start correctly.)
mkdir -p evidence logs
rm -f evidence/*.jsonl logs/*.log
if [ "$WITH_PR1182" = "1" ]; then
  mkdir -p evidence_pr1182 logs_pr1182
  rm -f evidence_pr1182/*.jsonl logs_pr1182/*.log
fi

# --- PID-tracked process cleanup --------------------------------------------
# Only PIDs started BY THIS invocation are ever targeted for cleanup -- never
# a broad pattern-match sweep of the whole process table (a `pkill -f
# "expense_agent/server.py"` pattern could kill an unrelated process on the
# machine that happens to match, e.g. a different checkout of this same
# project, or another session running a similarly-named script).
PIDS=()

cleanup() {
  echo "Tearing down servers started by this run..."
  for pid in "${PIDS[@]:-}"; do
    if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT

echo "=== Starting Expense Agent (a2a-sdk==1.2.2) on :8201 ==="
"$PY" expense_agent/server.py > logs/expense_agent.log 2>&1 &
PIDS+=("$!")
sleep 2
curl -sf http://127.0.0.1:8201/__health__ > /dev/null && echo "expense agent healthy"

echo "=== Starting Coordinator (@a2a-js/sdk@1.3.0) on :8301 ==="
node coordinator/coordinator.mjs > logs/coordinator.log 2>&1 &
PIDS+=("$!")
sleep 2
curl -sf http://127.0.0.1:8301/__health__ > /dev/null && echo "coordinator healthy"

echo "=== Running all 12 scenarios (H01-H12) ==="
(cd test_client && "$PY" scenarios.py)

echo "=== Running focused approval-policy tests (in-process, no servers needed) ==="
(cd test_client && "$PY" approval_policy_focused_tests.py)

if [ "$WITH_PR1182" = "1" ]; then
  PY_PR1182="$HERE/py_venv_pr1182/bin/python"
  echo
  echo "=== H04 cross-SDK-version comparison (--with-pr1182-comparison) ==="
  echo "--- Starting second Expense Agent on :8202 (editable install, PR #1182) ---"
  EXPENSE_HOST=127.0.0.1 EXPENSE_PORT=8202 \
    EXPENSE_WIRE_EVIDENCE="$HERE/evidence_pr1182/expense-agent-wire.jsonl" \
    "$PY_PR1182" expense_agent_pr1182_run/server.py > logs_pr1182/expense_agent.log 2>&1 &
  PIDS+=("$!")
  sleep 2
  curl -sf http://127.0.0.1:8202/__health__ > /dev/null && echo "PR1182 expense agent healthy"

  echo "--- Starting second Coordinator on :8302, pointed at :8202 ---"
  COORDINATOR_HOST=127.0.0.1 COORDINATOR_PORT=8302 EXPENSE_AGENT_URL=http://127.0.0.1:8202 \
    COORDINATOR_WIRE_EVIDENCE="$HERE/evidence_pr1182/coordinator-wire.jsonl" \
    node coordinator/coordinator.mjs > logs_pr1182/coordinator.log 2>&1 &
  PIDS+=("$!")
  sleep 2
  curl -sf http://127.0.0.1:8302/__health__ > /dev/null && echo "PR1182 coordinator healthy"

  echo "--- H04 through full stack: v1.2.2 vs PR#1182 ---"
  (cd test_client && "$PY" h04_cross_sdk.py "H04_v1.2.2" "http://127.0.0.1:8301" "http://127.0.0.1:8201")
  (cd test_client && "$PY" h04_cross_sdk.py "H04_pr1182" "http://127.0.0.1:8302" "http://127.0.0.1:8202")

  echo "--- H04 direct-to-child probe (bypasses coordinator's own terminal guard) ---"
  (cd test_client && "$PY" h04_direct_child_probe.py "v1.2.2_direct" "http://127.0.0.1:8201")
  (cd test_client && "$PY_PR1182" h04_direct_child_probe.py "pr1182_direct" "http://127.0.0.1:8202")
else
  echo
  echo "=== PR#1182 comparison skipped (baseline mode; pass --with-pr1182-comparison or set RUN_PR1182_COMPARISON=1 to enable) ==="
fi

echo
echo "=== Checking recorded evidence ==="
"$PY" verify_evidence.py

echo "=== Done. See evidence/ (and evidence_pr1182/ if enabled) for raw wire traffic, TEST_RESULTS.md and SDK_COMPARISON.md for write-ups. ==="
