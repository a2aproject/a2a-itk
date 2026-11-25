# Integrating an SDK with ITK

This page lists what an SDK repository has to provide for ITK to test it, and what has to change in this repository when a new SDK or a new language joins. The reference implementations are [a2a-python/itk](https://github.com/a2aproject/a2a-python/tree/main/itk) and [a2a-go/itk](https://github.com/a2aproject/a2a-go/tree/main/itk).

## In the SDK repository

### 1. The ITK agent under `itk/`

An A2A server built with the SDK, living in the SDK's own repository under `itk/`. ITK starts the same agent code whether it is the SUT (mounted from a working tree) or a peer (fetched at a tag or `main`), so there is exactly one agent per SDK to maintain.

**Process contract.** The launcher detects the language from the directory and starts the agent like this:

| Language | Detected by | Started with |
| --- | --- | --- |
| Go | `itk/main.go` | `go run -mod=readonly . --httpPort N --grpcPort M` |
| Python | `itk/main.py` | `uv run --locked main.py --httpPort N --grpcPort M` |
| TypeScript | `package.json` in the **parent** of `itk/` | `npm run itk-agent -- --httpPort N --grpcPort M`, from `itk/` |
| .NET | `itk/*.csproj` | `dotnet publish` once, then `dotnet itk/publish/<project>.dll --httpPort N --grpcPort M` |
| Java | `itk/pom.xml` | `mvn -Pitk -pl itk -am install`, then `mvn exec:java` with the ports as `exec.args` |
| Rust | `itk/Cargo.toml` | `cargo build --locked --release`, then the binary with the ports |

The agent must bind its HTTP server (JSON-RPC and HTTP+JSON) to `--httpPort` and its gRPC server to `--grpcPort`, both on localhost. Ports are allocated per run, so nothing may be hard-coded.

**Readiness.** The launcher considers the agent up when `GET http://127.0.0.1:<httpPort>/.well-known/agent-card.json` returns 200. The card must advertise every transport the agent serves in `supportedInterfaces`, with the URL each one is mounted at, because that is how the other agents in the cluster find them. ITK itself posts the initial request to `<card base>/jsonrpc/` (without the trailing slash for Go and Rust agents - see `testlib.py`).

**Instruction handling.** When a message arrives whose first part is a file part containing a serialised `itk.Instruction` ([`protos/instruction.proto`](../../protos/instruction.proto)), the agent executes it:

| Node | Agent does |
| --- | --- |
| `ReturnResponse{response, hold_task}` | Reply with `response` as text. If `hold_task`, leave the task in `WORKING` rather than completing it |
| `CallAgent{transport, agent_card_uri, instruction, streaming, behavior}` | Fetch the card at `agent_card_uri`, pick the interface for `transport`, send `instruction` as a new message with the SDK's own client, and return the called agent's reply text |
| `SeriesOfSteps{instructions, CONCAT}` | Execute the children in order; reply with their outputs concatenated |

The `behavior` on a `CallAgent` changes how the call is made:

- `send_message`: a plain send, streamed if `streaming` is set.
- `push_notification{url}`: register a push notification config pointing at `url` on the outgoing message and, as a server, push every status update of the task to the config's URL with the config's token. Both ends matter: the agent is a push client on one hop and a push server on the next.
- `resubscribe`: start a streaming send, read the task id from the first event, drop the connection, subscribe to the task again, collect the remaining events, then cancel the task. `hold_task` is set throughout so tasks stay open long enough to be resubscribed to.

Trace tokens are just text the agent concatenates; it does not need to know what they mean.

**Fields for A2A 1.1.** An agent that speaks A2A 1.1 also reads the fields below. The harness sets the first two only on steps carried out by an agent whose line in `matrix.yaml` lists A2A 1.1. An agent that speaks 1.0 or 0.3 is asked nothing new, and a traversal among such agents is the instruction it was before. An agent has to keep these fields when it passes an instruction on, whatever it speaks itself: a traversal that mixes versions carries them through every agent. The harness does not set the last two fields yet.

| Field | Agent does |
| --- | --- |
| `CallAgent.protocol_version` | Use the interface of the called agent's card that lists this version for `transport`, and send the version as `A2A-Version`. Fail the call when the card lists no such interface. When the field is empty, use the version the SDK negotiates |
| `ReturnResponse.report_protocol_version` | Follow `response` with a space and `served:<version>`, where `<version>` is the `A2A-Version` of the request the agent is answering, or `0.3` when the request carries none. This is how a scenario proves the version a hop was made at |
| `PushNotificationBehavior.protocol_version` | Put this version in the push notification config registered for the call |
| `ResubscribeBehavior.from_last_seen_generation` | Subscribe again from the last generation seen on the first stream, and accept a stream that starts with the task or one that continues from that generation |

The proto also defines four behaviors that need A2A 1.1 at both ends of a call. The harness does not send these either.

| Behavior | Calling agent | Called agent |
| --- | --- | --- |
| `live_message{follow_ups, foreign_task_request}` | Open a live exchange (`SendLiveMessage`) with the instruction as its first message. Send `follow_ups` more messages to the task, and one request that names another task if `foreign_task_request` is set. Read the exchange until the reply and an answer to every request have arrived. Return the reply, the text of each answer, and `stream-error:<code>` for a request that was refused | Carry out the instruction. Answer the n-th later message with a status update whose text is `live-echo:<n>`, counting from 1. Refuse a request for another task with a `StreamError` and keep the exchange open |
| `user_artifacts{text}` | Send the instruction together with one artifact in `artifactUpdates`. The artifact has one text part, `text`, and no generation. Return the reply | Store the artifact and add `artifact-received:<text>` to the reply |
| `multi_turn{stale_attempt}` | Send the instruction on a stream and read it until the task asks for input, without waiting for the stream to close. Send a message to the task with `ifGenerationMatch` set to the last generation seen, and return the reply. If `stale_attempt` is set, first send one message with an older generation, expect a generation mismatch error, and add `generation-mismatch:ok` to what is returned | Honor `ReturnResponse.await_input`: move the task to `INPUT_REQUIRED` first, and carry out the whole instruction when the next message for the task arrives |
| `long_poll` | Send the instruction with `returnImmediately`. Repeat `GetTask` with `currentGeneration` set to the last generation seen until the task is in a terminal state, up to a limit of the agent's own. Return the reply and `long-poll:<generation>`, the generation of the task at the end | Honor `ReturnResponse.hold_ms`: after carrying out the instruction, wait that long with the task in `WORKING`, then complete it |

`hold_ms` and `await_input` are about the task, as `hold_task` is. An agent honors one when any `ReturnResponse` it gives itself sets it; one in an instruction that the agent passes on does not count. The largest `hold_ms` applies, and `hold_task` overrides it.

**Version negotiation.** Peers on a `v03` line send A2A 0.3 requests and expect 0.3 responses. An SDK that has a 0.3 compatibility layer should serve it on the same HTTP port (the ITK agents publish a 0.3 interface alongside the 1.0 one). An SDK without one records the fact in [`known_failures.yaml`](../../known_failures.yaml), as a2a-rs does.

### 2. Proto stubs

The agent imports stubs generated from `protos/instruction.proto`. Who generates them depends on how the agent was started:

- **As the SUT**, the SDK's `itk/run_itk.sh` does, on the host, in its `itk_generate_protos` hook. Locally with `run_tests.py --mount` you run the same commands yourself first.
- **As a peer**, the launcher does, in [`test_suite/launcher/codegen.py`](../../test_suite/launcher/codegen.py). It mirrors each SDK's hook, so the hook and the preparer must agree on the output layout:

  | Language | Stubs land in | How |
  | --- | --- | --- |
  | Python | `itk/pyproto/` | `grpc_tools.protoc`, with the import patched to be relative |
  | Go | `itk/pb/` | `protoc` with `protoc-gen-go` and `protoc-gen-go-grpc` |
  | TypeScript | `itk/pb/instruction.ts` | `buf generate` using the SDK's own `buf.gen.yaml` |
  | Rust | - | `a2a-itk/` symlinked into `itk/`; `build.rs` runs `prost_build` on `a2a-itk/protos/instruction.proto` |
  | Java | - | `a2a-itk/` symlinked into `itk/`; `protobuf-maven-plugin` reads the same path |
  | .NET | - | Nothing. The .NET agent keeps a hand-written C# translation of the proto (`itk/InstructionProto.cs`), which has to be updated by hand when the proto changes |

Generated files are not committed in the SDK repos; the hooks and preparers regenerate them, and `itk_extra_cleanup` removes them after a run.

### 3. `itk/run_itk.sh`

A shim over the shared driver. Set `ITK_SDK_NAME`, override the few variables that differ for your repo, define the two hooks, clone `a2a-itk`, source the driver. Every current shim is reproduced in [`scripts/README.md`](../../scripts/README.md#shims); copy the closest one.

The things that commonly need overriding:

| Variable | When |
| --- | --- |
| `ITK_SDK_REPO` | The GitHub repo name is not `a2a-<ITK_SDK_NAME>` (a2a-rs) |
| `ITK_MATRIX_SDK` | The key in `matrix.yaml` differs from the SDK name (a2a-js is `ts`) |
| `ITK_COPY_PROTO=0` | The build reads the proto from the `a2a-itk` checkout itself (Java, Rust) |
| `ITK_MOUNT_ITK_DIR=0` | The repo-root mount already exposes `itk/` and a second mount would break the build (Java) |
| `ITK_EXTRA_DOCKER_ARGS` | The agent needs an environment variable in the container (Go sets `GOLANG_PROTOBUF_REGISTRATION_CONFLICT=warn`) |

Set `ITK_SCENARIO_SET=shared` to use the shared scenario sets from this repo instead of a local `scenarios.json`. All six SDKs do.

### 4. Workflows

Two workflows, copied from any existing SDK and described in [04_ci.md](04_ci.md#the-pull-request-workflow): `itk.yaml` on pull requests and a nightly on a cron. The nightly needs `contents: write` to upload `itk_<sdk>.json` to the `nightly-metrics` release.

## In this repository

### A new SDK in an existing language

1. Add the line to [`matrix.yaml`](../../matrix.yaml). `v10` points at `main`. Give it a `transports` ceiling only if the SDK genuinely cannot serve a transport.
2. Run the nightly set against it locally once, and record anything broken in `known_failures.yaml` with a reason.
3. Add it to the dashboard: a `fetch` line in `dashboard/scripts/fetch-metrics.sh` and an entry in `dashboard/src/shared/sdks.ts`.
4. Update the support matrix in [06_status.md](06_status.md).

Every `peers: all` scenario picks the new SDK up from the matrix; the shared PR set names its peers explicitly and is left alone.

### A new language

Everything above, plus code in the launcher:

| File | Add |
| --- | --- |
| `test_suite/current.py` | Detection rule and a `_spawn_<lang>` function |
| `test_suite/launcher/builders.py` | A `Language` member, the same detection rule, and a builder that produces the `.itk-built` state |
| `test_suite/launcher/codegen.py` | A preparer that generates the stubs the way the SDK's hook does |
| `Dockerfile` | The toolchain, with pinned versions |
| `tests/` | Cases in `test_spawn_contract.py`, `test_builders.py`, `test_codegen.py`, `test_image_contents.py` |

The cache key includes the image digest, so a Dockerfile change invalidates every cached peer tree on the next run. That is intended.

### A line for an A2A version not released yet

An SDK that implements a new A2A version on a development branch gets a line of its own for it, such as `v11` for A2A 1.1:

```yaml
python:
  v11: {repo: a2aproject/a2a-python, ref: <40-character commit SHA>, protocol_versions: ["1.1", "1.0", "0.3"]}
```

- **Pin the line to a commit, not to the branch.** The traversal suite gates pull requests in every SDK, so a development branch that breaks overnight must not fail them. A pull request here moves the pin. A pin to a commit that does not exist fails the run when it is planned.
- **List `protocol_versions`.** Only `v10` and `v03` have a default.
- **After the SDK's first release of that version,** point the line at `main`, as `v10` is today.
- **When the SDK's `main` moves to the new version,** pin its `v10` line to the last 1.0 release tag, so that the 1.0 agent stays in the matrix.

### A `v03` line

The 0.3 releases of each SDK predate ITK, so a `v03` peer is a tag of the form `v0.3.N+itk`: the release commit with the `itk/` agent added on top. To make one, branch from the release tag, add an agent that builds against that version, tag it, and point the matrix at the tag. Only Python, Go and TypeScript have one today, because only those SDKs shipped a 0.3 line.
