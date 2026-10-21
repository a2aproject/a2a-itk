# Running ACTS

Three ways, one pipeline ([`acts_runner.py`](../../acts_runner.py)):

| Way | Use it for |
| --- | --- |
| `uv run run_acts.py` | Local runs against an SDK checkout. Prints a readable report |
| The ITK container | Same CLI with every toolchain preinstalled |
| The HTTP service (`POST /run-acts`) | What CI uses; returns the report document |

Unlike the traversal suite, ACTS has no peer-only mode, because the thing under test is the one agent you point it at: either an SDK checkout that the harness builds and starts, or an agent that is already running at a URL (see [Against an already-running agent](#against-an-already-running-agent)).

## Prerequisites

- uv, and the toolchain of the SDK you are testing (see the table in [interoperability/01_running.md](../interoperability/01_running.md#prerequisites)).
- An SDK checkout with an ITK agent under `itk/`. The CLI generates the agent's proto stubs for you before starting it (`--no-codegen` turns that off), so a fresh clone works.

## Local CLI

```bash
uv run run_acts.py --mount ../a2a-python/itk --sdk a2a-python --language python
```

`--mount` is the SDK's `itk/` directory; `--sdk` and `--language` are recorded in the report and nothing else. The default binding is JSON-RPC.

```bash
# every binding - each gets a freshly started SUT, as the nightly does
uv run run_acts.py --mount ../a2a-python/itk --sdk a2a-python --transport all

# two bindings
uv run run_acts.py --mount ../a2a-go/itk --sdk a2a-go --transport grpc --transport rest

# one test, for iterating on a failure
uv run run_acts.py --mount ../a2a-python/itk --sdk a2a-python -t CORE-SEND-001 -t CORE-SEND-002

# write the section 13 report file(s) as well
uv run run_acts.py --mount ../a2a-rs/itk --sdk a2a-rs --transport all --out reports/

# machine-readable: print the report(s) as JSON instead of the summary
uv run run_acts.py --mount ../a2a-python/itk --sdk a2a-python --json
```

| Flag | Effect |
| --- | --- |
| `--transport BINDING` | `jsonrpc`, `grpc`, `rest` or `all`. Repeatable. Default `jsonrpc` |
| `-t ID` | Run only this test id. Repeatable. A test that does not target a requested binding is skipped for that binding with a note |
| `--suite FILE` | An alternative `*.acts.yaml` manifest instead of the mirrored corpus |
| `--out DIR` | Write `acts-report-<sdk>-<transport>-<timestamp>.json` into `DIR` |
| `--sut-url URL` | Instead of `--mount`: run against an agent that is already running. See [Against an already-running agent](#against-an-already-running-agent) |
| `--reduced-sut-url URL`, `--auth-sut-url URL` | With `--sut-url`: the instances that stand in for the two deviation SUTs |
| `--behaviors FILE` | Read the `tck-*` contract from this file instead of the checkout's `acts/sut-behaviors.yaml` |
| `--no-gate` | Ignore the SDK's `acts/sut-behaviors.yaml`. Tests needing an undeclared `tck-*` prefix then run instead of failing |
| `--no-codegen` | Do not generate the agent's proto stubs first |
| `--json` | Print the report document(s) rather than the summary |
| `--sdk-version`, `--repository` | More report metadata |
| `-v` | Debug logging, including every request and response |

Exit code: 0 if conformant on every binding run, 1 if not, 2 if a run could not be set up (the SUT did not start, the card was unreadable, a test id does not exist).

### What a run looks like

```
INFO acts_runner: SUT up at http://127.0.0.1:40113 (grpc :40114)
INFO acts_runner: Webhook receiver listening on http://127.0.0.1:40115
INFO acts_runner: Re-running 4 capability-gated test(s) against a ITK_ACTS_REDUCED_CAPABILITIES SUT: CORE-CAP-001, CORE-CAP-002, PUSH-CFG-004, SEC-EXTCARD-003
INFO acts_runner: Re-running 5 authentication-gated test(s) against a ITK_ACTS_AUTH SUT: SEC-AUTH-001, ...
ACTS a2a-go / jsonrpc — 101 tests in 43850ms
  MUST   56/59 passed (3 failed)
  SHOULD 24/29 passed (5 failed)
  MAY    12/13 passed (1 failed)
  => NOT CONFORMANT
    CORE-SEND-004: expected an error, but the call succeeded
    STREAM-SUB-003: expected an error, but the stream opened and produced 1 event(s)
    VER-NEG-001: body.error.code is missing
    ...
```

(One real nightly run, kept as an illustration. Current numbers for every SDK are on the [dashboard](https://a2aproject.github.io/a2a-itk/dashboard/).)

Three SUTs are started in sequence per binding: the default one for the bulk of the corpus, then two more in **deviation modes** for the tests the default one has to skip (an agent cannot both advertise streaming and refuse it; nor both require a credential and serve the raw steps that must go unauthenticated). The log says which tests were re-run and why. See [02_architecture.md](02_architecture.md#deviations) for the mechanism.

The denominator differs per binding. Tests that declare a `transport:` are graded only on that binding and are left out of the others entirely, not counted as skipped. With the current corpus that is 101 tests on JSON-RPC, 88 on gRPC and 94 on REST, out of 113.

With `--transport all` a combined block follows:

```
========================================================
ACROSS 3 BINDING(S)
========================================================
  113 test(s) in all, each scored on the binding(s) it targets
  jsonrpc   92/101 passed   must 56/59   9 failed, 0 error(s)   NOT CONFORMANT
  grpc       83/88 passed   must 46/47   5 failed, 0 error(s)   NOT CONFORMANT
  rest       86/94 passed   must 47/49   8 failed, 0 error(s)   NOT CONFORMANT
  => NOT CONFORMANT overall
```

If any test was skipped for a precondition **no** agent card could satisfy - a corpus defect, not an SDK one - it is listed under `COULD NOT RUN ON ANY BINDING`. Those cost nothing in the verdict but are printed so the gap is visible.

### When something goes wrong

- `the code under test failed to start`: the agent did not come up within `ITK_READINESS_TIMEOUT` (35 s by default; raise it for Java or .NET). The CLI discards the agent's own output, so start the agent by hand from its `itk/` directory (`--httpPort 9000 --grpcPort 9001`) to see why. The service captures it under `/app/logs` when that directory is mounted.
- `cannot read the agent card at .../.well-known/agent-card.json`: the agent is up but does not serve its card at the root, or serves a redirect. ACTS does not follow redirects on the card, on purpose.
- `the agent card advertises no grpc interface`: the card's `supportedInterfaces` has no entry for the binding you asked for. The URL of every binding comes from the card, never from a convention.
- A whole group of tests skipped with `agent card capability X=False`: the card does not advertise `X`. Either the SDK lacks the feature (correct skip) or the agent forgot to advertise it.
- Many tests failing with `SUT does not declare behavior(s) tck-...`: the SDK's `acts/sut-behaviors.yaml` is missing prefixes. See [05_sdk-integration.md](05_sdk-integration.md).
- `Behaviour gating is off (no acts/sut-behaviors.yaml in the SUT checkout)`, or `(no behaviors file given for the external SUT)`: a warning. The run proceeds, tests needing behaviors run anyway and will mostly fail.

## Against an already-running agent

For an agent this repo cannot build or start, such as an SDK in a language the launcher does not support or a deployed service, start it yourself and pass its base URL instead of `--mount`:

```bash
uv run run_acts.py --sut-url http://127.0.0.1:9000 --sdk a2a-foo --language php \
  --behaviors ../a2a-foo/acts/sut-behaviors.yaml \
  --reduced-sut-url http://127.0.0.1:9100 --auth-sut-url http://127.0.0.1:9200 \
  --transport all
```

The agent has to meet the same contract as any SDK's ITK agent ([05_sdk-integration.md](05_sdk-integration.md)). What differs from `--mount`:

- **Nothing is built, generated or started.** Every binding runs against the same instance, so later bindings see the tasks earlier ones created. The corpus copes with that, because the list tests filter by their own context, but restart the agent between bindings if you want each report to start from a clean store.
- **The base URL is only where the card is read.** Each binding is dialed at the URL its `supportedInterfaces` entry gives, so those URLs must be reachable from where the runner runs.
- **The webhook receiver listens on `127.0.0.1`.** `PUSH-DELIV-*` and `SEC-PUSH-*` need the agent on the same host as the runner.
- **There is no checkout to read `acts/sut-behaviors.yaml` from.** Pass it with `--behaviors`, or use `--no-gate`. With neither, the run warns and runs ungated.
- **The harness cannot restart the agent in a deviation mode.** Start two more instances, one with `ITK_ACTS_REDUCED_CAPABILITIES=1` and one with `ITK_ACTS_AUTH=1`, and pass them as `--reduced-sut-url` and `--auth-sut-url`. Their cards are checked exactly as a spawned deviation SUT's would be, and their verdicts carry the same `configuration` stamp. Without them, the nine tests those modes exist for (`CORE-CAP-001`, `CORE-CAP-002`, `SEC-EXTCARD-003`, `PUSH-CFG-004`, `SEC-AUTH-001` to `004`, `SEC-AUTH-006`) stay skipped, and the run logs a warning naming them.
- **The report's `environment` block carries `"sut": "external"`**, so a reader can tell a run the harness started from one it did not.

## In the container

```bash
docker build -t itk_service .

docker run --rm \
  -v "$HOME/Source/a2a-python:/app/agents/repo" \
  -v "$HOME/.cache/a2a-itk-launcher:/root/.cache/a2a-itk" \
  itk_service uv run run_acts.py --mount /app/agents/repo/itk --sdk a2a-python --transport all
```

The SDK repo is mounted at `/app/agents/repo` so that the agent is at `/app/agents/repo/itk` and the contract file at `/app/agents/repo/acts/sut-behaviors.yaml`. Both paths matter.

## The HTTP service

Start the service as described in [interoperability/01_running.md](../interoperability/01_running.md#the-http-service), with the SDK repo mounted at `/app/agents/repo`.

### `POST /run-acts`

Runs the corpus over one binding against the mounted SUT and returns the report document **as-is** - no ITK envelope around it, because the section 13 report format is standardised so a dashboard can read a run from any ACTS runner.

Request:

```json
{
  "transport": "jsonrpc",
  "sdk": "a2a-python",
  "sdk_version": "1.0.3",
  "language": "python",
  "repository": "https://github.com/a2aproject/a2a-python",
  "tests": null,
  "variables": {},
  "capabilities": [],
  "gate_on_behaviors": true
}
```

| Field | Default | Meaning |
| --- | --- | --- |
| `transport` | required | `jsonrpc`, `grpc` or `rest` |
| `sdk` | required | Name for the report's `sdk` block |
| `sdk_version`, `language`, `repository` | `unknown` / `unknown` / omitted | More report metadata |
| `tests` | all | Restrict to these test ids |
| `variables` | `{}` | Override or add runner variables. The two the corpus needs (`insufficientAuthToken`, `otherUserTaskId`) are supplied by the runner already |
| `capabilities` | `[]` | Extra runner requirements to claim. The runner already declares the four it has |
| `gate_on_behaviors` | `true` | Fail tests whose `tck-*` prefixes the SUT does not declare |
| `sut_url` | omitted | Run against an agent already running at this URL instead of the mounted SDK, as `--sut-url` does. The URL must be reachable from inside the container, so run it with `--network host` for an agent on the host. There is no field for the contract file, so such a run is ungated; use the CLI's `--behaviors` when gating matters |
| `reduced_sut_url`, `auth_sut_url` | omitted | With `sut_url`: the instances that stand in for the two deviation SUTs |

Response: the report (see [02_architecture.md](02_architecture.md#the-report)). Errors: 400 when the SUT would not start, its card is unreadable, a test id does not exist, or deviation URLs are given without `sut_url`; 500 for anything else.

One request covers one binding. To test all three, send three requests. With a mounted SDK each starts a fresh SUT, so no run inherits another's task store; with `sut_url` all three use the same agent.

To run several bindings from a shell the way the CI driver does, see [04_ci.md](04_ci.md).

## Unit tests

```bash
uv run pytest tests/test_acts_*.py
```

These cover the schema, loader, dispatchers (with fake transports), assertions, streaming, variables, the runner, the report and the deviation logic without starting any agent. `tests/test_acts_corpus.py` additionally pins the shape of the mirrored corpus - test count, ids, levels - so drift from upstream shows up as a named failure.
