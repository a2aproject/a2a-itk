# Test results summary

## What ran, and where

| Test mechanism | Ran in this session? | Result |
| --- | --- | --- |
| `a2a-itk`'s own ACTS loader/runner against the H09-derived draft test (`acts-draft/README.md`) | **No** | Not applicable — see "Why ACTS's own runner could not be exercised" below |
| The worked example's own `./run.sh` (all 12 scenarios + 2 focused policy tests), against the live, real-SDK stack | **Yes** | 12/12 scenario PASS, 2/2 focused test PASS, evidence verifier (`verify_evidence.py`) reports PASS |
| `a2a-itk`'s own unit test suite (`uv run pytest`), to confirm this branch's doc-only changes do not break the existing repo | Not run this session (no code in `test_suite/`, `scenarios/acts/`, or any file the existing unit tests assert against was modified — only new files under `docs/conformance/proposals/` were added) | N/A |

## Why ACTS's own runner could not be exercised against the draft test

`a2a-itk`'s ACTS runner (`test_suite/acts/`, driven by `acts_runner.py` /
`run_acts.py`) loads tests strictly from `scenarios/acts/*.acts.yaml`, which
is a byte-identical mirror of the upstream `a2aproject/A2A` corpus (see
`scenarios/acts/PROVENANCE.md`). Running the draft `SEC-AUTH-007` test
through this repository's own runner would require first adding it to
`scenarios/acts/auth-security.acts.yaml`, which would itself violate the
provenance convention this repository states for itself ("changes go
upstream first... the runner never rewrites a document on the way in").
So: not a limitation of this environment, but a deliberate decision to
respect the repository's own stated process rather than force a test into a
file this PR has no business editing.

What *can* be said with confidence about the draft test's correctness is
that it is a direct YAML transcription of a scenario (`scenario_h09` in
`worked_example/test_client/scenarios.py`) that was run for real, this
session, against a live stack built from the officially released
`a2a-sdk==1.2.2` (Python) and `@a2a-js/sdk@1.3.0` (TypeScript) SDKs — see
below.

## Supporting evidence: the worked example's own scenario run (this session)

Executed via `./run.sh` (the worked example's own test harness) against the
live Expense Agent (`:8201`) and Coordinator (`:8301`) processes, this
session, immediately before drafting this proposal:

```
[PASS] H01: Full happy path through coordinator with real SDK events.
[PASS] H02: Denial path through coordinator with real SDK events.
[PASS] H03: APPLICATION-POLICY test, not an A2A protocol one.
[PASS] H04: Caller-visible check via get_task + ledger size, through full real-SDK stack.
[PASS] H05: Approval itself was for different content than requested.
[PASS] H06: Duplicate approval did not execute a second time (blocked by the SDK's own terminal-task guard in this full-HTTP run; see note below).
[PASS] H07: Post-approval resubmission with changed terms did not execute (same SDK-layer guard as H06; see note below).
[PASS] H08: Verifies parent/child correlation + faithful AUTH_REQUIRED propagation.
[PASS] H09: New A2A client/connection queries same task_id while pending.
[PASS] H10: AUTH_REQUIRED means blocked-on-human-input; a client retry must not be treated as a decision.
[PASS] H11: Exercises execute_protected_submit's untrusted_principal check (mock allowlist).
[PASS] H12: Channel actively down (vs H03's silence), via fault injection.

2/2 focused approval-policy tests passed (replay-safety, action-binding-tamper).

EVIDENCE VERIFICATION SUMMARY: RESULT: PASS (all checks satisfied)
```

**Note on H06/H07's actual mechanism:** in this full-HTTP run, the SECOND
request in each scenario (the duplicate approval for H06, the tampered
resubmission for H07) was rejected by the real SDK's own request-handler
layer (`ActiveTask.start()`'s terminal-state guard, raising
`UnsupportedOperationError: task is in a terminal state`) before the
request ever reached `approval_gate.py`'s own `used`/digest-mismatch
checks a second time. That is a real, valid, caller-visible outcome, but it
is the **SDK's** guard that was actually exercised here, not the
application's. Direct evidence that `approval_gate.py`'s own single-use and
digest-mismatch invariants independently hold — with no SDK/task/HTTP layer
anywhere in the call path to "help" — comes instead from the two focused,
mutation-tested unit tests above
(`approval_policy_focused_tests.py`): `APPROVAL-POLICY-01-replay-safety`
races two concurrent calls against the same challenge purely through
`approval_gate.py`'s own lock/flag, and
`APPROVAL-POLICY-02-action-binding-tamper` submits a tampered action
against a freshly-approved challenge with zero task/executor/HTTP
involvement. Treat H06/H07 (full-stack) and the two focused tests
(in-process) as two separately-useful, separately-attributed pieces of
evidence, not one.

Raw evidence (`scenario-results.jsonl`, wire-level HTTP traffic) for the H09
row specifically, from this session's run:

```json
{"scenario": "H09", "expected": "Fresh client reconnecting via get_task observes current AUTH_REQUIRED state, not stale/default data", "actual": "state_from_original_client=TASK_STATE_AUTH_REQUIRED, state_from_fresh_client=TASK_STATE_AUTH_REQUIRED", "status": "PASS", "note": "New A2A client/connection queries same task_id while pending."}
```

## Condensed 12-scenario matrix

Full detail, expected/actual text, and the corrected H12 wording (clarifying
that the simulated channel outage affects only the in-band A2A resumption
path, never the out-of-band decision-recording call) are in the originating
fixture's `TEST_RESULTS.md`. Condensed here:

| ID | What it checks | Proposed for ACTS? |
| --- | --- | --- |
| H01 | Full happy-path approval through the coordinator | No — worked example only |
| H02 | Denial path | No — worked example only |
| H03 | TTL/expiry is application policy, not a protocol timeout | No — worked example only |
| H04 | Late-approval-after-expiry is caller-invisible due to an SDK-layer guard; informs classification only | No — informational, ties to upstream `a2a-python` PR #1182 |
| H05 | Action tampering detected via digest mismatch (approval-content side) | No — worked example only |
| H06 | Duplicate approval does not execute twice in the full HTTP stack — but attributed to the SDK's own terminal-task guard, not `approval_gate.py`'s single-use check specifically (see note above); the focused in-process test proves the app-layer check independently | No — worked example only |
| H07 | Same SDK-layer attribution note as H06, for a post-approval-tampering resubmission; the focused in-process test proves the app-layer digest check independently | No — worked example only |
| H08 | Parent/child `AUTH_REQUIRED` correlation in a delegating multi-agent topology | No — fixture-specific convention (`coordinator.childTaskId`), no standard A2A mechanism exists; framed as opt-in, not `level: must` |
| **H09** | **Fresh client/connection observes current (non-stale) `AUTH_REQUIRED` state via `GetTask`** | **Conceptual follow-up candidate only — not a ready test; see `acts-draft/README.md` for why the draft YAML does not yet exercise a real fresh connection** |
| H10 | Naive retry of the original message must not bypass the gate | No — depends on this fixture's application policy, not protocol normativity |
| H11 | Untrusted-principal claim rejected (mock allowlist, not real auth) | No — worked example only |
| H12 | Out-of-band decision recording vs. in-band resumption-channel outage, kept observably distinct | No — worked example only |

## Why existing ITK/ACTS coverage does not already cover this gap

`scenarios/acts/auth-security.acts.yaml` (read in full as part of this
session's Task 2) contains exactly one `AUTH_REQUIRED`-related test,
`SEC-AUTH-005` ("In-task auth transitions to AUTH_REQUIRED state"). Its
single assertion: sending a `tck-auth-required` trigger message produces a
task in `TASK_STATE_AUTH_REQUIRED` with a non-empty status message. That is
the *entry* transition only. There is no coverage anywhere in the corpus (14
suite files, 113 tests, re-confirmed by reading the full file list and the
`auth-security.acts.yaml` suite this session) for:

- observing that state remains correctly reported to an independent/fresh
  caller while the task is pending (the gap this proposal's draft test
  closes),
- resumption after a decision is recorded,
- the distinction between protocol task state and any application-level
  decision-validation layered on top of it.

This matches the prior finding from `CONTRIBUTION_READY.md` (confirmed
against a fresh clone of `a2a-itk` this session, not merely trusted from an
earlier round) — nothing has changed upstream since.
