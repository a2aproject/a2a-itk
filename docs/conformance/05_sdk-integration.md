# Making an SDK's agent ACTS-ready

ACTS tests the same agent the traversal suite starts: the one under `itk/` in the SDK repository, built and launched by the same launcher, serving the same card. One binary serves both suites. Everything in [interoperability/05_sdk-integration.md](../interoperability/05_sdk-integration.md) - the process contract, `--httpPort`/`--grpcPort`, the card at `/.well-known/agent-card.json`, `run_itk.sh`, proto stubs - is a prerequisite here and is not repeated.

What ACTS adds is a set of conventions the agent has to follow so that a declarative test can get a predictable answer out of it. Some come from the ACTS specification (section 11, the behaviour contract); the rest are this runner's own, listed in [03_corpus.md](03_corpus.md#what-the-runner-adds-on-top) and explained in [02_architecture.md](02_architecture.md). This page is the implementer's view: what to build, in what order, and how to check it.

Reference implementations: a2a-python (`itk/main.py`, `itk/acts_behaviors.py`, `itk/acts_client_parse.py`, `acts/sut-behaviors.yaml`) and a2a-dotnet (`itk/ActsAuth.cs`, `itk/ActsBehaviors.cs`, `itk/ActsClientParse.cs`). All six SDKs have done this work, so for a new agent the fastest route is to read the one in the nearest language.

## 1. The agent card

The runner fetches the card once per pass, with `A2A-Version: 1.0`, without following redirects, and derives everything else from it:

- **Binding URLs** come from `supportedInterfaces`. The entry with `protocolVersion: "1.0"` is preferred for each binding. There is no fallback convention: no JSON-RPC interface in the card means `the agent card advertises no jsonrpc interface` and no report for that binding.
- **Preconditions** read `capabilities` (`streaming`, `pushNotifications`, `extendedAgentCard`), `securitySchemes` plus `securityRequirements`, `skills` and `supportedInterfaces`. A capability the agent has but does not advertise makes every test needing it skip silently; a capability it advertises but lacks makes them fail. Be honest in both directions.
- `defaultInputModes` should include whatever the agent accepts. The ITK instruction envelope is a binary part, so the Python agent lists `application/x-protobuf` alongside `text/plain`.

## 2. Behaviors

A test cannot know how to make an arbitrary agent fail a task, stay in `WORKING`, or stream three artifact chunks. ACTS section 11 solves this with a prefix convention: the text of the **first part of the first user message** names a behaviour, and the agent produces the corresponding outcome. The message `tck-complete-task hello world` must come back as a completed task; `tck-cancel start` must stay in `WORKING` until a `cancel_task` arrives.

The standard prefixes (section 11.2) and what the runner's tests expect of each:

| Prefix | Expected outcome | Corpus uses it |
| --- | --- | --- |
| `tck-complete-task` | Task reaches `TASK_STATE_COMPLETED` with a text status message | 35 tests |
| `tck-long-running` | Task stays in `TASK_STATE_WORKING` for a moment, then completes. The Python agent waits 1 s; the corpus polls every 2 s, so it must be observable as non-terminal at least once | 14 |
| `tck-multi-turn` | `TASK_STATE_INPUT_REQUIRED` after each turn until the user sends a message starting with `done`, then `COMPLETED`. Continuation messages carry no prefix - recover the behaviour from the task's history | 6 |
| `tck-stream-basic` | On a streaming call: status `WORKING` -> one artifact -> status `COMPLETED` (final) | 5 |
| `tck-message-response` | A `Message` in the response, not a `Task` | 4 |
| `tck-cancel` | Stays in `WORKING`; `cancel_task` moves it to `TASK_STATE_CANCELED` | 2 |
| `tck-task-failure` | `TASK_STATE_FAILED` with an error message in the status | 1 |
| `tck-stream-chunked` | On a streaming call: one artifact delivered as several appended chunks, then `COMPLETED` | 1 |
| `tck-auth-required` | `TASK_STATE_AUTH_REQUIRED` with a status message describing what is needed | 1 |
| `tck-artifact-text` | Completes with a text artifact | 1 |
| `tck-artifact-data` | Completes with a structured data artifact | 1 |
| `tck-artifact-file` | Completes with a file artifact carrying inline bytes | 1 |
| `tck-artifact-file-url` | Completes with a file artifact carrying a URL | 1 |
| `tck-input-required` | `TASK_STATE_INPUT_REQUIRED` | 0 |
| `tck-reject-task` | `TASK_STATE_REJECTED` | 0 |

The last two are in the spec and in every SDK's contract, but no test in the current corpus uses them. Implement them anyway; a corpus refresh may.

Things that bite:

- **Match the longest prefix.** `tck-artifact-file-url` must not be taken for `tck-artifact-file`. The Python agent reads the name with a regex to the word boundary instead of checking prefixes in order.
- **Route on `tck-`, fall through otherwise.** A message that does not start with `tck-` is a traversal instruction and goes down the ITK path. One that starts with `tck-` but names a prefix the agent does not implement should fail the task loudly, not fall through - "no valid instruction" sends the reader to the wrong suite.
- **The behaviour belongs to the task, not the message.** Multi-turn continuations (`here is more input`, `done`) carry no prefix.
- **Streaming behaviours must work on `send_streaming_message` and on `subscribe`,** and the final event must be marked final. The runner reads events until it sees one, or until the step's timeout (30 s by default).
- **Non-streaming behaviours must also work over a stream.** The streaming tests mostly send `tck-long-running`, `tck-message-response` and `tck-complete-task`, and expect a stream of events that ends in the usual outcome.
- `returnImmediately: true` with `tck-long-running` is how the push and polling tests get a task that is still running when the next step arrives.

## 3. The contract file

Declare the prefixes the agent implements in **`acts/sut-behaviors.yaml` at the root of the SDK repository** (not under `itk/`; the runner looks one level above the mounted directory):

```yaml
acts_version: "1.0"

behaviors:
  - prefix: "tck-complete-task"
    description: "Complete the task with a text response message"
    response_type: task
    terminal_state: TASK_STATE_COMPLETED

  - prefix: "tck-message-response"
    description: "Return a direct Message, not a Task"
    response_type: message

  - prefix: "tck-long-running"
    description: "Stay in WORKING briefly, then complete"
    response_type: task
    terminal_state: TASK_STATE_COMPLETED
    delay_ms: 1000

  - prefix: "tck-stream-basic"
    description: "Stream working -> artifact -> completed"
    response_type: task
    terminal_state: TASK_STATE_COMPLETED
    streaming: true
    artifacts:
      - text: "streamed content"
```

Only `prefix` is required per entry; the rest is documentation for humans and is not checked. The top level allows exactly `acts_version` and `behaviors`, and `behaviors` must not be empty.

How the runner treats it:

| Situation | Effect |
| --- | --- |
| File absent | Gating is off. A warning is logged, every test runs, and tests needing an unimplemented prefix fail on their assertions instead. This is how a repo behaves before it adopts section 11 |
| File present, test needs a listed prefix | The test runs |
| File present, test needs an unlisted prefix | The test **fails** with `SUT does not declare behavior(s) ...`. Not a skip, on purpose: missing support must stay visible in the SDK's own report rather than shrink it |
| File invalid | The run fails to set up |

Keep the file as the single list of names. The Python agent deliberately does not restate the prefixes in code: the YAML is the claim, the code is the behaviour, and the tests check one against the other.

Add `acts/**` to the paths that trigger the ACTS pull-request workflow, as every SDK's `acts.yaml` does. Editing the contract changes what the suite demands.

## 4. Authentication

The tokens are a convention of this runner, described in [02_architecture.md](02_architecture.md#authentication). What the agent has to do with them depends on the pass:

**Default pass** (`ITK_ACTS_AUTH` unset). The runner sends `Authorization: Bearer itk-valid-token` on abstract operations and nothing on raw steps, and fifteen raw steps expect to succeed without a credential. So the agent must **not** require a credential on ordinary operations here, and its card should declare no `securitySchemes` - then the `SEC-AUTH-*` tests skip on their `authentication` precondition, honestly.

The one exception is `get_extended_agent_card`, which A2A section 13.3 puts behind authentication unconditionally. Guard it always:

| Presented | Answer |
| --- | --- |
| `Bearer itk-valid-token` | 200, the extended card |
| `Bearer itk-insufficient-token` | 403 - authenticated, not authorised |
| anything else, or nothing | 401, with a `WWW-Authenticate: Bearer ...` challenge |

That is what makes `SEC-EXTCARD-001/002/004` meaningful against a default agent. Error bodies should be a `google.rpc.Status` shape, as A2A section 11.6 asks.

**Auth pass** (`ITK_ACTS_AUTH=1` in the environment). The runner starts the agent a second time, checks that the card now has both `securitySchemes` and `securityRequirements`, and re-runs the tests that were skipped as `agent card authentication=False, needs True`. In this mode apply the same three-way table to **every** operation on every binding. The public agent card at `/.well-known/agent-card.json` must stay open in both modes - the readiness probe fetches it unauthenticated, and A2A section 7.3 has clients learn the required schemes from it.

If the card does not change under the variable, the skips stand and the run logs `With ITK_ACTS_AUTH set, the card declares no securitySchemes, ..., so ... stay skipped`; nothing fails, but the auth tests never produce a verdict for that SDK.

## 5. Reduced capabilities

`ITK_ACTS_REDUCED_CAPABILITIES=1` in the environment asks for the opposite of the default: a card that advertises `streaming: false`, `pushNotifications: false` and `extendedAgentCard: false`, and an agent that answers `UnsupportedOperationError` when those operations are called anyway. The runner starts a second agent with the variable set and re-runs the tests skipped as `agent card capability X=True, needs False` - four of them.

For most SDKs this is a few lines: the server already refuses operations the card does not advertise, so publishing less is enough. If the card still advertises the capabilities under the variable, the skips stand and the run logs it.

Both deviation variables are read by the agent process; the runner sets them in its own environment before spawning, which the launcher passes through unchanged.

## 6. Push notifications

Tests under `push-notifications.acts.yaml` register a config whose `url` is the runner's receiver - `{{webhookUrl}}`, resolved to `http://127.0.0.1:<port>/notifications` on the host the runner is on - and then watch the receiver. The agent has to:

- accept `create_push_config` / `get` / `list` / `delete` for a task, and reject them with `PushNotificationNotSupportedError` only if `pushNotifications` is not advertised;
- when the task changes state, `POST` the update to the configured URL;
- put `PushNotificationConfig.token` in the `X-A2A-Notification-Token` header, and the `authentication.credentials` in `Authorization: Bearer ...` when a scheme was given.

The delivery tests (`PUSH-DELIV-*`) are `may`-level and need the receiver, which `acts_runner` starts for every pass; a receiver that would not start makes them skip. An agent that advertises `pushNotifications: false` skips the whole file, which is the current state of a2a-dotnet.

## 7. Client tests

`client-parsing.acts.yaml` holds eight tests (five `must`) that do not exercise the server at all: they hand the SDK's **client** a canonical wire payload and ask what it parsed. The ACTS spec defines the step but not how a runner reaches a client, so this runner delivers it as a behaviour:

```json
{
  "message": {
    "role": "ROLE_USER",
    "parts": [
      {"text": "tck-client-parse"},
      {"data": {"operation": "send_message", "wire_payload": {"jsonrpc": "2.0", "id": "req-001", "result": {"task": {"...": "..."}}}}}
    ]
  }
}
```

`operation` is one of `send_message`, `get_task`, `get_agent_card`, `get_extended_agent_card`. The agent should:

1. Build an instance of its own SDK client whose HTTP transport returns `wire_payload` for any request, without opening a socket. Rewriting the JSON-RPC `id` in the canned payload to echo the request's is necessary: the corpus uses fixed ids, and a client that checks correlation would otherwise reject the payload before parsing it.
2. Perform the operation with that client.
3. Return a **completed task with one artifact holding a data part** whose content is what the client produced, shaped like the operation's response: `{"task": ...}` or `{"message": ...}` for `send_message`, the task fields for `get_task`, the card for the card operations, and `{"error": {"code": ..., "message": ...}}` when the client surfaced an error (`CLIENT-PARSE-004` feeds an error envelope and expects exactly that).

The runner takes the first data part on any artifact of the returned task and evaluates `expect_parsed` against it. No data part means `the SUT returned no parsed payload; it may not implement tck-client-parse` - a failure. These tests have no `requires_behaviors`, so the contract file does not gate them; an SDK without the behaviour fails all eight.

Use the real client with a mock transport, not a bare deserialiser. Calling `FromJson` directly skips the envelope handling and error mapping that the tests are about.

## 8. Checking the work

Iterate locally against the SDK checkout; every flag is described in [01_running.md](01_running.md):

```bash
# one test, with request/response logging
uv run run_acts.py --mount ../a2a-foo/itk --sdk a2a-foo -t CORE-SEND-001 -v

# see what fails once the contract is in place
uv run run_acts.py --mount ../a2a-foo/itk --sdk a2a-foo --transport all

# before the contract file exists, or to see assertion failures instead of
# "does not declare behavior"
uv run run_acts.py --mount ../a2a-foo/itk --sdk a2a-foo --no-gate
```

A rough order that keeps each step's failures readable:

1. Card and bindings right: `-t CARD-DISC-001` and one `CORE-SEND-*` per binding.
2. `tck-complete-task`, then the rest of the prefixes; add each to the contract as it lands.
3. Streaming prefixes over `send_streaming_message` and `subscribe`.
4. Extended-card guarding, then `ITK_ACTS_AUTH`.
5. `ITK_ACTS_REDUCED_CAPABILITIES`.
6. `tck-client-parse`.
7. Push delivery.

Then wire the two workflows as in [04_ci.md](04_ci.md). Leave `continue-on-error: true` on the PR job until the SDK is conformant on every binding it runs.
