# ACTS: current state

A snapshot of where conformance testing stands: which SDKs run it, what gates and what does not, what the corpus is pinned to, and what is still open.

## Who runs it

All six SDKs run ACTS on pull requests and nightly through the shared driver, over all three bindings, declare the full set of fifteen standard `tck-*` prefixes in `acts/sut-behaviors.yaml`, and implement both deviation modes and `tck-client-parse`. One narrowing: the .NET agent advertises `pushNotifications: false`, because the SDK's server does not support push configs, so the push-notification tests are skipped there rather than failed. Workflow names, schedules and asset names per SDK are in [04_ci.md](04_ci.md#where-each-sdk-stands).

A `must` failure on the dashboard is an SDK defect or a spec disagreement to resolve in that SDK's repository, not a runner problem, as long as the same corpus passes elsewhere. How to read one is in [04_ci.md](04_ci.md#reading-a-failed-run).

## Gating

- **Nightly** publishes whatever happened. Conformance is a recorded metric; the job fails only if the service errored or the report was malformed.
- **Pull requests** run the suite with `--require-conformant`, which turns a `must` failure into a non-zero exit - but every SDK's PR workflow still has `continue-on-error: true` on that step, so a red ACTS job does not block a merge anywhere. The line dates from when no SDK was conformant. For an SDK that is conformant on the dashboard it can be removed today; until it is, a regression there shows up on the dashboard the next morning rather than on the pull request.
- ITK's traversal job remains the actual merge gate in all six repos.

## What the corpus is pinned to

The mirrored corpus is a byte-identical copy of `tests/acts/*.acts.yaml` from the A2A repository at commit `a8c99376...` on branch `conformance-spec-adjustments` - PR [#2227](https://github.com/a2aproject/A2A/pull/2227), which targets the branch of [#1882](https://github.com/a2aproject/A2A/pull/1882), where the ACTS specification itself is proposed. Neither is merged into A2A `main`. Both the specification and the test files can still change under review, and the `1.0` in `acts_version` is the draft's own number, not a published release. The pin, and the procedure for moving it, are in [03_corpus.md](03_corpus.md#refreshing-the-mirror).

An earlier pin contained tests whose preconditions no agent could meet (a capability the protocol does not define). The runner reports such tests separately, as `PRECONDITION CANNOT BE SATISFIED`, so a corpus problem can never read as an SDK's skip. The current pin has none.

## What this runner decides on its own

The specification leaves several things to the runner, and an SDK that passes here is passing *this* runner's reading of them. Each is explained in [02_architecture.md](02_architecture.md) and listed in [03_corpus.md](03_corpus.md#what-the-runner-adds-on-top):

- the credential tokens, the fixed `otherUserTaskId`, and the rule that raw steps carry no credential;
- the two deviation passes and their environment variables;
- `tck-client-parse` as the way to reach an SDK's client;
- behaviour gaps as failures rather than skips;
- transport-scoped tests counted only against their own binding;
- the A2A spec's method and error tables over the ACTS spec's copies where the two disagree.

If a second ACTS runner appears, these are the points where its results could legitimately differ from this one's.

## What is frozen

- The `POST /run-acts` request and response, and the report document shape (section 13.1): dashboards and the nightly processor read them. Fields are added, not changed.
- The `acts-report-<sdk>-<transport>-<timestamp>.json` name and the `acts_<sdk>.json` history entry shape.
- The behaviour-contract path `acts/sut-behaviors.yaml` at the SDK root.
- The environment variable names `ITK_ACTS_REDUCED_CAPABILITIES` and `ITK_ACTS_AUTH`, and the token strings - six agents read them.

## Open items

- **Promote the PR job to a gate**: remove `continue-on-error` in the SDKs that are conformant, and in the others once they are.
- **Corpus pin**: move to A2A `main` once #1882/#2227 land. Until then a change to the draft can arrive here only by a deliberate re-pin.
- **SUT output on the CLI**: `run_acts.py` discards the agent's stdout and stderr. A start-up failure has to be reproduced by hand; the service path captures logs under `/app/logs` but the CLI has no `--log-dir`.
- **Leftover on the ACTS CI path**: the driver still selects a traversal scenario file and builds a `/run` body before branching to `/run-acts`. Harmless, but a traversal scenario problem could fail an ACTS job for an unrelated reason.
