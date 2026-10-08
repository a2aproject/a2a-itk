<!-- markdownlint-disable no-inline-html first-line-heading -->

<p align="center">
  <a href="https://a2a-protocol.org/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/a2aproject/A2A/refs/heads/main/docs/assets/a2a_logo/white/SVG/a2a_white.svg">
      <img src="https://raw.githubusercontent.com/a2aproject/A2A/refs/heads/main/docs/assets/a2a_logo/color/SVG/a2a_color.svg" width="360" alt="Agent2Agent Protocol">
    </picture>
  </a>
</p>

<h1 align="center">Integration Test Kit</h1>

<p align="center">
  <b>SDK compatibility, every night.</b><br>
  Interoperability and conformance testing for the Agent2Agent (A2A) SDKs.
</p>

<p align="center">
  <a href="https://a2aproject.github.io/a2a-itk/dashboard/">Dashboard</a> |
  <a href="docs/README.md">Documentation</a> |
  <a href="https://a2a-protocol.org/v1.0.0/specification/">Specification</a>
</p>

<p align="center">
  <a href="https://github.com/a2aproject/a2a-itk/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/a2aproject/a2a-itk/ci.yml?branch=main&style=flat-square&label=CI&labelColor=09090b" alt="CI"></a>
  <a href="https://a2aproject.github.io/a2a-itk/dashboard/"><img src="https://img.shields.io/badge/dashboard-live-065f46?style=flat-square&labelColor=09090b" alt="Dashboard"></a>
  <a href="https://a2a-protocol.org/v1.0.0/specification/"><img src="https://img.shields.io/badge/A2A_Protocol-v1.0-525252?style=flat-square&labelColor=09090b" alt="A2A Protocol v1.0"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-525252?style=flat-square&labelColor=09090b" alt="License: Apache 2.0"></a>
</p>

<!-- markdownlint-enable no-inline-html -->

---

Two test suites for the SDKs of the [A2A protocol](https://github.com/a2aproject/A2A) live in this repository. They share one agent launcher, one container image and one CI driver, but they answer different questions - the same two questions the dashboard has a tab for:

| 🔀 Interoperability - **ITK** | 📐 Conformance - **ACTS** |
| --- | --- |
| *Every SDK against its peers.* | *Each SDK against the specification.* |
| Can this SDK talk to the other SDKs, in both directions, over every transport? | Does this SDK, on its own, do what the specification says? |
| Starts a cluster of agents built from several SDKs and versions, sends one nested instruction through all of them over JSON-RPC, gRPC or HTTP+JSON, and checks the trace that comes back | Starts one agent and runs the ACTS corpus from the A2A repository against it, one report per binding |

- 🧱 **One agent, both suites.** Each SDK repository carries a small agent under `itk/`; ITK walks it as a peer, ACTS interrogates it as the system under test.
- 🔌 **Three transports.** JSON-RPC, gRPC and HTTP+JSON, with streaming, push notifications and resubscription covered on the ITK side and every binding reported separately on the ACTS side.
- 🐳 **One pipeline everywhere.** The same code runs from the local CLI, inside the container and behind the HTTP service CI calls, so a result reproduces.
- 🌙 **Every night.** Each SDK publishes its nightly results; the dashboard shows the latest night and the trend.

## 🧭 What it is for

- **Gating SDK changes.** An SDK's pull request runs a shared ITK scenario set against reference peers and the ACTS corpus against the SDK's own agent, through the same `itk/run_itk.sh` in every repository. Nightly runs cover the full peer matrix and publish results.
- **Checking a local checkout before pushing.** Both suites run from this repo against an SDK directory on disk, with the pipeline CI uses.
- **Seeing where the ecosystem stands.** The dashboard, per SDK and per suite.

## 🧩 Supported SDKs

| SDK | ITK peer lines | ACTS bindings | Dashboard |
| --- | --- | --- | --- |
| Python | 1.0, 0.3 | JSON-RPC, gRPC, REST | [ITK](https://a2aproject.github.io/a2a-itk/dashboard/#/itk/python) / [ACTS](https://a2aproject.github.io/a2a-itk/dashboard/#/acts/python) |
| Go | 1.0, 0.3 | JSON-RPC, gRPC, REST | [ITK](https://a2aproject.github.io/a2a-itk/dashboard/#/itk/go) / [ACTS](https://a2aproject.github.io/a2a-itk/dashboard/#/acts/go) |
| TypeScript | 1.0, 0.3 | JSON-RPC, gRPC, REST | [ITK](https://a2aproject.github.io/a2a-itk/dashboard/#/itk/ts) / [ACTS](https://a2aproject.github.io/a2a-itk/dashboard/#/acts/ts) |
| Java | 1.0 | JSON-RPC, gRPC, REST | [ITK](https://a2aproject.github.io/a2a-itk/dashboard/#/itk/java) / [ACTS](https://a2aproject.github.io/a2a-itk/dashboard/#/acts/java) |
| Rust | 1.0 | JSON-RPC, gRPC, REST | [ITK](https://a2aproject.github.io/a2a-itk/dashboard/#/itk/rust) / [ACTS](https://a2aproject.github.io/a2a-itk/dashboard/#/acts/rust) |
| .NET | 1.0 | JSON-RPC, gRPC, REST | [ITK](https://a2aproject.github.io/a2a-itk/dashboard/#/itk/dotnet) / [ACTS](https://a2aproject.github.io/a2a-itk/dashboard/#/acts/dotnet) |

Which repository and ref each peer line means is in [`matrix.yaml`](matrix.yaml). Last night's results are on the dashboard; what each suite covers, what is frozen and what is still open is in [ITK status](docs/interoperability/06_status.md) and [ACTS status](docs/conformance/06_status.md).

## 🚀 Quick start

You need [uv](https://docs.astral.sh/uv/) and, for ITK, the toolchain of every peer you select (builds run on your machine with each SDK's native tools; the bundled smoke set uses only Python and Go peers). The container has every toolchain if you would rather not install them.

### ITK

```bash
uv run run_tests.py                                           # bundled smoke set
uv run run_tests.py --scenarios scenarios/traversal/pr.yaml   # the shared PR set
uv run run_tests.py --list-sdks                               # peers matrix.yaml can resolve
uv run run_tests.py --dry-run                                 # plan only, no network

# a local SDK checkout as the code under test
uv run run_tests.py --mount ~/Source/a2a-python/itk --sut-sdk python \
    --scenarios scenarios/traversal/pr.yaml
```

Peers are fetched and built on first use and cached under `~/.cache/a2a-itk`, so the first run is slow and the next ones are not. Details: [running ITK](docs/interoperability/01_running.md).

### ACTS

```bash
uv run run_acts.py --mount ~/Source/a2a-python/itk --sdk a2a-python                   # JSON-RPC
uv run run_acts.py --mount ~/Source/a2a-python/itk --sdk a2a-python --transport all   # every binding
uv run run_acts.py --mount ~/Source/a2a-python/itk --sdk a2a-python -t CORE-SEND-001 -v
```

Exit code 0 means conformant: every `must` test passed on every binding that ran. Details: [running ACTS](docs/conformance/01_running.md).

### In the container

```bash
docker build -t itk_service .

docker run --rm \
  -v "$HOME/Source/a2a-python:/app/agents/repo" \
  -v "$HOME/.cache/a2a-itk-launcher:/root/.cache/a2a-itk" \
  itk_service uv run run_tests.py \
    --scenarios scenarios/traversal/pr.yaml --sut-sdk python --mount /app/agents/repo/itk
```

Run with no command and the image starts the HTTP service CI talks to, with `POST /run` for ITK and `POST /run-acts` for ACTS:

```bash
docker run -d --name itk-service -p 8000:8000 \
  -v "$HOME/Source/a2a-python:/app/agents/repo" \
  -v "$HOME/.cache/a2a-itk-launcher:/root/.cache/a2a-itk" \
  itk_service

curl http://127.0.0.1:8000/health
```

### In an SDK repository

Each SDK's `itk/run_itk.sh` is a short shim over [`scripts/run_itk_shared.sh`](scripts/README.md): it builds and starts the container, mounts the SDK into it, posts the request and reports the result. Running it locally is the same as the workflow:

```bash
cd ~/Source/a2a-python/itk
A2A_ITK_REVISION=main bash run_itk.sh                                                      # ITK, PR set
A2A_ITK_REVISION=main ITK_ACTS_RUN=1 ITK_ACTS_TRANSPORTS=jsonrpc,grpc,rest bash run_itk.sh  # ACTS
```

How the workflows are wired, and what the nightly publishes: [ITK in CI](docs/interoperability/04_ci.md), [ACTS in CI](docs/conformance/04_ci.md).

### Unit tests

```bash
uv run pytest
```

No network and no SDK needed. This is what this repository's own CI runs.

## 📊 Dashboard

<https://a2aproject.github.io/a2a-itk/dashboard/>

The latest night per SDK for both suites and the trend over the rolling window, in the same pass / fail / skip vocabulary the reports use. It is a daily snapshot, not live monitoring: each SDK's nightly uploads its results to a `nightly-metrics` release in its own repository, and a workflow here fetches them at 04:00 UTC and rebuilds the site. Adding an SDK to it is described in [ITK in CI](docs/interoperability/04_ci.md#the-dashboard).

## 📚 Documentation

Everything beyond this page is in [`docs/`](docs/README.md), split the same way as the suites. Each folder starts with an overview and continues in reading order:

| | Interoperability (ITK) | Conformance (ACTS) |
| --- | --- | --- |
| Overview | [README](docs/interoperability/README.md) | [README](docs/conformance/README.md) |
| 01 Running | [locally, in Docker, via the service](docs/interoperability/01_running.md) | [locally, in Docker, via the service](docs/conformance/01_running.md) |
| 02 Architecture | [resolve -> launch -> traverse -> verify](docs/interoperability/02_architecture.md) | [YAML test -> wire calls -> verdict](docs/conformance/02_architecture.md) |
| 03 Inputs | [scenarios, `matrix.yaml`, known failures](docs/interoperability/03_scenarios.md) | [the corpus and where it comes from](docs/conformance/03_corpus.md) |
| 04 CI | [PR and nightly workflows](docs/interoperability/04_ci.md) | [PR and nightly workflows](docs/conformance/04_ci.md) |
| 05 SDK integration | [what an SDK's `itk/` agent must do](docs/interoperability/05_sdk-integration.md) | [behaviors, contract file, auth, deviations](docs/conformance/05_sdk-integration.md) |
| 06 Status | [coverage and open items](docs/interoperability/06_status.md) | [gating, corpus pin and open items](docs/conformance/06_status.md) |

## 🤝 Contributing

Contributions are welcome! Please see the [CONTRIBUTING.md](CONTRIBUTING.md) file for guidelines on how to get involved.

## 📄 License

This project is licensed under the Apache 2.0 License. See the [LICENSE](LICENSE) file for more details.
