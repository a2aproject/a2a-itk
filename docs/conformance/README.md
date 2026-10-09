# ACTS: conformance testing

ACTS is the **A2A Conformance Test Specification**: a format for writing protocol conformance tests as YAML, plus a corpus of such tests, both maintained in the [A2A repository](https://github.com/a2aproject/A2A). This repository contains a runner for that format and a mirror of the corpus, and uses them to check each SDK against the specification.

Where ITK asks "can these SDKs talk to each other", ACTS asks "does this one SDK do what the spec says". The two are separate suites on purpose. An ACTS run starts **one** agent - the SDK's ITK agent, the same one the traversal suite uses - and interrogates it: send this message, expect that task state; request this task id, expect `TaskNotFoundError`; open this stream, expect these events in this order.

## How a test works, in short

A test is a list of steps. Each step names an abstract operation (`send_message`, `get_task`, `cancel_task`, ...) and parameters, and says what to expect back. The runner translates the abstract operation to a concrete call on one transport binding - a JSON-RPC method, a REST path or a gRPC RPC - sends it, and checks the response against the expectations. A value captured from one step can be used in the next.

```yaml
- id: CORE-SEND-001
  name: "SendMessage returns a completed task"
  level: must
  requires_behaviors: ["tck-complete-task"]
  steps:
    - id: send
      operation: send_message
      params:
        message:
          role: ROLE_USER
          parts: [{text: "tck-complete-task hello world"}]
      expect:
        status: 200
        body:
          task:
            status: {state: TASK_STATE_COMPLETED}
```

The text `tck-complete-task` is a **behavior prefix**: the test agent is required to react to it in a defined way (here, complete the task). This is how a test can ask a server for a specific outcome without knowing anything about the SDK. Each SDK declares which prefixes its agent implements in a small contract file, and the runner holds it to that list.

Every test has a level - `must`, `should` or `may`. An SDK is **conformant** on a binding when every graded `must` test passed. Each binding gets its own report; conformance overall is passing on all of them.

## Reading order

| Doc | Read it when you want to |
| --- | --- |
| [01_running.md](01_running.md) | Run the corpus against a local SDK checkout, in Docker, or through the service |
| [02_architecture.md](02_architecture.md) | Understand how a YAML test becomes wire calls and a verdict, and what the runner does that the spec leaves open |
| [03_corpus.md](03_corpus.md) | Know where the tests come from, how they are organised, and how to refresh the mirror |
| [04_ci.md](04_ci.md) | See how SDK repositories run ACTS on pull requests and nightly, and where reports go |
| [05_sdk-integration.md](05_sdk-integration.md) | Make an SDK's test agent ACTS-ready: behaviors, contract file, auth, deviation modes |
| [06_status.md](06_status.md) | Know which SDKs run it, how gating works today, and what is still open |

## Vocabulary

| Term | Meaning |
| --- | --- |
| SUT | System under test: the one agent an ACTS run talks to. Always the mounted `current` checkout |
| binding, transport | `jsonrpc`, `grpc` or `rest` (ACTS's name for HTTP+JSON) |
| corpus | The set of `*.acts.yaml` files. 113 tests in 14 files plus a manifest |
| suite | A named group of tests inside one file (`core-operations`, `streaming`, ...) |
| level | `must`, `should`, `may` - the RFC 2119 weight of what the test checks |
| behavior, `tck-*` prefix | A text prefix the SUT recognises and reacts to in a defined way |
| contract, `sut-behaviors.yaml` | The file in which an SDK declares which prefixes its agent implements |
| precondition | A condition on the SUT's agent card (a capability, a security scheme) a test needs; unmet means skip |
| runner requirement | A capability of the runner itself a test needs (a webhook receiver, header inspection, ...) |
| deviation | A second SUT started in a different configuration to reach tests the default one cannot |
| section N | Section N of the ACTS specification, `docs/acts-specification.md` in the A2A repo |

## Proposals

Candidate additions that are not (yet) part of the corpus or the runner live
under [`proposals/`](proposals/). See
[`proposals/human-approval-lifecycle/README.md`](proposals/human-approval-lifecycle/README.md)
for a non-normative human-approval (`AUTH_REQUIRED`) worked example and
a conceptual reconnection-observability follow-up (not a ready ACTS test).
