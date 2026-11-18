# ACTS architecture

How a YAML test becomes wire calls and a verdict. The runner lives in [`test_suite/acts/`](../../test_suite/acts/); the pipeline that starts the SUT around it is [`acts_runner.py`](../../acts_runner.py). As with ITK, two front ends ([`run_acts.py`](../.uv run run_acts.py) and `POST /run-acts` in [`itk_service_v2.py`](../../itk_service_v2.py)) share that one pipeline.

```mermaid
flowchart LR
    C[Corpus<br/>scenarios/acts/*.acts.yaml] --> L[Load + scope to binding<br/>loader.py]
    L --> S[Start SUT, read its card<br/>launcher]
    S --> R[Run each test<br/>runner.py]
    R --> D[Deviation passes<br/>reduced caps, auth]
    D --> P[section 13 report<br/>report.py]
    W[wire_map.py] --> X[dispatcher/]
    X --> R
    B[acts/sut-behaviors.yaml<br/>in the SDK repo] --> R
```

ACTS is kept apart from the traversal suite on purpose. They share the launcher and the agents, nothing else: a traversal starts N agents and walks a circuit between them, a conformance run starts one and interrogates it.

## The pipeline

`acts_runner.run(transport, spec_version, ...)` does the following for one binding at one A2A version. `spec_version` is the run version; it defaults to `1.0`.

1. **Load the corpus.** `loader.load_suite` reads the manifest (`suite.acts.yaml`), follows its `include:` list, validates every file against the schema exactly as written - nothing is rewritten on the way in - and flattens the result into one ordered list of tests, each tagged with the suite it came from. Duplicate test ids across files are an error here, because they would overwrite each other in the report.
2. **Select the tests for the run version, then scope to the binding.** A test whose `min_spec_version` / `max_spec_version` range does not contain the run version is dropped, not skipped, so a run at an older version keeps its tests and totals when tests for a newer version are added. A file whose own `spec_version` is older than the run version stops the run: its unbounded tests were never checked against the newer version. Tests that declare `transport:` for a different binding are dropped, not skipped. A report covers one binding, and a test that cannot apply to it should not appear in the denominator - gRPC is scored out of 88, not out of 113 with 25 phantom skips. `-t` subsets are applied just before this.
3. **Read the behavior contract.** `acts/sut-behaviors.yaml` from the SDK repository root (the parent of the mounted `itk/`), or the file `--behaviors` names. Present: the set of `tck-*` prefixes the SDK claims. Absent: gating is off, with a warning.
4. **Start the SUT** through the same `Cluster` the traversal suite uses, as a single `MOUNT` target. The agent is started with `--httpPort` and `--grpcPort` and is up when its card answers. With `--sut-url` (or `sut_url` on the service) this step is skipped and the given URL is used as the agent's root.
5. **Read the card** at `/.well-known/agent-card.json`, with `A2A-Version` set to the run version. The header matters: the spec makes an absent header mean 0.3, and an SDK with a compatibility layer would answer with its 0.3 card, on which capabilities are spelled differently. Redirects are not followed; the card decides what the whole run tests, so an agent that serves it somewhere else should fail loudly.
6. **Build the dispatcher** for the binding. The URL comes from the card's `supportedInterfaces`: the entry for the binding whose `protocolVersion` is the run version, otherwise the newest entry of the same major version that is newer than the run version, otherwise an entry with no version. An entry for an older version is never used (the ITK agents also publish a 0.3 interface at the same URL for traversal compatibility); with nothing usable the run stops with a configuration error. A JSON-RPC agent may mount at `/jsonrpc/`, a REST one at `/rest/`; the card is the only reliable source. The dispatcher adds `Authorization: Bearer itk-valid-token` to every abstract operation (see [Authentication](#authentication) below).
7. **Start the webhook receiver**: `notifications_app.py` on a free port, the same mock the traversal suite uses for push notifications. If it comes up, the runner is granted the `webhook_endpoint` requirement and the `webhookUrl` variable; if not, tests needing it skip rather than losing the run.
8. **Run every test in order** (`Runner.run_suite`). Sequential, because the SUT's task list is global to it and `list_tasks` assertions only mean something if nothing else is creating tasks at the same time.
9. **Deviation passes.** Some tests were skipped because the default SUT advertised a capability the test needs absent, or declared no authentication when the test needs some. For each such group a second SUT is started with an environment variable asking it to behave differently, those tests alone are re-run, and their verdicts are spliced in.
10. **Build the report** (`report.build`), a section 13 document.

Each binding is a separate call; `run_acts.py` loops over the bindings you asked for and the CI driver sends one request per binding. With a mounted SDK each call starts its own SUT. An external SUT is the same instance for every call.

## From an abstract step to a wire call

A step says `operation: get_task` with `params: {id: ...}`. Nothing in the corpus names an HTTP method, a path, a gRPC RPC or a JSON-RPC method, which is what lets one corpus run on three bindings.

[`wire_map.py`](../../test_suite/acts/wire_map.py) is the one table that maps abstract names to wire facts, in both directions:

| Abstract | JSON-RPC | REST | gRPC |
| --- | --- | --- | --- |
| `send_message` | method `SendMessage` | `POST /message:send` | `SendMessage` |
| `get_task` | method `GetTask` | `GET /tasks/{id}` | `GetTask` |
| `TaskNotFoundError` | code `-32001` | status 404, `ErrorInfo.reason` | `NOT_FOUND` + reason |

The tables follow the A2A specification's own method and error mapping (sections 5.3 and 5.4) rather than the copies in the ACTS spec, because the ACTS spec says the A2A spec wins where they disagree and the copies have drifted. Divergences are noted inline in the file.

[`dispatcher/`](../../test_suite/acts/dispatcher/) has one adapter per binding. All three return the same `WireResponse`: status, headers, payload and, on failure, a `WireError` that has already been normalised - error type, message, the JSON-RPC code or gRPC status that carried it, and the details list whichever key the binding used. The runner never looks at the transport.

The transports are driven at a low level - `httpx` for the two HTTP bindings, generated stubs over `grpc.aio` for gRPC - not through an A2A client library. A conformance suite asserts on exactly the things a client is built to hide: the status code, the headers, whether the body was valid JSON at all. Several tests also send payloads a well-behaved client would refuse to construct.

**Raw steps** (`raw: {method, path, body, headers}`) bypass the mapping and send an HTTP request as written. They exist for the binding-specific tests (`JSONRPC-*`, `REST-*`), are refused on gRPC, and carry **no** default headers - which is what makes an unauthenticated probe actually unauthenticated.

## Deciding what happened

[`runner.py`](../../test_suite/acts/runner.py) sequences one test: gate it, run its steps, evaluate assertions. Four outcomes, and the distinctions between them carry most of the design.

| Outcome | Means | Counts against conformance |
| --- | --- | --- |
| `pass` | Every step and assertion held | - |
| `fail` | The SUT answered, and its answer did not conform | yes, at `must` level |
| `skip` | The test does not apply to this SUT or this runner | no |
| `error` | The run could not complete the step: connection refused, a variable nobody defined, a raw step on gRPC | yes - but it is a statement about the harness, and is counted separately so a broken runner is not read as a non-conformant SDK |

A SUT answering in a shape the binding does not permit - for example a streaming response with the wrong `Content-Type`, so it cannot be read as SSE - is a `fail`, not an `error`: that is a finding about the SUT.

### Gating: why a test is skipped

Checked in this order, before any step runs:

1. **Binding.** A backstop - the pipeline already scoped the corpus.
2. **Step kinds.** The one kind the runner does not execute is a bare `assertion` step; the corpus contains none.
3. **Runner requirements.** A test may declare it needs `header_inspection`, `auth_credentials`, `concurrent_streams`, `stream_disconnect`, `concurrent_requests` or `webhook_endpoint` (section 12.1). This runner has the first five by construction and the last whenever the receiver started.
4. **Preconditions** (section 12.5), evaluated against the agent card:
   - `capabilities: {streaming: true}` - the card must advertise it (a card that omits a capability has not advertised it). A capability name that A2A does not define at all is reported as `PRECONDITION CANNOT BE SATISFIED`, a distinct skip that `run_acts.py` lists separately, because no SUT could ever clear it and it would otherwise look like "not applicable here".
   - `authentication: true` - the card must declare both `securitySchemes` and a non-empty `securityRequirements`. Schemes alone are an offer, not a requirement.
   - `skills`, `transport`, `extensions` - presence checks.
   - `protocol_versions: ["1.0"]` - the card must list, for the binding under test, an interface for each of these A2A versions. It gates a test that sends a step as a client of a version other than the one the run targets.

### Behaviors: a failure, not a skip

After gating, a test's `requires_behaviors` is compared with the SDK's contract. A prefix the SUT does not declare **fails** the test. A skip would let an SDK's missing support disappear quietly from its own conformance report, which is the opposite of what the report is for. `--no-gate` turns this off; then the test runs and almost certainly fails on its assertions instead.

### Steps

Each test gets a fresh variable `Scope` (section 3.2, isolation), seeded with the document's `variables` and the runner's. Steps run in order; the first one that does not pass ends the test, because later steps read this one's captures and would only produce a cascade describing the same cause.

| Step kind | What the runner does |
| --- | --- |
| operation, unary | Substitute `{{...}}` in `params`, dispatch, record the response, run `capture` and evaluate `expect` (status, headers, body) or `expect_error`. A failed assertion is reported in preference to a `capture` that found no value, because a value is usually captured on the step that also asserts it. When the step's own assertions use the value the capture missed, the miss is what is reported |
| operation, with `repeat` | Re-dispatch until the `until` expression holds or attempts run out (defaults: 10 attempts, 1000 ms, no backoff). Running out is a fail |
| operation, streaming | Read events until the stream closes, `max_count` is reached, or the timeout. Evaluate `expect_stream`: event count, matchers on specific events, the final event, and the `ordering` rules (see [Stream events](#stream-events)). `streams: N` opens N concurrent streams; `disconnect_after` hangs up mid-stream. `capture` reads the first event, with the kind of event as the first path segment (`task.id`), and `expect_stream` may compare events with captured values, this step's included |
| raw | Send the HTTP request as written, no default headers; evaluate as above |
| `client_response` | section 10: ask the SUT's *own client* to parse a canned payload. No A2A operation does this, so it rides a `send_message` whose text is `tck-client-parse` and whose data part carries `{operation, wire_payload}`; the agent answers with what its client parsed and `expect_parsed` is evaluated like a body. For an operation that is answered with a stream the data part carries `wire_stream`, the events in order, in place of `wire_payload`; the agent answers `{events: [...]}`, the events its client surfaced, and each of them is addressed as a [stream event](#stream-events) is |
| `expect_webhook` | Poll the receiver for notifications delivered to `webhookUrl` for a task until `min_count` have arrived and every entry of `events` is decided, or `timeout_ms` passes; evaluate `events`, `each_event`, `final_event`, `headers` and `token` against the deliveries read up to then |

Every abstract operation carries `A2A-Version` set to the run version (section 12.4); on gRPC it is call metadata. A raw step is sent as written and reads the run version from `{{specVersion}}`.

`a2a_version` on an operation step sends that one step as a client of another version: the header carries that version, and where the card lists an interface for it on the binding under test, the step goes to that interface. A version the agent does not list goes to the interface the run uses, which is expected to refuse it.

`min_duration_ms` and `max_duration_ms` on an operation step bound the time from sending the request to the complete response. They are how a test observes that the SUT held a request open (the A2A 1.1 long poll) or answered at once; `delay_ms` before the step is not counted. A request that is still unanswered at `max_duration_ms` is abandoned and the step fails. They cannot be combined with `repeat` or `expect_stream`.

`events` in `expect_webhook` names individual deliveries, in the forms `expect_stream.events` uses, and makes the step read on until they have arrived. An entry with `match: any_position` is waited for until a delivery matches it; an entry at a position is waited for until that many deliveries have arrived, and is then decided. An entry that is decided and does not hold fails the step at once, without waiting for the others. A webhook step without `events` stops at `min_count`, which is usually the first delivery, so a test of what is delivered later, or of a kind that must never be delivered, names in `events` a delivery that comes after it, such as the one that reports the task completed.

The receiver stores `task`, `statusUpdate` and `messageUpdate` payloads. It still refuses `artifactUpdate` and `message` payloads, so a webhook step never sees a delivery of those two kinds.

`send_live_message`, the two-way exchange A2A 1.1 adds, is in the wire map so that a client step can name it. An operation step cannot name it, because the runner does not drive a live exchange yet.

`stays_open_ms` in `expect_stream` is for a stream the SUT must not close. After each event the runner waits that long for the next one; if none comes and the stream has not ended, the runner hangs up and evaluates the other assertions on the events it has. A stream the SUT closes fails the step. Where the step and one of its streams both set it, the longer wait applies. It cannot be combined with `disconnect_after`, and `timeout_ms` has to be longer than it.

### Background steps

Steps run one after another, with one exception. `background: true` on an operation step sends the request, leaves it open and goes on to the next step. It is how a test sends a message to a task while a subscription to that task is being read, or while a read of it is being held. A test that uses it declares `runner_requirements: [concurrent_requests]`.

- **When the next step starts.** For a streaming operation, when the first event of the stream has arrived, which shows the SUT has the stream in place. For any other operation, when the request has been written out; the response is not waited for. A test that needs the SUT to be working on such a request puts `delay_ms` on the next step.
- **When it is evaluated.** After the last step of the test, once its response has arrived or the read of its stream has ended. `expect`, `expect_error`, `expect_stream` and the duration bounds are evaluated then, so they may name what a later step captured. The duration bounds are held against the step's own exchange, from its request to its response, and a request still unanswered at `max_duration_ms` is abandoned then. The `duration_ms` the report gives for the step runs until it was evaluated.
- **Capture.** On a background stream `capture` reads the first event, so the steps that follow can use the value. On any other background step it reads the response, which arrives later: the value is available to the test's `assertions` and not to the steps that follow.
- **Reporting.** A failure is reported against the background step, and steps are listed in the order the test writes them. Where several steps did not pass, the first of them in that order is the test's verdict.
- **A test that stops early.** When a step does not pass, the test ends, and a background request that is still open is closed without being evaluated: it was waiting for steps that never ran. A background step is still reported if it went wrong before the next step started, because it is then the likely cause of the later failure: its request could not be sent, its stream was refused or ended without an event, or its first event did not give its `capture`.
- **Not combined with `repeat`.** A repeated step sends its request again once it is answered, and a background step is answered after the steps that follow it.

A streaming step with no `timeout_ms` of its own gets 30 seconds. Not one streaming step in the corpus declares a timeout, and without a backstop a SUT that never closes a stream would hang the run and produce no report at all.

### Stream events

A stream event is one of six kinds: `task`, `message`, `status_update`, `artifact_update`, and from A2A 1.1 `message_update` and `error`. An assertion addresses an event by the name of its kind (`status_update: {status: ...}`) or straight through to the fields of its payload (`status: ...`). The wire writes the kind in camelCase (`statusUpdate`) and a test usually in snake_case; both spellings name the same kind, so `message_update: {absent: true}` fails on an event that arrived as `messageUpdate`. A payload delivered to the webhook is addressed the same way.

At the top of an event the name of a kind always means the kind. A message update and a stream error both have a field called `message`; `message: ...` does not match them, and that field is reached through its own kind: `message_update: {message: ...}`.

An event of a kind the runner does not know is kept as it arrived and counts as an event of the stream.

`ordering` takes one rule or a list:

| Rule | The stream fails when |
| --- | --- |
| `monotonic_state` | a state returns to `SUBMITTED`, or anything follows a terminal state |
| `monotonic_generation` | a status, artifact or message update has no `generation`, one below 1, or one that is not greater than every generation before it; or a task snapshot has one below 1 or lower than one before it. A snapshot may repeat the latest value |
| `contiguous_generation` | any of the above, or an update is not exactly one above the latest generation before it. Only for a stream that is sent every event |

`message` and `error` events carry no generation and are skipped by the two generation rules.

### Assertions

[`assertions.py`](../../test_suite/acts/assertions.py) implements section 5. An `expect.body` is a tree that mirrors the response: a scalar leaf is an exact match, a map is either a descent into a field or a set of operators on the value - and the syntax does not say which. `{status: {state: X}}` is two field descents; `{type: array, count_gte: 1}` is two operators. The rule the evaluator applies: **a key is an operator when it is a known operator name and its argument is well-formed for that operator; otherwise it is a field name.** Both halves are needed - the corpus asserts on RFC 9457 bodies whose members really are called `type` and `title`.

Operators: `type`, `exists`, `absent`, `contains`, `matches`, `starts_with`, `ends_with`, `gt`/`gte`/`lt`/`lte`, `count`/`count_gte`/`count_lte`, `one_of`, and `items` for per-element assertions on an array. Named assertions on a collection use `any`/`all`/`none` with a path and a matcher.

A 64-bit integer such as `generation` reaches an assertion as a string of digits (`"5"`) on every binding, because that is how ProtoJSON writes it, while a test writes the number `5`. `gt`/`gte`/`lt`/`lte` read a string of digits as the integer it spells, for the value and for the operand, so a value captured from one response can be compared with another. An exact match does not convert: `generation: 5` does not match `"5"`; write `{gte: 5, lte: 5}`.

Failures record the assertion path (`task.status.state`), the expected and actual values, and the step, so a report entry can be acted on without re-running.

## Deviations

Four tests assert that an agent **lacking** a capability answers `UnsupportedOperationError`; five assert that an agent **requiring** a credential rejects a request without one. Their preconditions are the opposite of what the rest of the corpus needs, and no single agent can be both. Rather than leave those branches of the protocol untested by anyone, the pipeline starts a second agent that meets them.

| Deviation | Env var set in the SUT's environment | Re-runs tests skipped as | Checks on the second card |
| --- | --- | --- | --- |
| reduced capabilities | `ITK_ACTS_REDUCED_CAPABILITIES=1` | `agent card capability X=True, needs False` | `streaming`, `pushNotifications`, `extendedAgentCard` no longer advertised |
| authentication | `ITK_ACTS_AUTH=1` | `agent card authentication=False, needs True` | `securitySchemes` and `securityRequirements` present |

The selection is keyed on the **skip reason**, not on a list of test ids: the corpus is authored upstream, and a hard-coded list would silently stop matching when a test is renamed, whereas a derived selector leaves an uncleared skip visible in the report.

If the SUT does not honour the variable - the card still advertises streaming, or still declares no security scheme - the original skips stand and a warning says why. A SUT that fails to start in a deviated mode is also a warning, not the loss of the binding's report. Verdicts obtained from a deviated SUT are stamped with `configuration: <env var>` in the report (section 12.8), because a result from a differently configured instance is not interchangeable with one from the agent the rest of the run tested.

The two run serially: the variable is set in the pipeline's own environment, which the launcher passes to the child, so two SUTs alive at once could not be told apart.

An external SUT cannot be restarted, so the caller starts the deviated instances itself, with the variable set, and passes their URLs (`--reduced-sut-url` / `--auth-sut-url`, or `reduced_sut_url` / `auth_sut_url` on the service). Nothing is spawned and the pipeline's environment is left alone; the card check and the `configuration` stamp are the same as for a spawned SUT. A deviation with no URL leaves its tests skipped, with a warning.

## Authentication

The ACTS spec says a runner needs `auth_credentials` for some tests but never says what the credential is. The pairing is this repository's convention:

| Token | Presented by | Expected answer |
| --- | --- | --- |
| `itk-valid-token` | Every abstract operation, as `Authorization: Bearer` | Authenticated and authorised. Needed for `get_extended_agent_card`, which the spec puts behind authentication |
| `itk-insufficient-token` | Raw steps that reference `{{insufficientAuthToken}}` | Authenticated but not authorised: 403, not 401 |
| nothing | Raw steps that set no header | 401 where a credential is required |

Raw steps never carry the default header; that is why the abstract and raw step kinds must not share one header set, and the whole reason the `SEC-EXTCARD-*` probes mean anything.

The runner also supplies `otherUserTaskId` (a fixed UUID the SUT has never issued) for the tests that ask for a task belonging to someone else.

## The report

`report.build` produces the section 13.1 document, which is frozen the way ITK's `/run` response is - dashboards read it, and fields are only added:

```json
{
  "acts_version": "1.0",
  "spec_version": "1.0",
  "generated_at": "2026-10-06T02:14:09Z",
  "sdk": {"name": "a2a-python", "version": "...", "language": "python", "repository": "..."},
  "transport": "jsonrpc",
  "environment": {"runner": "a2a-itk", "python": "3.12....", "platform": "...", "interface_protocol_version": "1.0"},
  "summary": {
    "total": 101, "passed": 96, "failed": 2, "skipped": 3, "errors": 0, "duration_ms": 48213,
    "by_level": {
      "must":   {"total": 59, "passed": 56, "failed": 1, "skipped": 2, "errors": 0},
      "should": {"...": "..."},
      "may":    {"...": "..."}
    }
  },
  "suites": [
    {"id": "core-operations", "name": "Core A2A operations", "tests": [
      {"id": "CORE-SEND-001", "name": "...", "level": "must", "result": "pass", "duration_ms": 412,
       "steps": [{"id": "send", "result": "pass", "duration_ms": 410}]},
      {"id": "CORE-HIST-003", "level": "must", "result": "fail", "duration_ms": 1203,
       "failure": {"message": "...", "step_id": "get", "expected": "4", "actual": "3",
                   "assertion_path": "history.length"}},
      {"id": "CORE-CAP-001", "level": "must", "result": "pass",
       "configuration": "ITK_ACTS_REDUCED_CAPABILITIES", "...": "..."}
    ]}
  ]
}
```

`is_conformant(report)` is section 12.7: `summary.by_level.must.failed == 0` and `summary.by_level.must.errors == 0`. Skips do not count either way.

The file name follows section 13.5: `acts-report-<sdk>-<transport>-<timestamp>.json`. `run_acts.py --out` and the CI driver both write it.
