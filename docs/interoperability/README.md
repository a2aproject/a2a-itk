# ITK: interoperability testing

ITK checks that A2A SDKs can talk to each other. It starts a small cluster of agents built from different SDKs and versions, sends one message that has to travel through every agent in the cluster, and checks that the message came back with proof that every hop happened.

The code under test is always one SDK checkout, called the **SUT** (system under test) or `current`. Everything else in the cluster is a **peer**: a released version of some SDK, fetched from its own repository and built on the spot.

## How a test works, in short

1. A scenario names the agents and the shape of the graph between them, for example a star with the SUT in the middle and four peers around it.
2. ITK builds one nested instruction that walks every edge of that graph exactly once (an Euler circuit) and sends it to the first agent over JSON-RPC.
3. Each agent reads its part of the instruction, calls the next agent over the transport the scenario asks for (JSON-RPC, gRPC or HTTP+JSON), and appends a trace token like `[current -> go_v10 (grpc)]` to the reply.
4. The reply unwinds back to the first agent and then to ITK. If every expected token is present, the scenario passed.

Push notifications and task resubscription are tested the same way, with the agents pushing trace tokens to a mock notification server instead of (or as well as) returning them.

## Reading order

| Doc | Read it when you want to |
| --- | --- |
| [01_running.md](01_running.md) | Run scenarios on your machine, in Docker, or against the HTTP service |
| [02_architecture.md](02_architecture.md) | Understand the pipeline, the launcher and how a traversal is built and checked |
| [03_scenarios.md](03_scenarios.md) | Write or change a scenario, add a peer to `matrix.yaml`, record a known failure |
| [04_ci.md](04_ci.md) | See how SDK repositories run ITK on pull requests and nightly, and where results go |
| [05_sdk-integration.md](05_sdk-integration.md) | Add a new SDK, or understand what an SDK's `itk/` agent has to do |
| [06_status.md](06_status.md) | Know what is covered today, how exclusions work, and what is still open |

## Vocabulary

| Term | Meaning |
| --- | --- |
| SUT, `current` | The SDK checkout under test. Mounted into the run, never fetched |
| peer | Any other agent in the cluster. Fetched from GitHub at the ref in `matrix.yaml` |
| agent identifier | An `<sdk>_<line>` pair such as `python_v10` or `go_v03`, with an optional `_N` suffix for a second instance (`ts_v10_2`) |
| line | A version line: `v10` is the SDK's `main` (A2A 1.0), `v03` is a tagged A2A 0.3 release with the ITK agent grafted on |
| transport | `jsonrpc`, `grpc` or `http_json` |
| behavior | What each hop asks the next agent to do: `send_message`, `push_notification` or `resubscribe` |
| topology | `star`, `chain`, `euler` (complete digraph), or an explicit edge list |
| scenario | One named graph plus transport, behavior and streaming flag. One pass/fail result |
| tier | `pr` or `nightly` - which CI run a scenario belongs to |
