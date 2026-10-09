# Proposal: human-approval lifecycle on `AUTH_REQUIRED`

A2A's `AUTH_REQUIRED` task state marks an authorization interruption,
including human approval; `INPUT_REQUIRED` separately supports requests for
additional user input. The protocol spec defines the authorization state and
its entry transition — already covered by `SEC-AUTH-005` in
[`scenarios/acts/auth-security.acts.yaml`](../../../../scenarios/acts/auth-security.acts.yaml)
— but says nothing about what a human-in-the-loop (HIL) authorization step
built on top of it must guarantee, because that is, correctly, an
application-level concern, not a protocol-level one.

This proposal is in two parts, scoped very differently on purpose:

1. **One conceptual follow-up candidate, not a ready test** — observable
   reconnection on a pending `AUTH_REQUIRED` task — sketched in
   [`acts-draft/README.md`](acts-draft/README.md). As currently written it
   does **not** actually exercise a fresh connection (this runner's
   `HttpDispatcher` shares one client across a scenario's steps, and the
   ACTS schema has no fresh-connection directive) — see that file for the
   full gap analysis. It is included so the idea and its rationale travel
   with this PR, not as something ready to propose to `a2aproject/A2A` as-is.
2. **A worked, runnable reference example** —
   [`worked_example/`](worked_example/README.md) — showing one way to build
   the rest of a HIL approval lifecycle (action binding, replay protection,
   principal trust, expiry, retry-safety, channel-outage handling,
   multi-agent propagation) on top of `AUTH_REQUIRED`. This is illustrative,
   not normative, and is not proposed as new ACTS requirements.

**The distinction between those two parts is the actual point of this
proposal.** It is restated below in full because it is the thing most likely
to get lost if only the code or only the YAML is skimmed.

## The normative/policy distinction

| # | Behavior | Classification | Where it is tested/shown |
| --- | --- | --- | --- |
| 1 | A task enters `AUTH_REQUIRED` with a status message when authorization is needed | Normative A2A protocol (already covered) | `SEC-AUTH-005` |
| 2 | A fresh client/connection querying `GetTask` on a pending task observes the *current* state, not a stale/cached/default one | Likely-normative A2A protocol property, **currently uncovered as an executable test** — but not yet expressible as one in this runner's ACTS schema (no fresh-connection directive exists) | `acts-draft/README.md` — a conceptual candidate only, demonstrated instead via the worked example's `scenario_h09` |
| 3 | A delegating parent's task faithfully propagates a child task's `AUTH_REQUIRED`, correlated so an outer caller can observe both | Fixture-specific worked-example design, not a protocol requirement (A2A defines no standard child/delegated-task-id mechanism) | `worked_example/` (H08) |
| 4 | A naive client that retries the original message while `AUTH_REQUIRED` must not have that retry treated as a human decision | Depends on application policy, not protocol normativity | `worked_example/` (H10) |
| 5 | The approved decision must bind to the exact action requested, come from a trusted principal, be single-use, expire, and survive an out-of-band channel outage without the protocol state being misrepresented | Application-policy responsibility — A2A is silent here by design | `worked_example/` (H03, H05-H07, H11, H12) |

Row 2 is the only row identified as a plausible future conformance
requirement — but it is a conceptual candidate, not a ready test (see
`acts-draft/README.md`); it is not being proposed for merge anywhere in its
current form. Everything else is demonstrated as "one correct way to build
this," explicitly **not** as a claim about what A2A or ITK/ACTS requires of
an arbitrary SDK or agent.

## Why existing coverage does not already address this

Confirmed this session by reading
[`scenarios/acts/auth-security.acts.yaml`](../../../../scenarios/acts/auth-security.acts.yaml)
in full against a fresh clone of this repository: it contains exactly one
`AUTH_REQUIRED`-related test (`SEC-AUTH-005`), asserting only the entry
transition. There is no existing coverage for resumption-after-approval,
reconnection-observability, or the distinction between protocol task state
and application-level approval validation. Full detail in
[`TEST_RESULTS_SUMMARY.md`](TEST_RESULTS_SUMMARY.md).

## Related, separate, already-in-flight work (not duplicated here)

Building the worked example surfaced a confirmed `a2a-python` SDK defect —
`TaskManager.save_task_event()` can overwrite a terminal task's status from a
late/stale event with no transition guard — that is already being fixed
upstream in `a2a-python` pull request #1182. That PR is existing, separate,
unmerged work; it is referenced here for context only. This proposal does
not re-propose a fix for it, does not describe this repository as the place
to fix it, and does not depend on it landing. See
[`worked_example/SECURITY_LIMITATIONS.md`](worked_example/SECURITY_LIMITATIONS.md)
for the one paragraph of context this proposal gives it.

## Contents of this directory

| Path | What it is |
| --- | --- |
| `README.md` | This file |
| `acts-draft/README.md` | The one conceptual ACTS test candidate, with a gap analysis of why it's not executable as written and what would need to change before it could be proposed to `a2aproject/A2A` |
| `worked_example/` | The runnable reference example (code + its own README + security limitations) |
| `TEST_RESULTS_SUMMARY.md` | What was actually run, where, and why the ACTS test itself could not be run through this repo's own harness |

## Where this fits in `a2a-itk`'s structure

`a2a-itk` has no `examples/` or `reference-agents/` directory: every SDK's
actual agent code lives in that SDK's own separate repository under `itk/`,
fetched by this repository's launcher at test time. This repository itself
contains only the shared test runner, scenario/ACTS definitions, and docs.
Given that, this proposal's only honest home inside `a2a-itk` is
documentation — which is what `docs/conformance/proposals/` is. The worked
example's code is included for completeness and reviewability, but it is not
claimed to live at a "natural" code location this repository already has,
because no such location exists here.
