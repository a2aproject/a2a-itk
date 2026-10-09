#!/usr/bin/env bash
# Starts the real-SDK Expense Agent (Python, a2a-sdk==1.2.2) and Coordinator
# (TypeScript, @a2a-js/sdk@1.3.0), runs all twelve H01-H12 scenarios
# (test_client/scenarios.py's main()) plus the two focused approval-policy
# tests against them, then tears everything down.
#
# This worked example bundles ONLY the baseline H01-H12 + 2-focused-test
# runner. The originating fixture also had an optional H04 cross-SDK-version
# comparison mode (against a second Expense Agent built from an unmerged
# a2a-python PR branch); that mode, and the files it depends on
# (h04_cross_sdk.py, h04_direct_child_probe.py, expense_agent_pr1182_run/),
# are intentionally NOT included in this upstream contribution, since they
# require external material this worked example does not (and should not)
# bundle. See ../TEST_RESULTS_SUMMARY.md and ../README.md for how that
# separate, already-completed comparison is referenced (not reproduced) here.
#
# Usage:
#   ./run.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# --- venv path convention ---------------------------------------------------
# The Python venv is created as a SIBLING of this worked_example/ directory
# (i.e. one level up), not inside it. See ../worked_example/README.md's
# "Running it" section for the exact setup commands this expects.
PY="$HERE/../py_venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "ERROR: Python venv not found at $PY" >&2
  echo "  Set it up with (from inside this worked_example/ directory):" >&2
  echo "    python3 -m venv ../py_venv" >&2
  echo "    ../py_venv/bin/pip install -r requirements-official-sdk.txt" >&2
  exit 1
fi

if [ ! -d "$HERE/coordinator/node_modules" ]; then
  echo "ERROR: coordinator/node_modules not found." >&2
  echo "  Set it up with:" >&2
  echo "    cd \"$HERE/coordinator\" && npm ci" >&2
  exit 1
fi

# --- evidence handling -------------------------------------------------------
# Only clear evidence/logs AFTER all preflight checks above have passed, so a
# failed preflight (missing venv, missing node_modules) never destroys
# packaged/prior evidence.
mkdir -p evidence logs
rm -f evidence/*.jsonl logs/*.log

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

echo
echo "=== Checking recorded evidence ==="
"$PY" verify_evidence.py

echo "=== Done. See evidence/ for raw wire traffic; ../TEST_RESULTS_SUMMARY.md for the write-up. ==="
