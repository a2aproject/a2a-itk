# ITK scripts

| Script | Runs where | Purpose |
| --- | --- | --- |
| `run_itk_shared.sh` | SDK repo, host | Shared driver for each SDK's `itk/run_itk.sh` |
| `itk_report.py` | SDK repo, host | Validates and summarises a `/run` response |
| `process_results.py` | SDK repo, host | Merges nightly results into the published history |
| `scenarios_diff.py` | a2a-itk, CI | Checks the shared scenario set still covers each SDK's legacy set |

## `run_itk_shared.sh`

One driver for all five SDKs. The per-SDK scripts were ~70% identical and had
drifted in the rest — three different result summarisers, only one response
validator, inconsistent git `safe.directory` handling. Adopting it is opt-in
per SDK; nothing changes for a repo that keeps its current script.

Your `itk/run_itk.sh` clones `a2a-itk` (it cannot source the shared script
otherwise), sets a few variables, optionally defines two hook functions, then
sources the script as its last statement.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `ITK_SDK_NAME` | *required* | `python` \| `go` \| `js` \| `java` \| `rust` |
| `ITK_SDK_REPO` | `a2a-$ITK_SDK_NAME` | GitHub repo name. Rust must override — its repo is `a2a-rs` |
| `ITK_METRICS_NAME` | `$ITK_SDK_NAME` | Nightly history basename → `itk_<name>.json` |
| `ITK_MATRIX_SDK` | `$ITK_SDK_NAME` | This SDK's key in `matrix.yaml`. a2a-js must override — it is `ts` there |
| `ITK_COPY_PROTO` | `1` | Copy `protos/instruction.proto` into the itk dir. Java and Rust set `0` — their builds read it from the checkout |
| `ITK_MOUNT_ITK_DIR` | `1` | Bind-mount the itk dir over `/app/agents/repo/itk`. Java sets `0` |
| `ITK_REMOVE_IMAGE` | `0` | Delete the `itk_service` image on exit |
| `ITK_EXTRA_DOCKER_ARGS` | `()` | Bash array of extra `docker run` arguments |
| `ITK_CONTAINER_RT` | autodetect | Force `docker` or `podman` |
| `ITK_SCENARIO_SET` | `local` | `local` = the SDK's own `scenarios*.json`; `shared` = the role-based sets in a2a-itk |
| `SCENARIO_FILE` | see below | An explicit file, overriding both |
| `ITK_ACTS_RUN` | `0` | Run the ACTS conformance suite **instead of** the traversal suite |
| `ITK_ACTS_TRANSPORTS` | `jsonrpc` | Comma-separated bindings to run ACTS over, e.g. `jsonrpc,grpc,rest` |
| `ITK_ACTS_LANGUAGE` | `$ITK_SDK_NAME` | Language string for the report's `sdk-info` (spec §13.1) |
| `ACTS_HISTORY_LIMIT` | `7` | Runs kept in the rolling ACTS history. Lower than ITK's 50 — an ACTS entry is far larger |

### ACTS conformance runs

`ITK_ACTS_RUN=1` swaps the traversal suite for the ACTS conformance corpus.
Orthogonal to `ITK_NIGHTLY_RUN`, which still decides whether results are
*recorded* — so the four combinations all mean something:

| | `ITK_NIGHTLY_RUN` unset | `ITK_NIGHTLY_RUN=true` |
| --- | --- | --- |
| `ITK_ACTS_RUN=0` | traversal, gate on `all_passed` | traversal, append to `itk_<sdk>.json` |
| `ITK_ACTS_RUN=1` | ACTS, gate on `must` conformance | ACTS, append to `acts_<sdk>.json` |

Each transport produces two files in the itk directory:

- `acts-report-<sdk>-<transport>-<timestamp>.json` — the full spec §13 report,
  with per-test failures, expected/actual and assertion paths. **This is the
  file to upload as a workflow artifact**; it is what someone reads to find
  out *why* a test failed.
- `acts_results_<transport>.json` — the same document under a stable name, so
  the following steps needn't glob for a timestamp.

On the nightly path **one** entry is appended to `acts_<metrics-name>.json`,
which the workflow re-uploads to the `nightly-metrics` release exactly as it
already does for `itk_<metrics-name>.json`. One commit tested over three
bindings is one run of the SDK, so the bindings nest under `results` rather
than becoming three entries — otherwise the 50-run window would cover ~17
nights, and every consumer would have to re-group by `commit_sha` to answer
"how did last night go".

`conformant` at the top is the conjunction over bindings: passing on JSON-RPC
and failing on gRPC is not conformant. `summary` is the sum, for a single
trend line; per-binding detail sits under `results.<transport>`.

Each test contributes `{id, level, result}`, plus the `failure` detail
(message, expected, actual, assertion path, failing step) when it did not pass
and the `skip_reason` when it was skipped. A history that only recorded
`"result": "fail"` could not answer the question anyone brings to it — is this
the same failure as last night, or a new one.

Left out on purpose: `name` (static per test id, so duplicated across every
run in the window), `duration_ms` (noise at this resolution) and `steps` (the
bulkiest field by far, and already in the per-run report the workflow keeps as
an artifact). Including `steps` would more than double the asset.

Measured against the real corpus, three bindings per run:

| what the entry carries | per run | 7-run window |
| --- | --- | --- |
| verdicts only | 32 KB | 224 KB |
| **+ failure detail and skip reasons (current)** | **62 KB** | **434 KB** |
| + name and duration | 80 KB | 560 KB |
| + full per-step breakdown | 145 KB | 1.0 MB |

The window is **7 runs**, not ITK's 50. An ACTS entry covers every binding and
carries each test's failure detail, so it is an order of magnitude larger than
a traversal entry — and a week is the window anyone reads, since "did last
night regress" needs yesterday, not last quarter. Raise `ACTS_HISTORY_LIMIT`
with the table above in mind: the asset is fetched by a browser on every
dashboard load.

```bash
ITK_ACTS_RUN=1 ITK_ACTS_TRANSPORTS=jsonrpc,grpc,rest ./itk/run_itk.sh
```

### Choosing scenarios

`local` (the default) keeps today's behaviour exactly: `scenarios.json`, or
`scenarios_full.json` when `ITK_NIGHTLY_RUN=true`.

`shared` reads `scenarios/traversal/pr.yaml` (or `nightly.yaml`) out of the
a2a-itk checkout, so the SDK repo carries no scenario file at all:

```bash
ITK_SCENARIO_SET=shared bash itk/run_itk.sh
ITK_SCENARIO_SET=shared ITK_NIGHTLY_RUN=true bash itk/run_itk.sh
```

Or name one directly — JSON or YAML, either schema:

```bash
SCENARIO_FILE=a2a-itk/scenarios/traversal/pr.yaml bash itk/run_itk.sh
```

The request body is built by `test_suite/scenarios/build_request.py` **inside
the container**, because reading YAML needs PyYAML and a bare CI runner has no
guarantee of it while the service image already depends on it. `ITK_MATRIX_SDK`
is passed through as `sut_sdk` so `test_when` and `include_own_lines` resolve
for the right SDK — without it a shared set would quietly run a different peer
mix.

> Everything in *this* directory runs on the host: `.dockerignore` excludes
> `scripts/` from the image. Anything that has to run inside the container
> belongs under `test_suite/`, which is why `build_request.py` lives there.

Unchanged and still honoured: `A2A_ITK_REVISION`, `ITK_ENTRYPOINT`,
`ITK_LOG_LEVEL`, `ITK_NIGHTLY_RUN`, `ITK_SKIP_BUILD`, `ITK_READINESS_TIMEOUT`,
`ITK_MAX_WORKERS`.

### Hooks

| Function | When |
| --- | --- |
| `itk_generate_protos` | After the proto copy, before the image build. Language-specific codegen |
| `itk_extra_cleanup` | On exit, from the itk dir. Extra generated paths to remove |

Both run with the itk directory as the working directory.

### Bootstrap

Every shim opens with the same clone block — unavoidable, since the shared
script lives in the repo being cloned:

```bash
: "${A2A_ITK_REVISION:?A2A_ITK_REVISION environment variable must be set}"
if [ ! -d a2a-itk ]; then
  git clone https://github.com/a2aproject/a2a-itk.git a2a-itk
fi
(cd a2a-itk && git fetch origin && git checkout "$A2A_ITK_REVISION" \
  && { git symbolic-ref -q HEAD > /dev/null && git pull origin "$A2A_ITK_REVISION" || true; })
```

### Shims

**a2a-python/itk/run_itk.sh**

```bash
#!/bin/bash
set -e
cd "$(dirname "${BASH_SOURCE[0]}")"

ITK_SDK_NAME=python

itk_generate_protos() {
  mkdir -p pyproto && touch pyproto/__init__.py
  uv run --with grpcio-tools python -m grpc_tools.protoc \
      -I. --python_out=pyproto --grpc_python_out=pyproto instruction.proto
  sed -i 's/^import instruction_pb2 as instruction__pb2/from . import instruction_pb2 as instruction__pb2/' \
      pyproto/instruction_pb2_grpc.py
}
itk_extra_cleanup() { rm -rf pyproto; }

# <bootstrap>
source a2a-itk/scripts/run_itk_shared.sh
```

**a2a-go/itk/run_itk.sh**

```bash
#!/bin/bash
set -e
cd "$(dirname "${BASH_SOURCE[0]}")"

ITK_SDK_NAME=go
# The SDK v2 depends on its v0.x predecessor, so both register the same proto
# names globally; downgrade the clash from fatal to a warning.
ITK_EXTRA_DOCKER_ARGS=(-e GOLANG_PROTOBUF_REGISTRATION_CONFLICT=warn)

itk_generate_protos() {
  export GOBIN="$HOME/go/bin" PATH="$PATH:$HOME/go/bin"
  go install google.golang.org/protobuf/cmd/protoc-gen-go@latest
  go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest
  mkdir -p pb
  protoc -I. \
      --go_out=pb --go_opt=Minstruction.proto=github.com/a2aproject/a2a-go/itk/pb --go_opt=paths=source_relative \
      --go-grpc_out=pb --go-grpc_opt=Minstruction.proto=github.com/a2aproject/a2a-go/itk/pb --go-grpc_opt=paths=source_relative \
      instruction.proto
  # go.sum is committed so the agent builds reproducibly; -diff fails instead
  # of silently rewriting it the way `go mod tidy` would.
  go mod tidy -diff
}
itk_extra_cleanup() { rm -rf pb; }

# <bootstrap>
source a2a-itk/scripts/run_itk_shared.sh
```

**a2a-js/itk/run_itk.sh**

```bash
#!/bin/bash
set -e
cd "$(dirname "${BASH_SOURCE[0]}")"

ITK_SDK_NAME=js
ITK_MATRIX_SDK=ts   # a2a-js is 'ts' in matrix.yaml (agent ids ts_v10 / ts_v03)

itk_generate_protos() {
  mkdir -p pyproto && touch pyproto/__init__.py
  uv run --with grpcio-tools python -m grpc_tools.protoc \
      -I. --python_out=pyproto --grpc_python_out=pyproto instruction.proto
  sed -i 's/^import instruction_pb2 as instruction__pb2/from . import instruction_pb2 as instruction__pb2/' \
      pyproto/instruction_pb2_grpc.py
  # itk_agent.ts's bindings, via the SDK's own buf.gen.yaml (out: ./pb, in: ./protos)
  mkdir -p protos && cp instruction.proto protos/instruction.proto
  ../node_modules/.bin/buf generate
  rm -rf protos
}
itk_extra_cleanup() { rm -rf pyproto pb protos; }

# <bootstrap>
source a2a-itk/scripts/run_itk_shared.sh
```

**a2a-java/itk/run_itk.sh**

```bash
#!/bin/bash
set -e
cd "$(dirname "${BASH_SOURCE[0]}")"

ITK_SDK_NAME=java
# protobuf-maven-plugin reads the proto from the checkout directly
# (`a2a.itk.proto.dir` in itk/pom.xml), and the repo-root mount already
# exposes itk/, so neither the copy nor the second mount is needed.
ITK_COPY_PROTO=0
ITK_MOUNT_ITK_DIR=0

# <bootstrap>
source a2a-itk/scripts/run_itk_shared.sh
```

**a2a-rs/itk/run_itk.sh**

```bash
#!/bin/bash
set -e
cd "$(dirname "${BASH_SOURCE[0]}")"

ITK_SDK_NAME=rust
ITK_SDK_REPO=a2a-rs   # repo name doesn't follow a2a-<sdk>
ITK_COPY_PROTO=0      # build.rs reads the proto from the checkout

# <bootstrap>
source a2a-itk/scripts/run_itk_shared.sh
```

### Behaviour differences from the old per-SDK scripts

Adopting the shared script changes four things. All are fixes; none affect
which scenarios run.

1. **Per-scenario PASS/FAIL is now accurate.** python, go and java tested
   `if passed:` where `passed` is the result *object* — always truthy, so
   every scenario printed `PASSED` regardless of outcome. Only the
   `OVERALL STATUS` line was correct. js and rust had already fixed this.
2. **Every SDK validates the response** before consuming it. Previously only
   rust did, so elsewhere a FastAPI `{"detail": ...}` error could reach
   `process_results.py` and write an empty entry to the published history.
3. **The image is kept by default** (`ITK_REMOVE_IMAGE=0`, rust's behaviour),
   so local re-runs reuse the layer cache.
4. **Readiness polls `/health`** rather than `/`, which 404s. Same signal,
   and it matches the endpoint the service documents as frozen for this.
