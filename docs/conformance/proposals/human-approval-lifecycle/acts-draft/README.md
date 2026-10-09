# Conceptual follow-up candidate: reconnection-observability on a pending `AUTH_REQUIRED` task

**This file is NOT a contribution to `a2a-itk`'s own `scenarios/acts/`, and the
YAML below is NOT conformance-ready.** It is a conceptual candidate for a
possible future `a2aproject/A2A` ACTS test, included here only so the idea and
its rationale travel with this PR. See "Why this is a conceptual candidate,
not a ready test" below before reading the draft YAML.

`a2a-itk`'s ACTS corpus (`scenarios/acts/*.acts.yaml`) is, by this
repository's own stated convention
([`scenarios/acts/PROVENANCE.md`](../../../../../scenarios/acts/PROVENANCE.md),
[`docs/conformance/03_corpus.md`](../../../03_corpus.md)), a **byte-identical
mirror** of `tests/acts/*.acts.yaml` in the separate `a2aproject/A2A`
specification repository. Tests are authored and reviewed there first and
copied here afterward — "the runner never rewrites a document on the way
in," and a hand-added file in `a2a-itk/scenarios/acts/` would not match any
commit of the upstream corpus, which `tests/test_acts_corpus.py` pins and
checks on every refresh.

So: if this idea is ever made executable (see the gap analysis below), its
genuinely correct upstream destination would be `a2aproject/A2A`'s
`tests/acts/auth-security.acts.yaml` (or a new file in that directory), not
this repository. This file sketches that idea in the same YAML dialect as
the existing `auth-security.acts.yaml` suite (the one that already contains
`SEC-AUTH-005`), so a reviewer here does not have to reconstruct it from
prose — not because it is ready to propose there as written.

## Why this one idea

Of the twelve scenarios in the worked example
(`../worked_example/test_client/scenarios.py`, H01-H12), only one —
**H09, reconnection-observability** — is a candidate for a genuinely
portable, SDK-agnostic, protocol-facing check:

> A fresh client/connection calling `GetTask` on a task that is pending in
> `AUTH_REQUIRED` observes the *current* state, not a stale, cached, or
> default one.

This needs nothing from the SUT beyond already supporting `AUTH_REQUIRED`
(which `SEC-AUTH-005` already requires a precondition-gated SUT to exhibit)
and answering `GetTask` observably — both checkable the same way
`SEC-AUTH-005` itself is gated (`requires_behaviors: [tck-auth-required]`).
It makes no assumption about *why* the task is in `AUTH_REQUIRED`, who may
resume it, or what a resume payload looks like — all of which are
application policy, not protocol. (Whether the second request must present
the same authorized identity as the first is a separate, unaddressed
question — see below.)

The other eleven scenarios (H01-H08, H10-H12) encode this fixture's own
application-policy choices — exact action-digest binding, a specific
trusted-principal string, a specific TTL, a specific child-task correlation
convention for multi-agent delegation — and are **not** proposed here as new
ACTS requirements. They remain a worked example
(`../worked_example/README.md`), not conformance assertions against an
arbitrary SUT. See `../README.md` for the full classification.

## Why this is a conceptual candidate, not a ready test

**The YAML below does not actually exercise a fresh connection.** Its two
steps are ordinary `send_message` and `get_task` operations, and this
repository's own ACTS runner (`test_suite/acts/dispatcher/http_base.py`'s
`HttpDispatcher`) owns a single `httpx.AsyncClient` for an entire scenario —
every step in a `.acts.yaml` test, including these two, is dispatched over
that one shared client. There is no `fresh_client` runner capability or step
directive in the current ACTS schema. The YAML's `description:` text on the
second step *claims* "a newly-established connection/client instance with no
shared in-process state," but nothing in the machine-readable `operation`/
`params`/`expect` block enforces that — a runner executing this draft today
would test "does `GetTask` return the current state on a second call,"
**not** "does an independent fresh observer see the current state." Those are
different properties; this draft's name and rationale were about the latter,
but as written it can only check the former.

Concretely, this means: do not treat the YAML below as something that could
be dropped into `a2aproject/A2A`'s `tests/acts/` and merged as-is. Before it
could become a real, executable ACTS test, at least one of the following
would need to happen: (a) the ACTS schema and runner gain a genuine
fresh-client/fresh-connection step directive, or (b) the test is honestly
rescoped to the weaker, already-executable property ("a second `get_task`
call for the same task observes current, non-cached state" — still useful,
but not reconnection coverage, and should not be called that), or (c) some
other mechanism is found to make "no shared in-process state with the
original caller" a property the runner actually enforces rather than asserts
in prose. None of that is resolved here. This file documents the idea and
flags the gap; it does not propose finished test code.

It also does not yet specify that the second request presents the same
authorized identity/principal as the first — a real reconnection test would
need to decide and state whether that matters, which this draft does not
address. A2A v1.0.1 section 3.1.3 (`GetTask` retrieves current state) is the
closest existing normative anchor for the "returns current state" half, but
no citation was found for the reconnection/no-shared-state half specifically,
and sections 7 and 13 (authentication/authorization scoping) are relevant to
the not-yet-addressed identity question above but were not incorporated into
the draft's `expect` block.

## Illustrative draft (not executable as a conformance test today)

```yaml
# NOT a proposed addition to a2aproject/A2A's tests/acts/auth-security.acts.yaml
# as written -- see "Why this is a conceptual candidate, not a ready test"
# above. The `steps:` below only exercise a second `get_task` call over the
# SAME dispatcher-owned client; they do NOT exercise a fresh connection, and
# this illustration deliberately omits `level:` and `spec_ref:` (rather than
# guessing values the real schema would need) because this is not a form
# intended for direct inclusion in the corpus.

      - id: SEC-AUTH-007-concept  # illustrative id only; not reserved
        name: "[CONCEPTUAL, NOT EXECUTABLE] A fresh connection observes the current AUTH_REQUIRED state, not a stale one"
        description: >
          CONCEPTUAL CANDIDATE, NOT A READY TEST (see README above this
          block). Intent: verify that GetTask, called from a new
          client/connection with no shared in-process state with the caller
          that created the task, observes the task's actual current state
          while it is pending in TASK_STATE_AUTH_REQUIRED -- not a cached,
          default, or otherwise stale state. As written below, the `steps:`
          do not actually force a fresh connection (this runner's
          HttpDispatcher reuses one httpx.AsyncClient for the whole
          scenario), so this YAML currently only demonstrates "a second
          GetTask call observes current state," not reconnection. Also
          unaddressed: whether the second request must present the same
          authorized identity as the first (see A2A v1.0.1 sections 7 and
          13 on authentication/authorization scoping).
        # spec_ref intentionally omitted: no normative citation was found
        # specifically for the reconnection/no-shared-state property (see
        # "Open question" below); A2A v1.0.1 section 3.1.3 covers GetTask
        # returning current state in general, but not this specific property.
        # level intentionally omitted: this is not proposed as a must/should/
        # may corpus entry in its current, non-executable form.
        requires_behaviors:
          - "tck-auth-required"
        tags: [auth, task-state, reconnection, conceptual-not-executable]
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
              Query the SAME task id. Intent: the runner MUST perform this
              call over a newly-established connection/client instance with
              no shared in-process state with the caller of the `send` step
              above (e.g. a new HTTP client, a new TCP connection, a fresh
              SDK client object) -- simulating an independent observer
              reconnecting, not the original caller re-reading its own local
              state. As implemented via `operation: get_task` below, this
              runner does NOT actually enforce that -- see the gap analysis
              above.
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

If this idea is ever made executable, it must **not** run unconditionally
against every SUT. The sketch above gates it exactly like `SEC-AUTH-005`:

- `requires_behaviors: [tck-auth-required]` — an SDK that has not declared
  this behavior in its `acts/sut-behaviors.yaml` contract **fails** the test
  (per this runner's own convention: a missing behavior declaration is a
  failure, not a silent skip — see
  [`docs/conformance/02_architecture.md`](../../../02_architecture.md#behaviors)).
  This is deliberate and matches `SEC-AUTH-005`'s own gating exactly; it does
  **not** introduce a new gating mechanism.
- No `preconditions` block beyond the behavior requirement, because
  `AUTH_REQUIRED` support is not a capability advertised on the agent card
  (unlike `streaming` or `pushNotifications`) — the existing corpus already
  treats `tck-auth-required` as the sole gate for `SEC-AUTH-005`, and this
  sketch follows that precedent rather than inventing a new one.
- This idea says nothing about *why* a task enters `AUTH_REQUIRED`, what a
  resume payload looks like, who may resume it, or what invariants govern
  the resumption decision. Those are exactly the application-policy
  questions the worked example (`../worked_example/`) explores, and are
  deliberately NOT part of this sketch's `expect` block.

## Open question for A2A maintainers (not resolved here)

`spec_ref` is intentionally omitted from the illustrative YAML above (see
the inline comment there). A2A v1.0.1 section 3.1.3 specifies that `GetTask`
returns the task's current state, but this proposal has not tracked down a
single normative citation that specifically addresses *reconnection* (a
caller with no shared state/cache observing a pending task's live state, as
opposed to simply "GetTask returns the task"). Rather than inventing or
stretching a citation, this is flagged as an open question for whoever
reviews a future proposal against `a2aproject/A2A`: either an existing
section already covers this and should be cited, or this is a genuine small
specification gap worth noting alongside the test idea itself.

## What was and was not run against this draft

This YAML has **not** been run through ACTS's own loader/runner/dispatcher
(`test_suite/acts/`) in this session. Two independent reasons, not one:
first, doing so would require it to first exist as a real file in
`a2a-itk/scenarios/acts/`, which — per the provenance convention above — is
exactly what should not happen before a test lands upstream in
`a2aproject/A2A`; second, as explained above, the `steps:` as written would
not actually exercise the fresh-connection property even if it were run, so
running it through this repo's dispatcher today would not prove reconnection
semantics regardless.

What was validated instead is the underlying behavior the worked example
demonstrates: see [`../TEST_RESULTS_SUMMARY.md`](../TEST_RESULTS_SUMMARY.md)
for how `scenario_h09` in the worked example's own
`test_client/scenarios.py` — which drives a genuinely separate, freshly
constructed `httpx.AsyncClient` and SDK `Client` object for its second call,
with no shared in-process state with the caller that created the task — was
run against the live, real-SDK worked-example stack and passed. That is
supporting evidence that the underlying *concept* (fresh-observer
reconnection to a pending `AUTH_REQUIRED` task) is sound and reproducible in
a real multi-SDK stack. It is explicitly **not** evidence that the ACTS YAML
draft above would correctly test that same property if merged as-is — it
would not, for the reasons given above, until the gaps in this file are
resolved.
