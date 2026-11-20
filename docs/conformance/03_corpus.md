# The ACTS corpus

The tests ACTS runs are not written in this repository. They are authored, reviewed and versioned in the A2A specification repository, and [`scenarios/acts/`](../../scenarios/acts/) is a byte-for-byte copy of one commit of them.

## Where the tests come from

| What | Where in [a2aproject/A2A](https://github.com/a2aproject/A2A) |
| --- | --- |
| The ACTS specification (format, runner rules, report) | `docs/acts-specification.md` |
| The corpus | `tests/acts/*.acts.yaml` |
| The PR that introduced both | [#1882](https://github.com/a2aproject/A2A/pull/1882) |
| The PR that aligned the corpus with the normative spec | [#2227](https://github.com/a2aproject/A2A/pull/2227) |
| The PR that added the tests for A2A 1.1 | [#2468](https://github.com/a2aproject/A2A/pull/2468) |

The copy here is pinned to one commit, named in [`scenarios/acts/PROVENANCE.md`](../../scenarios/acts/PROVENANCE.md) together with the branch and the PRs.

Changes go **upstream first**. A test that is wrong, missing or ambiguous is fixed in A2A and then copied here; the runner never rewrites a document on the way in, and a corpus that does not satisfy the schema is a defect to raise upstream, not to paper over. That keeps the mirror refreshable and keeps the runner a description of the format rather than of one snapshot.

## What is in it

Nineteen files: a manifest and eighteen suite files, 173 tests in all. One corpus serves A2A 1.0 and A2A 1.1; see [One corpus, two versions](#one-corpus-two-versions). The two test counts in the table are the tests that apply from A2A 1.0 and the tests that start at A2A 1.1.

| File | Test ids | 1.0 | From 1.1 | What it covers |
| --- | --- | --- | --- | --- |
| `suite.acts.yaml` | - | - | - | The manifest. Lists the files below in `include:` order |
| `discovery.acts.yaml` | `CARD-DISC`, `CARD-SCHEMA`, `CARD-CACHE`, `CARD-EXT`, `CARD-ROOT` | 10 | 2 | Agent card at the well-known path, its schema, caching headers, the extended card |
| `core-operations.acts.yaml` | `CORE-SEND`, `CORE-GET`, `CORE-CANCEL`, `CORE-LIST`, `CORE-FAIL` | 12 | - | Send, get, cancel, list; a failing task |
| `history.acts.yaml` | `CORE-HIST` | 6 | - | Task history length and ordering |
| `multi-turn.acts.yaml` | `CORE-MULTI`, `CORE-CTX` | 5 | - | `INPUT_REQUIRED` round trips, context ids |
| `streaming.acts.yaml` | `STREAM-SSE`, `STREAM-SUB`, `STREAM-RESUB`, `STREAM-MULTI`, `STREAM-MSG`, `STREAM-MSGUPD`, `STREAM-OPEN` | 11 | 8 | Event sequences, final events, subscription and resubscription, concurrent streams; from 1.1 resuming from a generation, the message update event, and streams that stay open while a task waits for input |
| `live-message.acts.yaml` | `STREAM-LIVE` | - | 10 | `SendLiveMessage`, the two-way exchange: opening it with a message or a subscription, later requests and their acknowledgment, rejected requests. Four of the ten are for gRPC only |
| `polling.acts.yaml` | `CORE-EXEC` | 2 | - | Polling a long-running task to completion |
| `generation.acts.yaml` | `CORE-GEN`, `STREAM-GEN`, `CORE-POLL`, `CORE-MATCH` | - | 14 | The task generation counter on tasks, artifacts and stream events; a `GetTask` held until the task changes; a send guarded by a generation |
| `timeline.acts.yaml` | `CORE-TIME` | - | 5 | The task timeline |
| `user-artifacts.acts.yaml` | `CORE-USERART` | - | 7 | Artifacts written by the client |
| `error-handling.acts.yaml` | `CORE-ERR`, `CORE-CAP`, `JSONRPC-ERR` | 14 | - | Unknown task, invalid params, terminal-state operations, unsupported capabilities |
| `auth-security.acts.yaml` | `SEC-AUTH`, `SEC-EXTCARD`, `SEC-PUSH` | 12 | - | Missing and insufficient credentials, extended card access, push config security |
| `version-negotiation.acts.yaml` | `VER-NEG` | 2 | 3 | `A2A-Version` handling |
| `wire-format.acts.yaml` | `DM-FMT` | 3 | - | Field naming and formats on the wire |
| `data-types.acts.yaml` | `DM-ART`, `DM-SERIAL`, `DM-EXTRA` | 7 | - | Text, data, file and file-URL artifacts; serialisation; unknown fields |
| `push-notifications.acts.yaml` | `PUSH-CFG`, `PUSH-LIST`, `PUSH-IDEM`, `PUSH-ERR`, `PUSH-DELIV`, `PUSH-VER` | 10 | 3 | Config CRUD, idempotency, errors, delivery to a webhook; from 1.1 the A2A version a configuration is delivered in |
| `transport-bindings.acts.yaml` | `JSONRPC-ENV/CT/SSE/GEN`, `REST-CT/PD/STATUS/SUB-GET/SUB-POST/GEN`, `GRPC-STATUS/STREAM/GEN` | 11 | 3 | Envelope rules, content types, problem details, status codes per binding; `SubscribeToTask` over REST by `GET` and by `POST`; from 1.1 how each binding reports a generation mismatch |
| `client-parsing.acts.yaml` | `CLIENT-PARSE`, `CLIENT-CAP`, `CLIENT-AUTH` | 8 | 5 | The SDK's own client parsing canonical payloads (section 10); from 1.1 the new fields, and streams with an event of a kind the client does not know |

Only `must` decides conformance.

### One corpus, two versions

A test may declare `min_spec_version` and `max_spec_version`. A run targets one A2A version and takes only the tests whose range contains it; a test outside the range is not part of the run and is not reported as skipped. The 113 tests without a lower bound are the tests for A2A 1.0. The 60 tests added for A2A 1.1 all declare `min_spec_version: "1.1"`, and one test, `REST-SUB-POST-001`, ends at 1.0, because `POST` is the subscribe verb over REST only in A2A 1.0.

| | Run at 1.0 | Run at 1.1 |
| --- | --- | --- |
| Tests | 113 | 172 |
| By level | 65 `must`, 35 `should`, 13 `may` | 105 `must`, 48 `should`, 19 `may` |
| On JSON-RPC, gRPC, REST | 101, 88, 94 | 155, 144, 147 |

A run at 1.0 takes the same test ids, with the same totals, as it did before the tests for 1.1 were added. That is what lets an SDK that implements only 1.0 keep its results unchanged.

Some tests declare a `transport:` and are graded only on that binding (28 of the tests for 1.0, 37 in all). The rest run on all three.

The tests for 1.1, and the constructs of the test format they use, come from [#2468](https://github.com/a2aproject/A2A/pull/2468), which is still under review. They can change with it, and several are marked `tentative` in the corpus, where the A2A text they test is incomplete or has a change pending.

### Test ids

Ids follow section 14: `<AREA>-<TOPIC>-<NNN>`. The area prefixes in use:

| Prefix | Area |
| --- | --- |
| `CORE` | Core operations and task lifecycle |
| `STREAM` | Streaming |
| `CARD` | Agent card discovery |
| `DM` | Data model and serialisation |
| `PUSH` | Push notifications |
| `SEC` | Authentication and authorisation |
| `VER` | Version negotiation |
| `JSONRPC`, `REST`, `GRPC` | One binding's rules |
| `CLIENT` | Client-side parsing |

Ids are stable across corpus revisions, which is what makes a nightly history a time series per test.

### What a test needs from the SUT

Three kinds of requirement appear on tests, and they are handled differently:

| Field | Example | If unmet |
| --- | --- | --- |
| `requires_behaviors` | `[tck-complete-task]` | **fail** - the SDK's contract does not declare it |
| `preconditions` | `capabilities: {streaming: true}`, `authentication: true` | skip - not applicable to this SUT (or re-run under a [deviation](02_architecture.md#deviations)) |
| `runner_requirements` | `[webhook_endpoint]` | skip - the runner cannot do it |

Of the tests for 1.0, seventy-two require a behaviour (one of them, `DM-FMT-002`, requires two). By prefix: `tck-complete-task` (35 tests), `tck-long-running` (14), `tck-multi-turn` (6), `tck-stream-basic` (5), `tck-message-response` (4), `tck-cancel` (2), and one each of `tck-task-failure`, `tck-stream-chunked`, `tck-auth-required`, `tck-artifact-text`, `tck-artifact-data`, `tck-artifact-file`, `tck-artifact-file-url`. The tests for 1.1 use these and six more: `tck-hold-working`, `tck-progress-update`, `tck-stream-input-required`, `tck-user-artifact-echo`, `tck-live-echo` and `tck-live-reject`. A run at 1.0 needs none of the six. Their meanings are in section 11.2 of the spec and in [05_sdk-integration.md](05_sdk-integration.md#2-behaviors).

### Variables

A document may define `variables`; the manifest defines `baseUrl`. Two more are referenced by tests but defined nowhere in the corpus, and section 12.2 leaves them to the runner: `insufficientAuthToken` and `otherUserTaskId`. Two others are runner-provided by nature: `webhookUrl`, and `specVersion`, the A2A version the run targets, which a raw step uses to write its own `A2A-Version` header. All four are supplied by `acts_runner` on every run, so no caller has to remember them.

## Refreshing the mirror

1. Make or merge the change in `A2A/tests/acts/` and note the commit.
2. Copy - do not merge. A three-way merge between two copies of the same file is how they drift.

   ```bash
   cp ../A2A/tests/acts/*.acts.yaml scenarios/acts/
   ```

3. Run the corpus pin test. It asserts the shape of what is mirrored - file list, test count, ids, levels - so anything that moved is a named failure rather than silent drift. Update the pins to the new truth.

   ```bash
   uv run pytest tests/test_acts_corpus.py
   ```

4. Verify against the commit, not against the working tree (which pins nothing while it has uncommitted changes):

   ```bash
   SHA=<new commit>
   for f in scenarios/acts/*.acts.yaml; do
     git -C ../A2A show "$SHA:tests/acts/$(basename "$f")" | cmp -s - "$f" || echo "DRIFT: $f"
   done
   ```

5. Update the SHA, branch and PR links in `PROVENANCE.md`.
6. Run the full unit suite. A corpus change can need a runner change - a new step field, a new error type, a new operator - and the schema is strict, so an unsupported construct fails at load time rather than being ignored.
7. Run the corpus against at least one SDK (`uv run run_acts.py --transport all`) and look at what changed in the report before merging. A run at 1.0 is what every SDK is scored on today: a change in its verdicts needs an explanation.

## What the runner adds on top

The corpus and spec deliberately leave some things to the runner. The decisions this runner has made, each explained in [02_architecture.md](02_architecture.md):

- the credential tokens (`itk-valid-token`, `itk-insufficient-token`) and the fixed `otherUserTaskId`;
- the two deviation modes and their environment variables;
- how a `client_response` step reaches the SDK's client (`tck-client-parse` over `send_message`);
- a 30-second default timeout on streaming steps that declare none;
- following the A2A spec's method and error tables where the ACTS spec's copies disagree with them;
- scoping transport-specific tests out of the other bindings' denominators;
- what happens to a background step that is still open when a later step fails;
- which kinds of push payload the webhook receiver stores.

These are conventions of this runner, not of ACTS. Another ACTS runner would be free to choose differently, and an SDK's test agent that wants to pass here has to follow this runner's conventions - which is what [05_sdk-integration.md](05_sdk-integration.md) spells out.
