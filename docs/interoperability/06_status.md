# ITK: current state

A snapshot of what the interoperability suite covers, what it deliberately leaves out, and what is still open.

## SDKs in the matrix

| SDK | Matrix key | `v10` (A2A 1.0) | `v03` (A2A 0.3) | Transports as peer | Runs ITK in its own CI |
| --- | --- | --- | --- | --- | --- |
| Python | `python` | `main` | `v0.3.24+itk` | all | PR + nightly |
| Go | `go` | `main` | `v0.3.15+itk` | all (`v03`: no `http_json`) | PR + nightly |
| TypeScript | `ts` | `main` | `v0.3.14+itk` | all | PR + nightly |
| Java | `java` | `main` | - | all | PR + nightly |
| Rust | `rust` | `main` | - | all | PR + nightly |
| .NET | `dotnet` | `main` | - | all | PR + nightly |

All six repositories use the shared driver (`scripts/run_itk_shared.sh`) and the shared scenario sets (`ITK_SCENARIO_SET=shared`). None carries a `scenarios.json` of its own any more, though the legacy format is still accepted.

Java, Rust and .NET have no `v03` line.

## What a run covers

Per SDK, per night, the nightly set walks the SUT against each of the nine peer lines in the matrix (`peers: all` includes the SUT's own SDK at `main`, so every SDK is also tested against its own released head) over every transport the pair can speak, with:

- `send_message`, non-streaming and streaming
- `push_notification`
- `resubscribe` (streaming)

That is up to 9 peers x 3 transports x 4 variants = 108 scenarios before exclusions, each a pass/fail of its own. The PR set is a four-peer star with the same variants, plus the SUT's own lines.

## Known failures

Pairs that are known not to work are excluded in [`known_failures.yaml`](../../known_failures.yaml). Each entry says which SUT, which peer(s), which transports or behaviours, and why, and is logged on every run it affects. The list is not repeated here, because it changes as SDKs fix things; the file is short and the reason sits next to each entry. What actually passed last night is on the [dashboard](https://a2aproject.github.io/a2a-itk/dashboard/).

The entries fall into three kinds:

- **The 0.3 compatibility layer lives on one side of the hop.** About half the entries. Whether a `v10 <-> v03` pair works depends on which SDK drives the hop and over which transport, so the limit is a property of the (SUT, peer) pair rather than of either agent alone.
- **Features an SDK does not implement.** A missing transport or behaviour. As a peer such a limit is a `transports` ceiling in `matrix.yaml`; as SUT it has to be an exclusion, because `current` does not go through the matrix.
- **Real interoperability defects.** A bug in one SDK, or two SDKs reading the specification differently. This is the group that should shrink: an exclusion is removed when the fix lands and the pair goes green.

## What is frozen

Because every SDK's CI pins `A2A_ITK_REVISION=main`:

- `GET /health`, port 8000, and the `tests`/`sut_sdk` request shape of `/run`
- the `passed`/`sdks`/`edges` keys of each result; everything else on a result was added later and is optional
- the legacy scenario format
- the published `itk_<sdk>.json` entry shape (fields are only ever added)

## Open items

Carried over from the project backlog. None is in progress unless noted.

**Failure-path coverage.** Everything today asserts on the happy path. Not yet tested: that SDKs raise structurally correct errors, that a status update before task creation is rejected, that subscribing to a completed task is refused.

**Schema validation.** Trace tokens prove a message got through, not that it was well-formed. Agent card exchange and message envelopes are not checked against the schema. The ACTS suite covers single-SDK conformance; the gap is cross-SDK.

**More operations.** `get_task`, `list_tasks`, the push-notification config CRUD operations and `get_extended_agent_card` are not exercised by any traversal.

**PR set over the full matrix.** The PR set names four peers explicitly. A `peers: all` PR set would catch more but costs nine agent builds per run; the cost has not been measured.

**Entry request URL.** `testlib.py` posts the initial request to `/jsonrpc/` or `/jsonrpc` depending on the first agent's language. The agents should agree on one, or ITK should read the URL from the card as the ACTS runner already does.
