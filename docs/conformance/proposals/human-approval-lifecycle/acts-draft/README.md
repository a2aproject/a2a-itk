# ACTS draft: reconnection-observability on a pending `AUTH_REQUIRED` task

**This file is NOT a contribution to `a2a-itk`'s own `scenarios/acts/`.**

`a2a-itk`'s ACTS corpus (`scenarios/acts/*.acts.yaml`) is, by this
repository's own stated convention
([`scenarios/acts/PROVENANCE.md`](../../../../../scenarios/acts/PROVENANCE.md),
[`docs/conformance/03_corpus.md`](../../03_corpus.md)), a **byte-identical
mirror** of `tests/acts/*.acts.yaml` in the separate `a2aproject/A2A`
specification repository. Tests are authored and reviewed there first and
copied here afterward — "the runner never rewrites a document on the way
in," and a hand-added file in `a2a-itk/scenarios/acts/` would not match any
commit of the upstream corpus, which `tests/test_acts_corpus.py` pins and
checks on every refresh.

So: the genuinely correct upstream destination for a new ACTS test is
`a2aproject/A2A`'s `tests/acts/auth-security.acts.yaml` (or a new file in
that directory), not this repository. This file is a **draft**, written in
the same YAML dialect as the existing `auth-security.acts.yaml` suite (the
one that already contains `SEC-AUTH-005`), ready to be proposed there. It is
included in this branch so the proposed test's shape and rationale travel
together with the rest of this contribution, and so a reviewer here does not
have to reconstruct it from prose.

## Why this one test

Of the twelve scenarios in the worked example
(`../worked_example/test_client/scenarios.py`, H01-H12), only one —
**H09, reconnection-observability** — is a genuinely portable, SDK-agnostic,
protocol-facing check:

> A fresh client/connection calling `GetTask` on a task that is pending in
> `AUTH_REQUIRED` observes the *current* state, not a stale, cached, or
> default one.

This needs nothing from the SUT beyond already supporting `AUTH_REQUIRED`
(which `SEC-AUTH-005` already requires a precondition-gated SUT to exhibit)
and answering `GetTask` observably — both checkable the same way
`SEC-AUTH-005` itself is gated (`requires_behaviors: [tck-auth-required]`).
It makes no assumption about *why* the task is in `AUTH_REQUIRED`, who may
resume it, or what a resume payload looks like — all of which are
application policy, not protocol.

The other eleven scenarios (H01-H08, H10-H12) encode this fixture's own
application-policy choices — exact action-digest binding, a specific
trusted-principal string, a specific TTL, a specific child-task correlation
convention for multi-agent delegation — and are **not** proposed here as new
ACTS requirements. They remain a worked example
(`../worked_example/README.md`), not conformance assertions against an
arbitrary SUT. See `../README.md` for the full classification.

## Draft test

```yaml
# Proposed addition to a2aproject/A2A's tests/acts/auth-security.acts.yaml,
# immediately following SEC-AUTH-005. Uses the same acts_version/spec_version
# as the rest of the file; spec_ref is left as TODO pending a normative
# citation from the A2A maintainers (see "Open question" below).

      - id: SEC-AUTH-007
        name: "A fresh connection observes the current AUTH_REQUIRED state, not a stale one"
        description: >
          Runner MUST verify that GetTask, called from a new client/connection
          with no shared in-process state with the caller that created the
          task, observes the task's actual current state while it is pending
          in TASK_STATE_AUTH_REQUIRED -- not a cached, default, or otherwise
          stale state. This is the reconnection-observability half of
          AUTH_REQUIRED: SEC-AUTH-005 already verifies that entering the
          state produces the right transition and status message; this test
          verifies that state remains correctly observable to an independent
          caller for as long as the task is pending, which is a distinct
          protocol-facing property not currently covered anywhere in the
          corpus.
        spec_ref: "TODO: cite the normative requirement for GetTask observing
          live, non-cached task state (see 'Open question' below) -- if none
          exists, raise as a spec gap alongside this PR rather than
          inventing a spec_ref"
        level: must
        requires_behaviors:
          - "tck-auth-required"
        tags: [auth, task-state, reconnection]
        steps:
          - id: send
            description: >
              Create a task that enters AUTH_REQUIRED, same trigger as
              SEC-AUTH-005.
            operation: send_message
            params:
              message:
                role: ROLE_USER
                parts:
                  - text: "tck-auth-required trigger auth"
            expect:
              status: 200
              body:
                task:
                  id:
                    type: string
                  status:
                    state: TASK_STATE_AUTH_REQUIRED
                    message:
                      exists: true
            capture:
              taskId: "task.id"

          - id: get-task-fresh-connection
            description: >
              Query the SAME task id, but the runner MUST perform this call
              over a newly-established connection/client instance with no
              shared in-process state with the caller of the `send` step
              above (e.g. a new HTTP client, a new TCP connection, a fresh
              SDK client object) -- simulating an independent observer
              reconnecting, not the original caller re-reading its own local
              state. This is what distinguishes the test from a trivial
              self-consistency check.
            operation: get_task
            params:
              id: "{{send.taskId}}"
            expect:
              status: 200
              body:
                task:
                  id: "{{send.taskId}}"
                  status:
                    state: TASK_STATE_AUTH_REQUIRED
```

## Capability/precondition gating (explicit, per the task brief's own
instruction that this needs it)

This test must **not** run unconditionally against every SUT. It is gated
exactly like `SEC-AUTH-005`:

- `requires_behaviors: [tck-auth-required]` — an SDK that has not declared
  this behavior in its `acts/sut-behaviors.yaml` contract **fails** the test
  (per this runner's own convention: a missing behavior declaration is a
  failure, not a silent skip — see
  [`docs/conformance/02_architecture.md`](../../02_architecture.md#behaviors)).
  This is deliberate and matches `SEC-AUTH-005`'s own gating exactly; it does
  **not** introduce a new gating mechanism.
- No `preconditions` block beyond the behavior requirement, because
  `AUTH_REQUIRED` support is not a capability advertised on the agent card
  (unlike `streaming` or `pushNotifications`) — the existing corpus already
  treats `tck-auth-required` as the sole gate for `SEC-AUTH-005`, and this
  draft follows that precedent rather than inventing a new one.
- This test says nothing about *why* a task enters `AUTH_REQUIRED`, what a
  resume payload looks like, who may resume it, or what invariants govern
  the resumption decision. Those are exactly the application-policy
  questions the worked example (`../worked_example/`) explores, and are
  deliberately NOT part of this draft's `expect` block.

## Open question for A2A maintainers (not resolved here)

`spec_ref` is left as a `TODO` above. A2A's `GetTask` operation is clearly
specified to return the task's current state, but this proposal has not
tracked down a single normative citation that specifically addresses
*reconnection* (a caller with no shared state/cache observing a pending
task's live state, as opposed to simply "GetTask returns the task"). Rather
than inventing or stretching a citation, this is flagged as an open
question for whoever reviews the upstream PR against `a2aproject/A2A`: either
an existing section already covers this and should be cited, or this is a
genuine small specification gap worth noting alongside the test itself.

## What was and was not run against this draft

This YAML has **not** been run through ACTS's own loader/runner/dispatcher
(`test_suite/acts/`) in this session, because doing so would require it to
first exist as a real file in `a2a-itk/scenarios/acts/`, which — per the
provenance convention above — is exactly what should not happen before the
test lands upstream in `a2aproject/A2A`. What was validated instead is the
underlying behavior this draft describes: see
[`../TEST_RESULTS_SUMMARY.md`](../TEST_RESULTS_SUMMARY.md) for how the
equivalent check (`scenario_h09` in the worked example's own
`test_client/scenarios.py`) was run against the live, real-SDK worked-example
stack and passed, which is offered as supporting evidence for this draft's
correctness — explicitly **not** the same thing as a native ACTS harness run.
