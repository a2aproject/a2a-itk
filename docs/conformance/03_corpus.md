# The ACTS corpus

The tests ACTS runs are not written in this repository. They are authored, reviewed and versioned in the A2A specification repository, and [`scenarios/acts/`](../../scenarios/acts/) is a byte-for-byte copy of one commit of them.

## Where the tests come from

| What | Where in [a2aproject/A2A](https://github.com/a2aproject/A2A) |
| --- | --- |
| The ACTS specification (format, runner rules, report) | `docs/acts-specification.md` |
| The corpus | `tests/acts/*.acts.yaml` |
| The PR that introduced both | [#1882](https://github.com/a2aproject/A2A/pull/1882) |
| The PR that aligned the corpus with the normative spec | [#2227](https://github.com/a2aproject/A2A/pull/2227) |

The copy here is pinned to one commit, named in [`scenarios/acts/PROVENANCE.md`](../../scenarios/acts/PROVENANCE.md) together with the branch and the PRs. At the time of writing that is `82c277f342a2fd7ea47548e2de94f97ea578a7ed` on the branch `conformance-spec-adjustments` - a branch, not a release, because the corpus has not been merged to A2A's `main` yet.

Changes go **upstream first**. A test that is wrong, missing or ambiguous is fixed in A2A and then copied here; the runner never rewrites a document on the way in, and a corpus that does not satisfy the schema is a defect to raise upstream, not to paper over. That keeps the mirror refreshable and keeps the runner a description of the format rather than of one snapshot.

## What is in it

Fifteen files: a manifest and fourteen suite files, 111 tests in all.

| File | Test ids | Tests | What it covers |
| --- | --- | --- | --- |
| `suite.acts.yaml` | - | - | The manifest. Lists the files below in `include:` order |
| `discovery.acts.yaml` | `CARD-DISC`, `CARD-SCHEMA`, `CARD-CACHE`, `CARD-EXT` | 10 | Agent card at the well-known path, its schema, caching headers, the extended card |
| `core-operations.acts.yaml` | `CORE-SEND`, `CORE-GET`, `CORE-CANCEL`, `CORE-LIST`, `CORE-FAIL` | 12 | Send, get, cancel, list; a failing task |
| `history.acts.yaml` | `CORE-HIST` | 6 | Task history length and ordering |
| `multi-turn.acts.yaml` | `CORE-MULTI`, `CORE-CTX` | 5 | `INPUT_REQUIRED` round trips, context ids |
| `streaming.acts.yaml` | `STREAM-SSE`, `STREAM-SUB`, `STREAM-RESUB`, `STREAM-MULTI`, `STREAM-MSG` | 11 | Event sequences, final events, subscription and resubscription, concurrent streams |
| `polling.acts.yaml` | `CORE-EXEC` | 2 | Polling a long-running task to completion |
| `error-handling.acts.yaml` | `CORE-ERR`, `CORE-CAP`, `JSONRPC-ERR` | 14 | Unknown task, invalid params, terminal-state operations, unsupported capabilities |
| `auth-security.acts.yaml` | `SEC-AUTH`, `SEC-EXTCARD`, `SEC-PUSH` | 12 | Missing and insufficient credentials, extended card access, push config security |
| `version-negotiation.acts.yaml` | `VER-NEG` | 2 | `A2A-Version` handling |
| `wire-format.acts.yaml` | `DM-FMT` | 3 | Field naming and formats on the wire |
| `data-types.acts.yaml` | `DM-ART`, `DM-SERIAL`, `DM-EXTRA` | 7 | Text, data, file and file-URL artifacts; serialisation; unknown fields |
| `push-notifications.acts.yaml` | `PUSH-CFG`, `PUSH-LIST`, `PUSH-IDEM`, `PUSH-ERR`, `PUSH-DELIV` | 10 | Config CRUD, idempotency, errors, delivery to a webhook |
| `transport-bindings.acts.yaml` | `JSONRPC-ENV/CT/SSE`, `REST-CT/PD/STATUS`, `GRPC-STATUS/STREAM` | 9 | Envelope rules, content types, problem details, status codes per binding |
| `client-parsing.acts.yaml` | `CLIENT-PARSE`, `CLIENT-CAP`, `CLIENT-AUTH` | 8 | The SDK's own client parsing canonical payloads (section 10) |

By level: 65 `must`, 33 `should`, 13 `may`. Only `must` decides conformance.

Twenty-six tests declare a `transport:` and are graded only on that binding. The rest run on all three. That gives 101 tests on JSON-RPC, 88 on gRPC and 92 on REST.

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

The behaviour prefixes the corpus uses, by frequency: `tck-complete-task` (72 tests), `tck-long-running` (24), `tck-multi-turn` (19), `tck-stream-basic` (10), `tck-message-response` (8), `tck-cancel` (4), and two each of `tck-task-failure`, `tck-stream-chunked`, `tck-auth-required`, `tck-artifact-text`, `tck-artifact-data`, `tck-artifact-file`, `tck-artifact-file-url`. Their meanings are in section 11.2 of the spec and in [05_sdk-integration.md](05_sdk-integration.md#2-behaviors).

### Variables

A document may define `variables`; the manifest defines `baseUrl`. Two more are referenced by tests but defined nowhere in the corpus, and section 12.2 leaves them to the runner: `insufficientAuthToken` and `otherUserTaskId`. A third, `webhookUrl`, is also runner-provided by nature. All three are supplied by `acts_runner` on every run, so no caller has to remember them.

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
7. Run the corpus against at least one SDK (`uv run run_acts.py --transport all`) and look at what changed in the report before merging.

## What the runner adds on top

The corpus and spec deliberately leave some things to the runner. The decisions this runner has made, each explained in [02_architecture.md](02_architecture.md):

- the credential tokens (`itk-valid-token`, `itk-insufficient-token`) and the fixed `otherUserTaskId`;
- the two deviation modes and their environment variables;
- how a `client_response` step reaches the SDK's client (`tck-client-parse` over `send_message`);
- a 30-second default timeout on streaming steps that declare none;
- following the A2A spec's method and error tables where the ACTS spec's copies disagree with them;
- scoping transport-specific tests out of the other bindings' denominators.

These are conventions of this runner, not of ACTS. Another ACTS runner would be free to choose differently, and an SDK's test agent that wants to pass here has to follow this runner's conventions - which is what [05_sdk-integration.md](05_sdk-integration.md) spells out.
