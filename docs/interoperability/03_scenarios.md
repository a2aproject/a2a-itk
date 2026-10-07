# Scenarios, the matrix and known failures

Three files decide what an ITK run tests:

| File | Says |
| --- | --- |
| a scenario file | which graphs to walk, over which transports, with which behavior |
| [`matrix.yaml`](../../matrix.yaml) | what each peer identifier means: repo, ref, and which transports that line can speak at all |
| [`known_failures.yaml`](../../known_failures.yaml) | which combinations are known broken and are left out, each with a stated reason |

## Scenario formats

Two formats are accepted everywhere a scenario can be given (`run_tests.py --scenarios`, `POST /run`), and one file or request may mix them. A scenario with a top-level `schema:` key is `traversal/v1`; one without is legacy.

### Legacy

What each SDK's `itk/scenarios.json` used before the shared sets. Agents are named by identifier and edges are written by hand, with indices into `sdks`.

```json
{"tests": [{
  "name": "Star Topology (Full) - JSONRPC & GRPC",
  "sdks": ["current", "python_v10", "go_v03"],
  "edges": ["0->1", "0->2", "1->0", "2->0"],
  "protocols": ["jsonrpc", "grpc"],
  "behavior": "send_message",
  "streaming": false,
  "build_subtests": false
}]}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Result key. Must be unique within a run |
| `sdks` | yes | Agent identifiers. Index 0 is where the traversal starts |
| `behavior` | yes | `send_message`, `push_notification` or `resubscribe` |
| `protocols` | no | Transports to walk, one circuit each in the same scenario. Default: all three |
| `edges` | no | Directed edges as `"i->j"`. Default: complete digraph |
| `streaming` | no | Stream every hop. Default `false` |
| `build_subtests` | no | Also run every balanced induced subgraph containing agent 0, as separate results |

Note that `protocols: [jsonrpc, grpc]` here is **one** scenario walking two circuits; it passes only if both do, and the result name does not say which one failed. The new format splits them.

### `traversal/v1`

Names roles instead of agents and resolves them against `matrix.yaml` when the run starts. The same scenario:

```yaml
schema: traversal/v1
name: Star - send message
tier: pr
roles:
  sut: current
  peers:
    - {sdk: python, line: v10}
    - {sdk: go, line: v03}
topology: star
transports: [jsonrpc, grpc]
behavior: send_message
```

This produces two executable scenarios, `Star - send message - jsonrpc` and `Star - send message - grpc`. Only axes that actually vary add a suffix, so a scenario with singular fields keeps its name exactly - which matters because the nightly history is a time series per name.

| Field | Default | Meaning |
| --- | --- | --- |
| `schema` | - | Must be `traversal/v1` |
| `name` | - | Base name. `{peer}` is replaced with the peer id under `expand: per_peer` |
| `tier` | `nightly` | `pr` or `nightly`. Informational; recorded on each result |
| `roles.sut` | `current` | Only `current` is allowed |
| `roles.peers` | - | A list of `{sdk, line, instance?}` or the string `all` |
| `roles.include_sut` | `true` | `false` makes a peer-only scenario (the smoke set does this) |
| `roles.include_own_lines` | `false` | Also add the SUT's own SDK's released lines as peers. Needs `sut_sdk` |
| `topology` | `star` | `star` (SUT <-> each peer), `chain` (a cycle through all), `euler` (complete digraph) |
| `edges` | - | Explicit edge list, overrides `topology`. Positional, so a trimmed peer cannot be re-indexed - such a scenario is skipped instead |
| `transports` | all three | Each becomes its own scenario |
| `transport_sets` | - | `[[jsonrpc, grpc], [http_json]]`: explicit groups, one scenario per group. Mutually exclusive with `transports` |
| `behavior` / `behaviors` | - | One, or a list that expands. Exactly one of the two |
| `streaming` / `streaming_variants` | `false` | One, or a list that expands. Exactly one of the two |
| `expand` | `together` | `together`: every peer in one graph. `per_peer`: one SUT-plus-one scenario per peer |
| `test_when.sut_sdk` | - | Only run when the SUT is one of these SDKs |
| `build_subtests` | `false` | As in legacy |

`peers: all` is what makes adding an SDK a one-file change: put it in `matrix.yaml` and every `all` scenario picks it up.

`instance: 2` on a peer gives a second copy of it (`python_v10_2`) on its own ports, for same-SDK-talks-to-itself graphs.

### The shared sets

| File | Tier | Shape |
| --- | --- | --- |
| [`scenarios/traversal/pr.yaml`](../../scenarios/traversal/pr.yaml) | pr | Star of the SUT against `python_v10`, `python_v03`, `go_v10`, `go_v03` plus the SUT's own lines. Three declarations: send message (both streaming variants), push notification, resubscribe. Every transport |
| [`scenarios/traversal/nightly.yaml`](../../scenarios/traversal/nightly.yaml) | nightly | The SUT against every peer in the matrix, one pair per scenario (`peers: all`, `expand: per_peer`). Same three behaviors |
| [`scenarios/traversal/smoke.yaml`](../../scenarios/traversal/smoke.yaml) | - | Peer-only Python <-> Go. The default for `run_tests.py`; runs with nothing but this repo checked out |
| [`scenarios/smoke.json`](../../scenarios/smoke.json) | - | The legacy twin of `smoke.yaml`, kept so the two formats stay provably equivalent |

The PR set names its peers explicitly rather than using `all` because a nine-agent star per scenario has not been costed for the PR tier. The nightly already runs the full matrix.

## `matrix.yaml`

Maps `(sdk, line)` to a repository and ref:

```yaml
sdks:
  python:
    v10: {repo: a2aproject/a2a-python, ref: main}
    v03: {repo: a2aproject/a2a-python, ref: "v0.3.24+itk"}
  go:
    v03: {repo: a2aproject/a2a-go, ref: "v0.3.15+itk", transports: [jsonrpc, grpc]}
  dotnet:
    v10: {repo: a2aproject/a2a-dotnet, ref: main, transports: [jsonrpc, http_json]}
```

- `ref` is resolved to a commit SHA when a run is planned. `main` means "whatever main is tonight", which is what the nightly wants; a tag pins the peer.
- `v03` lines point at `+itk` tags: a released 0.3.x of the SDK with the ITK agent added under `itk/`. The 0.3 releases predate ITK, so the agent had to be grafted on.
- `transports` is a **capability ceiling**: the line cannot speak anything not listed, to anyone. A peer drops out of a transport it cannot speak rather than failing the scenario. Omitted means all three.
- `current` is not in this file. It is always the mounted checkout.

Use `transports` here only when a line genuinely cannot serve a transport at all (`go_v03` has no HTTP+JSON server; `dotnet` has no gRPC server). A pair that fails over some transport while other pairs pass belongs in `known_failures.yaml` instead, because a ceiling here would also hide the pairings that do work.

## `known_failures.yaml`

A list of exclusions, matched against resolved scenarios just before they run. All fields are optional except `reason`; those given must all match.

```yaml
exclusions:
  - sut_sdk: [ts]
    agents: [python_v03, go_v03]
    transports: [grpc, http_json]
    reason: >
      The TypeScript v0.3 compat layer handles JSON-RPC against other SDKs
      but not gRPC or HTTP+JSON.
    issue: https://github.com/a2aproject/a2a-js/issues/NNN
```

| Field | Matches when |
| --- | --- |
| `agents` | the scenario contains any of these agent identifiers |
| `transports` | the scenario's transport is one of these |
| `behaviors` | the scenario's behavior is one of these |
| `streaming` | the scenario's streaming flag equals this |
| `sut_sdk` | the run's `sut_sdk` is one of these |
| `unless_sut_sdk` | the run's `sut_sdk` is **not** one of these |
| `reason` | always required. Logged on every run |
| `issue` | optional tracking link |

What happens on a match:

- If the exclusion names `agents`, those peers are **removed from the graph** and the scenario still runs. A star with one arm gone still tests the others. This is reported as "trimmed", separately from skips, because the scenario now covers less than its file says.
- If removing them would leave fewer than two agents, or the scenario has an explicit `edges` list that cannot be re-indexed, the scenario is skipped.
- If the exclusion names no `agents` (for example "dotnet as SUT over gRPC"), the whole scenario is skipped.

Why exclusions exist rather than per-scenario markers: with `peers: all` there is no line in any file to annotate. And why every one is logged: an exclusion nobody sees is the same thing as coverage that silently vanished.

Most entries are about 0.3 interoperability, where the compatibility layer lives in whichever SDK drives the hop. That makes the limit a property of the (SUT, peer) **pair**, which `sut_sdk` and `unless_sut_sdk` can express and a per-line ceiling in `matrix.yaml` cannot. The file itself is the list; every entry carries a `reason`, and [06_status.md](06_status.md#known-failures) describes the kinds of entry.

## Tooling

### Validate before running

A malformed scenario would otherwise surface only after CI built the image and started every peer; one that resolves to nothing would go green having tested nothing.

```bash
uv run python -m test_suite.scenarios.validate scenarios/
uv run python -m test_suite.scenarios.validate --resolve scenarios/traversal/
uv run python -m test_suite.scenarios.validate ../a2a-go/itk/scenarios.json
```

`--resolve` also binds roles against `matrix.yaml` and prints how many executable scenarios each file expands to, which catches a peer that was removed from the matrix.

### Check that coverage did not shrink

When a repo moves from its own `scenarios.json` to a shared set, or when a shared set is edited, this checks that nothing previously exercised went away. The unit of comparison is a hop - `(caller, callee, transport, behavior, streaming)` - because the shared sets reshape scenarios freely and a file-level diff cannot see through that.

```bash
uv run python scripts/scenarios_diff.py \
    --old ../a2a-python/itk/scenarios.json \
    --new scenarios/traversal/pr.yaml --sut-sdk python
```

Added coverage is reported and never fails. Lost coverage fails unless `matrix.yaml` or `known_failures.yaml` explains it.

### Dry run

```bash
uv run run_tests.py --scenarios scenarios/traversal/nightly.yaml --sut-sdk rust --dry-run
```

Prints which agents would be started and which scenarios would run, after resolution and exclusions, without touching the network. The quickest way to see what a change to any of the three files actually does.
