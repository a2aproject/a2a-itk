# Worked example: human-approval lifecycle on top of `AUTH_REQUIRED`

**Status: illustrative, non-normative.** This directory is a worked example of
one way to build human-in-the-loop (HIL) approval semantics on top of A2A's
`AUTH_REQUIRED` task state. It is not part of `a2a-itk`'s test suites, is not
run by this repository's CI, and demonstrates application-policy choices that
A2A itself does not mandate. See
[`../README.md`](../README.md) for what *is* being proposed as portable, and
[`SECURITY_LIMITATIONS.md`](SECURITY_LIMITATIONS.md) before using any part of
this as a starting point for anything real.

## What it demonstrates

A three-process topology, all using real, officially released A2A SDKs (not a
hand-rolled protocol stand-in):

```
test_client (Python, a2a-sdk==1.2.2, real Client)
   -> coordinator (TypeScript, @a2a-js/sdk@1.3.0, real server+client)
        -> expense_agent (Python, a2a-sdk==1.2.2, real AgentExecutor/TaskUpdater)
```

`expense_agent` models an expense-submission tool gated behind a human
approval. When the test client requests an expense submission, the agent
transitions its task to `AUTH_REQUIRED` (the normative A2A protocol state for
"something outside the agent itself must authorize this before I can
proceed") instead of executing the protected tool immediately. A separate,
out-of-band, **non-A2A** endpoint, `/__human__/decision`, is the only place a
human decision ever gets recorded — this stands in for whatever a real
deployment would use (a Slack approval bot, an internal approvals UI, etc).
Once a decision is recorded, the test client resumes the A2A task; the
application-level `approval_gate.py` independently re-validates every
invariant below before letting the protected tool fire, regardless of what
the A2A task state machinery or any client claims.

`coordinator` sits between the two as a delegating parent agent: it receives
the request, forwards it to `expense_agent` as a child task, and must
propagate the child's `AUTH_REQUIRED` state faithfully to its own, outer task
rather than absorbing or misrepresenting it.

## The normative/policy distinction (read this first)

This is the single most important thing this example is trying to show, and
it is easy to lose when skimming the code, so it is stated up front and
repeated in [`../README.md`](../README.md):

| What | Source of the requirement |
| --- | --- |
| The task must enter `AUTH_REQUIRED` with a status message when something needs out-of-band authorization | **Normative A2A protocol** (already covered by `SEC-AUTH-005`) |
| A fresh client/connection querying `GetTask` on a pending task observes the current state, not a stale or default one | Likely-**normative A2A protocol** property, currently untested by ITK/ACTS and not yet expressible as an ACTS test in this runner (no fresh-connection directive exists) — a conceptual follow-up candidate only; see `../acts-draft/` for the full gap analysis |
| A delegating parent's task faithfully propagates a child task's `AUTH_REQUIRED`, correlated so an outer caller can observe both | **This fixture's own convention** (`coordinator.childTaskId` metadata) — valuable worked-example design, not a protocol requirement, since A2A defines no standard way to expose a child/delegated task id |
| The approved decision must bind to the *exact* action it was requested for, come from a trusted principal, be single-use, expire, and survive a naive retry or an out-of-band channel outage without the protocol state being misrepresented | **Application policy** (`approval_gate.py`'s own invariants) — A2A is silent here by design, because HIL authorization semantics are explicitly out of protocol scope |

Everything in the last two rows is "one way to build this correctly," shown
as a worked, runnable example — not a claim about what A2A requires, and not
something being proposed for ACTS inclusion.

## Twelve scenarios (H01-H12)

`test_client/scenarios.py` exercises twelve end-to-end scenarios against the
live stack. All twelve were validated (12/12 PASS) in the originating
fixture, along with two focused, mutation-tested unit tests of
`approval_gate.py`'s own logic
(`test_client/approval_policy_focused_tests.py`, 2/2 PASS). See
[`../TEST_RESULTS_SUMMARY.md`](../TEST_RESULTS_SUMMARY.md) for the full
matrix and for exactly what was (and was not) re-verified as part of this
proposal.

Only H09 (reconnection-observability) is identified as a plausible future
ACTS candidate, and even that is a conceptual sketch with a known execution
gap (see `../acts-draft/README.md`), not something proposed for merge. The
rest are included here, runnable, as supporting design for the worked
example — not as new conformance requirements.

## Running it

This is the same code as the originating fixture, reproduced here for
reference; it is not wired into `a2a-itk`'s own `uv run` / pytest flow. To run
it standalone:

```sh
cd docs/conformance/proposals/human-approval-lifecycle/worked_example
python3 -m venv ../py_venv
../py_venv/bin/pip install -r requirements-official-sdk.txt
(cd coordinator && npm ci)
./run.sh
```

The venv is created as `../py_venv` — a **sibling** of this `worked_example/`
directory, not inside it — because `run.sh` (reproduced unmodified from the
originating fixture) resolves its Python interpreter at that fixed sibling
path. Start every command above from inside `worked_example/` itself; do not
run them from one level up, since `requirements-official-sdk.txt` and
`coordinator/` are both relative to this directory.

`run.sh` starts the Expense Agent (`:8201`) and Coordinator (`:8301`), runs
all twelve scenarios plus the two focused policy tests, prints a PASS/FAIL
summary, then tears both processes down. Results land in `evidence/` (raw
wire traffic plus a `scenario-results.jsonl` line per scenario).

Requires Python 3.11+ and Node 20+.

## Security limitations

Read [`SECURITY_LIMITATIONS.md`](SECURITY_LIMITATIONS.md) in full before
treating any part of this as a starting point for a real system. In short:
no real principal authentication, a single hardcoded allowlist string for
the "trusted" principal, in-memory non-durable state, synthetic and
local-only throughout.
