# a2a-itk documentation

This repository hosts two test suites for A2A SDKs. They share the agent launcher, the container image and the CI driver, but they answer different questions and are documented separately.

| Suite | Question it answers | Docs |
| --- | --- | --- |
| **ITK** (interoperability) | Can this SDK talk to the other SDKs, in both directions, over every transport? | [interoperability/](interoperability/README.md) |
| **ACTS** (conformance) | Does this SDK, on its own, behave the way the A2A specification says? | [conformance/](conformance/README.md) |

Start with the overview in each folder; it links to the rest in reading order.

## Where things live in the repo

| Path | What it is |
| --- | --- |
| `itk_runner.py`, `run_tests.py`, `itk_service_v2.py` | ITK pipeline, its local CLI, and the HTTP service (which also serves ACTS) |
| `acts_runner.py`, `run_acts.py` | ACTS pipeline and its local CLI |
| `test_suite/launcher/` | Fetches, builds, starts and stops agents. Used by both suites |
| `test_suite/scenarios/` | ITK scenario schemas and the resolver that binds them to real agents |
| `test_suite/acts/` | ACTS schema, loader, transport dispatchers, assertion engine, report writer |
| `test_suite/__init__.py`, `testlib.py` | ITK traversal: Euler circuits, nested instructions, trace verification |
| `scenarios/traversal/` | Shared ITK scenario sets (`pr.yaml`, `nightly.yaml`, `smoke.yaml`) |
| `scenarios/acts/` | The ACTS corpus, mirrored byte-for-byte from the A2A repository |
| `matrix.yaml` | Which repo and ref each peer identifier (`python_v10`, `go_v03`, ...) means |
| `known_failures.yaml` | ITK combinations that are known broken and excluded, each with a reason |
| `protos/instruction.proto` | The traversal instruction every SDK's ITK agent understands |
| `scripts/` | The shared `run_itk.sh` driver and the result/metrics scripts SDK repos call |
| `dashboard/` | The GitHub Pages site showing nightly ITK and ACTS results |
| `tests/` | Unit tests for everything above. `uv run pytest`, no network needed |
