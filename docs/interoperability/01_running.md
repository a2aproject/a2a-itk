# Running ITK

There are three ways to run the traversal suite. All three go through the same pipeline ([`itk_runner.py`](../../itk_runner.py)), so a scenario behaves the same whichever one you pick.

| Way | Use it for |
| --- | --- |
| `uv run run_tests.py` | Day-to-day local runs. No container, no SDK checkout needed unless you mount one |
| The ITK container | Same CLI, but with every SDK toolchain preinstalled |
| The HTTP service (`POST /run`) | What CI uses. Also useful to reproduce a CI run exactly |

## Prerequisites

- [uv](https://docs.astral.sh/uv/). Everything Python in this repo runs through it.
- The toolchain of every peer you select. Builds happen on your machine with the SDK's native tools:

  | Peer | Needs |
  | --- | --- |
  | `python_*` | uv |
  | `go_*` | Go 1.25+, `protoc`, `protoc-gen-go`, `protoc-gen-go-grpc` |
  | `ts_*` | Node.js 20+ and npm |
  | `java_*` | JDK 17 and Maven 3.9+ |
  | `rust_*` | cargo (Rust 1.98 is what the image ships) |
  | `dotnet_*` | .NET SDK 10 plus the ASP.NET Core 8.0 runtime |

  The bundled smoke set uses only Python and Go peers for this reason. If you do not want to install toolchains, use the container (below).

- Network access to GitHub on the first run of each peer. Builds are cached afterwards.

## Local CLI

```bash
uv run run_tests.py                                            # bundled smoke set
uv run run_tests.py --scenarios scenarios/traversal/pr.yaml    # the shared PR set
uv run run_tests.py --scenarios ~/Source/a2a-go/itk/scenarios.json   # any legacy file
uv run run_tests.py --sdks python_v10,go_v10                   # keep only scenarios using these peers
uv run run_tests.py --list-sdks                                # what matrix.yaml can resolve
uv run run_tests.py --dry-run                                  # plan only, no network
```

To test a local SDK checkout as the SUT, point `--mount` at its `itk/` directory. The shared sets all name `current`, so they need this:

```bash
uv run run_tests.py --mount ~/Source/a2a-python/itk \
                    --scenarios scenarios/traversal/pr.yaml \
                    --sut-sdk python
```

`--sut-sdk` tells the resolver which SDK the mounted checkout is. It matters for two scenario features: `test_when` (restrict a scenario to certain SUTs) and `include_own_lines` (add the SUT's own released lines as peers). Without it those are simply not applied.

Other flags:

| Flag | Effect |
| --- | --- |
| `--log-dir DIR` | Write each agent's stdout/stderr to `DIR/agent_<id>.log`. The first thing to add when an agent will not start |
| `--output FILE` | Write results as JSON, in the same shape as the `/run` response |

Exit code is 0 when every scenario passed, 1 otherwise.

### What a run looks like

```
Planning cluster for 5 scenario(s): ['go_v10', 'python_v10']
Cluster up: AgentTable(go_v10=:41231, python_v10=:41229)
Executing scenario 'Smoke - python v1.0 <-> go v1.0 - all transports'
...
[1/5] Smoke - backwards compat python v0.3 <-> v1.0           PASS
[2/5] Smoke - push notification - python v1.0 <-> go v1.0    PASS
...
5/5 scenarios passed — OK
```

The cluster is started once for the whole file (the union of every agent any scenario names) and torn down at the end. Scenarios run one after another against it.

### The build cache

Fetched peers are built once and reused. The cache lives under `$ITK_CACHE_DIR`, default `~/.cache/a2a-itk`. A cold run of the smoke set takes a few minutes; a warm one starts agents in seconds.

The cache key includes the peer's commit SHA, the digest of the ITK image and a hash of `protos/instruction.proto`, so a changed proto or a rebuilt image invalidates old trees automatically. Trees unused for seven days are evicted (`ITK_TREE_TTL`), and total size is capped at 50 GB (`ITK_DISK_BUDGET_BYTES`).

To force a rebuild of one peer, delete its directory under `~/.cache/a2a-itk/trees/`.

### When something does not start

- Re-run with `--log-dir logs`. Each agent's output lands in its own file.
- A peer that fails to build or start is **dropped**, not fatal: scenarios that can lose it run without it, the rest are skipped, and both are listed as warnings. Only the SUT failing to start aborts the run.
- `Cluster startup failed ... the code under test ('current') failed to start` usually means the mounted agent has no generated proto stubs. In CI the SDK's `run_itk.sh` generates them before the container starts; locally you have to. See [05_sdk-integration.md](05_sdk-integration.md#2-proto-stubs).
- `git ls-remote` timeouts are retried three times (`ITK_RETRIES`), then the run fails with an infrastructure error. A ref that does not exist fails immediately.

## In the container

The image has every toolchain. Build it once from the repo root and run the same CLI inside it:

```bash
docker build -t itk_service .

docker run --rm \
  -v "$PWD/scenarios:/scenarios" \
  -v "$HOME/.cache/a2a-itk-launcher:/root/.cache/a2a-itk" \
  itk_service uv run run_tests.py --scenarios /scenarios/smoke.json
```

The second mount keeps the build cache on the host so repeated runs stay fast. It is the same directory the CI driver uses.

To mount a local SDK as the SUT, bind the SDK repo at `/app/agents/repo` (the launcher expects the agent at `/app/agents/repo/itk`):

```bash
docker run --rm \
  -v "$HOME/Source/a2a-python:/app/agents/repo" \
  -v "$HOME/.cache/a2a-itk-launcher:/root/.cache/a2a-itk" \
  itk_service uv run run_tests.py \
    --scenarios scenarios/traversal/pr.yaml --sut-sdk python --mount /app/agents/repo/itk
```

## The HTTP service

Running the image with no command starts the service on port 8000. This is what every SDK's `run_itk.sh` talks to.

```bash
docker run -d --name itk-service -p 8000:8000 \
  -v "$HOME/Source/a2a-python:/app/agents/repo" \
  -v "$HOME/.cache/a2a-itk-launcher:/root/.cache/a2a-itk" \
  itk_service

curl http://127.0.0.1:8000/health          # {"status":"ok"}
```

### `POST /run`

Request:

```json
{
  "tests": [ ...scenario objects, legacy or traversal/v1, mixed is fine... ],
  "sut_sdk": "python"
}
```

Response:

```json
{
  "all_passed": false,
  "results": {
    "PR - star - send message - jsonrpc - non-streaming": {
      "passed": true,
      "sdks": ["current", "python_v10", "go_v10"],
      "edges": ["0->1", "1->0", "0->2", "2->0"],
      "protocols": ["jsonrpc"],
      "behavior": "send_message",
      "streaming": false,
      "tier": "pr"
    }
  },
  "startup": null
}
```

`startup` is `null` on a clean run. When a peer failed to start it lists `dropped_peers`, the scenarios that ran `trimmed` and those `skipped`. When the SUT failed to start, every scenario is reported failed and `startup.cluster_error` says why; this is returned as a 200 on purpose so the nightly history records a red night instead of a gap.

Status codes:

| Code | Meaning |
| --- | --- |
| 400 | Malformed scenario, unknown peer, unresolvable ref, or every scenario resolved to nothing runnable |
| 502 | Transient fetch failure (`git ls-remote` kept timing out). Safe to retry |
| 500 | Anything else. Check the container log |

Scenario files in YAML have to be converted to this JSON body first. The driver does it inside the container with `python -m test_suite.scenarios.build_request`, see [04_ci.md](04_ci.md).

Two things about the service are frozen because every SDK's script depends on them: `GET /health` and port 8000, and the `passed`/`sdks`/`edges` keys of each result. New fields are only ever added.

## Unit tests

```bash
uv run pytest            # launcher, resolver, traversal, service - no network
uv run ruff check test_suite tests scripts
```

## Environment variables

Everything the launcher does can be tuned without code changes. The ones you are most likely to touch:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ITK_CACHE_DIR` | `~/.cache/a2a-itk` | Build cache root |
| `ITK_MOUNT_DIR` | `/app/agents/repo/itk` | Where `current` is served from. `--mount` sets this |
| `ITK_LOG_LEVEL` | `INFO` | `DEBUG` also makes the service capture agent logs under `/app/logs` |
| `ITK_READINESS_TIMEOUT` | `35` s | How long to wait for an agent's card after spawn. The CI driver sets 180 |
| `ITK_SCENARIO_TIMEOUT` | `60` s | How long one scenario (or one subtest) may run before it is recorded as failed and the run moves on. Needed because an SSE keep-alive resets the HTTP read timeout, so a stuck hop would otherwise hold the run open |
| `ITK_MAX_WORKERS` | `max(4, n)` | Parallel builds/spawns. Set 2-3 on small CI runners to avoid OOM |
| `ITK_BUILD_TIMEOUT` | 10 min | Per-peer build budget |
| `ITK_CHECKOUT_TIMEOUT` | 5 min | Per-peer fetch budget |
| `ITK_RETRIES` | 3 | Retries for transient git failures |
| `ITK_TREE_TTL` | 7 days | Cache eviction age |
| `ITK_DISK_BUDGET_BYTES` | 50 GB | Cache size cap |
| `ITK_TEARDOWN_GRACE` | 10 s | SIGTERM-to-SIGKILL grace per agent |
| `ITK_ENTRYPOINT` | `itk_service_v2.py` | Which script the container runs |

The full list, with the reasoning behind each default, is in [`test_suite/launcher/config.py`](../../test_suite/launcher/config.py).
