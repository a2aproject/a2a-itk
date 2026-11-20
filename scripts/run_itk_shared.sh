#!/bin/bash
# Shared ITK driver, sourced by each SDK's itk/run_itk.sh.
#
# The five per-SDK scripts were ~70% identical and had drifted in the rest —
# three different result summarisers, only one response validator,
# inconsistent git safe.directory handling. This is the union of the correct
# behaviours; each SDK keeps only what is language-specific.
#
# The caller clones a2a-itk (it cannot source this file otherwise), sets the
# configuration, optionally defines the hooks, then sources this as its last
# statement.
#
# Configuration, hooks and a copy-pasteable shim per SDK are documented in
# scripts/README.md. Keep that table the single source; do not restate it
# here.

set -ex
ITK_NIGHTLY_RUN="${ITK_NIGHTLY_RUN:-}"

: "${ITK_SDK_NAME:?ITK_SDK_NAME must be set before sourcing run_itk_shared.sh}"
: "${A2A_ITK_REVISION:?A2A_ITK_REVISION environment variable must be set}"

ITK_SDK_REPO="${ITK_SDK_REPO:-a2a-${ITK_SDK_NAME}}"
ITK_METRICS_NAME="${ITK_METRICS_NAME:-${ITK_SDK_NAME}}"
# The key this SDK has in matrix.yaml. Usually the same as ITK_SDK_NAME, but
# a2a-js is 'ts' there — its agent ids are ts_v10 / ts_v03 — so it overrides.
ITK_MATRIX_SDK="${ITK_MATRIX_SDK:-${ITK_SDK_NAME}}"
ITK_COPY_PROTO="${ITK_COPY_PROTO:-1}"
# Run the ACTS conformance suite instead of the traversal suite. A separate
# switch from ITK_NIGHTLY_RUN because the two are orthogonal: ACTS can run on
# a PR, and a traversal nightly does not imply a conformance one.
ITK_ACTS_RUN="${ITK_ACTS_RUN:-0}"
ITK_ACTS_TRANSPORTS="${ITK_ACTS_TRANSPORTS:-jsonrpc}"
# The A2A versions to run ACTS at, one run of every binding each, and one
# history file each. An SDK lists 1.1 only once its agent serves it.
ITK_ACTS_SPEC_VERSIONS="${ITK_ACTS_SPEC_VERSIONS:-1.0}"
ITK_ACTS_LANGUAGE="${ITK_ACTS_LANGUAGE:-${ITK_SDK_NAME}}"
ITK_MOUNT_ITK_DIR="${ITK_MOUNT_ITK_DIR:-1}"
ITK_SELINUX_MOUNT_LABEL="${ITK_SELINUX_MOUNT_LABEL:-0}"
ITK_REMOVE_IMAGE="${ITK_REMOVE_IMAGE:-0}"
export ITK_LOG_LEVEL="${ITK_LOG_LEVEL:-INFO}"

# a2a-itk root, derived from this script's own location rather than assumed
# to be ./a2a-itk — the shim may have cloned it elsewhere.
ITK_REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ITK_DIR="$(pwd)"
SDK_ROOT="$(cd .. && pwd)"

# Declared so the unconditional expansion below is safe when a shim does not
# set it.
if ! declare -p ITK_EXTRA_DOCKER_ARGS &> /dev/null; then
  ITK_EXTRA_DOCKER_ARGS=()
fi

# Non-zero until the run proves otherwise, so an unexpected early exit is
# reported as a failure rather than a pass.
RESULT=1

# What the files of one A2A version are called: nothing added for 1.0, so
# they keep the names they had before ACTS ran at more than one version, and
# `_v11` for 1.1, as in the agent ids of matrix.yaml (`python_v10`).
acts_suffix() {
  if [ "$1" != "1.0" ]; then
    echo "_v${1//./}"
  fi
}

# Checked before anything is built or started: a list that is mistyped would
# otherwise surface at the end of a long run. The whole value is checked
# before it is split, and a version is written the one way the runner accepts,
# so `01.0` stops here too.
if [ "${ITK_ACTS_RUN}" = "1" ] || [ "${ITK_ACTS_RUN^^}" = "TRUE" ]; then
  ACTS_VERSION='(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
  if ! [[ "$ITK_ACTS_SPEC_VERSIONS" =~ ^${ACTS_VERSION}(,${ACTS_VERSION})*$ ]]; then
    echo "Error: ITK_ACTS_SPEC_VERSIONS is '${ITK_ACTS_SPEC_VERSIONS}', which is not a comma-separated list of Major.Minor versions such as 1.0,1.1" >&2
    exit 1
  fi
  declare -A ACTS_SEEN=()
  for ACTS_SPEC_VERSION in ${ITK_ACTS_SPEC_VERSIONS//,/ }; do
    if [ -n "${ACTS_SEEN[$ACTS_SPEC_VERSION]:-}" ]; then
      echo "Error: ITK_ACTS_SPEC_VERSIONS lists ${ACTS_SPEC_VERSION} twice" >&2
      exit 1
    fi
    ACTS_SEEN[$ACTS_SPEC_VERSION]=1
  done
fi

# Podman is a drop-in for the handful of commands used here; a2a-java's CI
# runs on it.
if [ -n "${ITK_CONTAINER_RT:-}" ]; then
  CONTAINER_RT="$ITK_CONTAINER_RT"
elif command -v docker &> /dev/null; then
  CONTAINER_RT=docker
elif command -v podman &> /dev/null; then
  CONTAINER_RT=podman
else
  echo "Error: neither docker nor podman found" >&2
  exit 1
fi

cleanup() {
  set +x
  echo "Cleaning up artifacts..."
  $CONTAINER_RT stop itk-service > /dev/null 2>&1 || true
  $CONTAINER_RT rm itk-service > /dev/null 2>&1 || true
  if [ "$ITK_REMOVE_IMAGE" = "1" ]; then
    $CONTAINER_RT rmi itk_service > /dev/null 2>&1 || true
  fi
  if declare -F itk_extra_cleanup > /dev/null; then
    itk_extra_cleanup || true
  fi
  rm -f instruction.proto run_request.json acts_request.json > /dev/null 2>&1 || true
  # Only remove the a2a-itk checkout when the shim put it inside the itk dir
  # (the documented shim does). A caller pointing at a checkout it manages
  # elsewhere keeps it. raw_results.json is deliberately left behind: the
  # nightly workflow and the shadow comparison job both read it after we exit.
  case "$ITK_REPO_DIR" in
    "$ITK_DIR"/*) rm -rf "$ITK_REPO_DIR" > /dev/null 2>&1 || true ;;
  esac
  echo "Done. Final exit code: $RESULT"
}
trap cleanup EXIT

# 1. Single-source the proto. Java's protobuf-maven-plugin and Rust's build.rs
# read it straight out of the a2a-itk checkout, so they opt out.
if [ "$ITK_COPY_PROTO" = "1" ]; then
  cp "$ITK_REPO_DIR/protos/instruction.proto" ./instruction.proto
fi

# 2. Language-specific codegen.
if declare -F itk_generate_protos > /dev/null; then
  itk_generate_protos
fi

# 3. Build the service image. CI skips this: the workflow builds via
# docker/build-push-action to get GHA layer caching.
if [ "${ITK_SKIP_BUILD:-0}" != "1" ]; then
  BUILD_ARGS=()
  if [ "$CONTAINER_RT" = "podman" ]; then
    BUILD_ARGS+=(--format docker)
  fi
  $CONTAINER_RT build "${BUILD_ARGS[@]}" -t itk_service "$ITK_REPO_DIR"
fi

# 4. Start the service.
$CONTAINER_RT rm -f itk-service || true

RUN_ARGS=(-d --name itk-service)
case "$ITK_SELINUX_MOUNT_LABEL" in
  0) ITK_MOUNT_SUFFIX="" ;;
  1)
    ITK_MOUNT_SUFFIX=":Z"
    echo "Warning: SELinux :Z relabels launcher bind mounts, including the SDK checkout and cache; this may affect access by host processes or other containers." >&2
    ;;
  *) echo "Error: ITK_SELINUX_MOUNT_LABEL must be 0 or 1" >&2; exit 1 ;;
esac
RUN_ARGS+=(-v "$SDK_ROOT:/app/agents/repo$ITK_MOUNT_SUFFIX")
if [ "$ITK_MOUNT_ITK_DIR" = "1" ]; then
  RUN_ARGS+=(-v "$ITK_DIR:/app/agents/repo/itk$ITK_MOUNT_SUFFIX")
fi

mkdir -p "$HOME/.cache/a2a-itk-launcher"
RUN_ARGS+=(-v "$HOME/.cache/a2a-itk-launcher:/root/.cache/a2a-itk$ITK_MOUNT_SUFFIX")

if [ "${ITK_LOG_LEVEL^^}" = "DEBUG" ]; then
  mkdir -p "$ITK_DIR/logs"
  RUN_ARGS+=(-v "$ITK_DIR/logs:/app/logs$ITK_MOUNT_SUFFIX")
fi

RUN_ARGS+=(-e ITK_LOG_LEVEL="$ITK_LOG_LEVEL")
RUN_ARGS+=(-e ITK_ENTRYPOINT="${ITK_ENTRYPOINT:-itk_service_v2.py}")
RUN_ARGS+=(-e ITK_READINESS_TIMEOUT="${ITK_READINESS_TIMEOUT:-180}")
RUN_ARGS+=(-e ITK_MAX_WORKERS="${ITK_MAX_WORKERS:-2}")
if [ -n "${ITK_SCENARIO_TIMEOUT:-}" ]; then
  RUN_ARGS+=(-e ITK_SCENARIO_TIMEOUT="$ITK_SCENARIO_TIMEOUT")
fi
RUN_ARGS+=("${ITK_EXTRA_DOCKER_ARGS[@]}")
RUN_ARGS+=(-p 8000:8000 itk_service)

$CONTAINER_RT run "${RUN_ARGS[@]}"

# 4.1. The bind-mounted trees are host-owned, so container-side git refuses
# them as "dubious ownership" — which breaks uv-dynamic-versioning and the
# launcher's peer checkouts. multiPackIndex is disabled because git chokes on
# the mount's pack index.
$CONTAINER_RT exec -u root itk-service git config --system --add safe.directory /app/agents/repo
$CONTAINER_RT exec -u root itk-service git config --system --add safe.directory /app/agents/repo/itk
$CONTAINER_RT exec -u root itk-service git config --system core.multiPackIndex false
$CONTAINER_RT exec -u root itk-service bash -lc \
  'while IFS= read -r -d "" d; do git config --system --add safe.directory "${d%/.git}"; done < <(find /root/.cache/a2a-itk -type d -name .git -print0)'

# 5. Wait for readiness.
MAX_RETRIES=30
echo "Waiting for ITK service to start on 127.0.0.1:8000..."
set +e
for i in $(seq 1 $MAX_RETRIES); do
  if curl -s http://127.0.0.1:8000/health > /dev/null; then
    echo "Service is up!"
    break
  fi
  echo "Still waiting... ($i/$MAX_RETRIES)"
  sleep 2
done

if ! curl -s http://127.0.0.1:8000/health > /dev/null; then
  echo "Error: ITK service failed to start on port 8000"
  $CONTAINER_RT logs itk-service
  exit 1
fi

# 6. Pick the scenario file.
if [ -z "${SCENARIO_FILE:-}" ]; then
  if [ "${ITK_SCENARIO_SET:-local}" = "shared" ]; then
    # The shared, role-based sets that live in this repo. Peers come from
    # matrix.yaml, so the SDK carries no scenario file of its own.
    SCENARIO_FILE="$ITK_REPO_DIR/scenarios/traversal/pr.yaml"
    if [ "${ITK_NIGHTLY_RUN^^}" = "TRUE" ]; then
      SCENARIO_FILE="$ITK_REPO_DIR/scenarios/traversal/nightly.yaml"
    fi
  else
    SCENARIO_FILE="scenarios.json"
    if [ "${ITK_NIGHTLY_RUN^^}" = "TRUE" ]; then
      SCENARIO_FILE="scenarios_full.json"
    fi
  fi
fi

echo "ITK Service is up! Sending compatibility test request using $SCENARIO_FILE..."
# Build the request body inside the container: the shared sets are YAML, and
# a bare CI runner has no PyYAML while the service image already depends on
# it. A legacy scenarios.json passes through unchanged. `sut_sdk` tells the
# service which SDK is under test, so scenarios gated on `test_when` and the
# `include_own_lines` peers resolve for the right one — without it a shared
# set would quietly run a different mix of peers.
# (Under test_suite/, not scripts/ — .dockerignore keeps scripts/ out of the
# image.)
$CONTAINER_RT exec -i -w /app itk-service \
  uv run python -m test_suite.scenarios.build_request \
    --scenarios - --sut-sdk "$ITK_MATRIX_SDK" --output - \
  < "$SCENARIO_FILE" > run_request.json
BUILD_REQUEST_STATUS=$?

# Bail here rather than POSTing whatever landed in the file. An empty body
# comes back as a FastAPI 422 about a missing field, which says nothing about
# the actual problem and sends the reader hunting in the wrong place.
if [ $BUILD_REQUEST_STATUS -ne 0 ] || [ ! -s run_request.json ]; then
  echo "Error: could not build the /run request from $SCENARIO_FILE" >&2
  echo "       (build_request exited $BUILD_REQUEST_STATUS)" >&2
  $CONTAINER_RT logs itk-service
  exit 1
fi

# 6b. ACTS conformance path. Runs instead of the traversal suite: at each
# A2A version in ITK_ACTS_SPEC_VERSIONS, one report per transport. Each report
# is left on disk as `acts-report-<sdk>-<transport>-<ts>.json` for the
# workflow to upload as a build artifact; on the nightly path a lean entry is
# also appended to the version's rolling release asset, `acts_<sdk>.json` for
# 1.0 and `acts_<sdk>_v11.json` for 1.1.
if [ "${ITK_ACTS_RUN}" = "1" ] || [ "${ITK_ACTS_RUN^^}" = "TRUE" ]; then
  RESULT=0
  for ACTS_SPEC_VERSION in ${ITK_ACTS_SPEC_VERSIONS//,/ }; do
    ACTS_SUFFIX="$(acts_suffix "$ACTS_SPEC_VERSION")"
    # Collected across the bindings: every binding this version exercised
    # contributes to ONE history entry, because one commit tested over three
    # transports is one run of the SDK, not three. A version is a run of its
    # own, in a history of its own.
    ACTS_REPORT_ARGS=()
    for ACTS_TRANSPORT in ${ITK_ACTS_TRANSPORTS//,/ }; do
      echo "Running ACTS conformance at A2A ${ACTS_SPEC_VERSION} over ${ACTS_TRANSPORT}..."
      ACTS_RESULTS="acts_results${ACTS_SUFFIX}_${ACTS_TRANSPORT}.json"
      python3 - "$ACTS_TRANSPORT" "$ITK_SDK_REPO" "$ITK_ACTS_LANGUAGE" "$ACTS_SPEC_VERSION" > acts_request.json <<'PY'
import json, sys
transport, repo, language, spec_version = sys.argv[1:5]
json.dump({
    'transport': transport,
    'sdk': repo,
    'language': language,
    'repository': f'https://github.com/a2aproject/{repo}',
    'spec_version': spec_version,
    # The corpus references these and no document defines them; §12.2 leaves
    # supplying them to the runner. Values the SUT should reject.
    'variables': {
        'insufficientAuthToken': 'itk-insufficient-token',
        'otherUserTaskId': '00000000-0000-0000-0000-0000000000ff',
    },
}, sys.stdout)
PY
      curl -s -X POST http://127.0.0.1:8000/run-acts \
        -H "Content-Type: application/json" \
        -d @acts_request.json \
        -o "$ACTS_RESULTS"

      # Validate before anything consumes it, so a FastAPI error envelope
      # cannot reach the metrics processor and land an empty history entry.
      ACTS_ARGS=(--response-file "$ACTS_RESULTS"
                 --title "ACTS CONFORMANCE (A2A ${ACTS_SPEC_VERSION}, ${ACTS_TRANSPORT})")
      if [ "${ITK_NIGHTLY_RUN^^}" != "TRUE" ]; then
        ACTS_ARGS+=(--require-conformant)
      fi
      python3 "$ITK_REPO_DIR/scripts/acts_report.py" "${ACTS_ARGS[@]}" || RESULT=$?

      # Keep the full §13 report under its spec §13.5 name for the workflow
      # to upload; the rolling asset only carries a tally. The version is in
      # the report, as `spec_version`. A response that is not a report is not
      # kept: acts_report.py has said what it is.
      python3 - "$ACTS_RESULTS" <<'PY'
import json, pathlib, sys
try:
    report = json.loads(pathlib.Path(sys.argv[1]).read_text())
    stamp = report['generated_at'].replace('-', '').replace(':', '').split('.')[0]
    name = f"acts-report-{report['sdk']['name']}-{report['transport']}-{stamp}Z.json"
except (OSError, ValueError, LookupError, TypeError):
    sys.exit()
pathlib.Path(name).write_text(json.dumps(report, indent=2))
print(f'ACTS report: {name}')
PY

      ACTS_REPORT_ARGS+=(--report-file "$ACTS_RESULTS")
    done

    if [ "${ITK_NIGHTLY_RUN^^}" = "TRUE" ]; then
      ACTS_HISTORY="acts_${ITK_METRICS_NAME}${ACTS_SUFFIX}.json"
      python3 "$ITK_REPO_DIR/scripts/process_acts_results.py" \
        "${ACTS_REPORT_ARGS[@]}" \
        --history_output_file "$ACTS_HISTORY" \
        --history_url "https://github.com/a2aproject/${ITK_SDK_REPO}/releases/download/nightly-metrics/${ACTS_HISTORY}" \
        || RESULT=$?
    fi
  done
  set -e
  if [ $RESULT -ne 0 ]; then
    echo "ACTS run failed. Container logs:"
    $CONTAINER_RT logs itk-service
  fi
  exit $RESULT
fi

ITK_RUN_TIMEOUT="${ITK_RUN_TIMEOUT:-1800}"
rm -f raw_results.json
curl -s --max-time "$ITK_RUN_TIMEOUT" -X POST http://127.0.0.1:8000/run \
  -H "Content-Type: application/json" \
  -d @run_request.json \
  -o raw_results.json
CURL_STATUS=$?

# 6c. Drop cached peer trees unused for ITK_TREE_TTL or over
# ITK_DISK_BUDGET_BYTES. CI saves this directory with actions/cache, so without
# this step the saved cache grows with every new peer commit.
# - After /run: every tree this run used was touched on its cache hit, so only
#   trees no run has used for the TTL are removed. A restored cache can be
#   days old (a nightly or re-run on the same commit saves nothing), and this
#   order keeps those stale times from evicting peers about to be reused.
# - Only when /run completed: a run that failed or timed out may have touched
#   only some of its trees.
# - Not in an ACTS run, which exits above: it starts no peers.
# - This is housekeeping, so a failure is reported and the run goes on.
if [ $CURL_STATUS -eq 0 ]; then
  $CONTAINER_RT exec -w /app itk-service \
    uv run python -m test_suite.launcher.cache evict \
    || echo "Warning: launcher cache eviction failed; continuing." >&2
fi

# 7. Report. itk_report.py validates the response shape before anything
# consumes it, so a FastAPI error envelope can't reach process_results.py and
# land an empty entry in the published history.
if [ $CURL_STATUS -eq 28 ]; then
  echo "Error: /run did not finish within ${ITK_RUN_TIMEOUT}s (ITK_RUN_TIMEOUT)" >&2
  RESULT=1
elif [ $CURL_STATUS -ne 0 ]; then
  echo "Error: POST /run failed (curl exit $CURL_STATUS)" >&2
  RESULT=1
elif [ "${ITK_NIGHTLY_RUN^^}" = "TRUE" ]; then
  echo "Nightly run detected. Saving raw results and running process_results.py..."
  python3 "$ITK_REPO_DIR/scripts/itk_report.py" \
    --response-file raw_results.json \
    --title "NIGHTLY ITK SUMMARY"
  RESULT=$?
  if [ $RESULT -eq 0 ]; then
    # process_results.py reads ./raw_results.json and owns the exit code on
    # the nightly path — scenario failures are recorded as metrics, not
    # treated as a broken run.
    python3 "$ITK_REPO_DIR/scripts/process_results.py" \
      --history_output_file "itk_${ITK_METRICS_NAME}.json" \
      --history_url "https://github.com/a2aproject/${ITK_SDK_REPO}/releases/download/nightly-metrics/itk_${ITK_METRICS_NAME}.json"
    RESULT=$?
  fi
else
  python3 "$ITK_REPO_DIR/scripts/itk_report.py" \
    --response-file raw_results.json \
    --title "ITK TEST RESULTS" \
    --require-all-passed
  RESULT=$?
fi
set -e

if [ $RESULT -ne 0 ]; then
  echo "Tests failed. Container logs:"
  $CONTAINER_RT logs itk-service
fi
echo "--------------------------------------------------------"

exit $RESULT
