"""Focused APPLICATION-POLICY tests for `expense_agent/approval_gate.py`.

These are explicitly DIFFERENT from the full-stack H01-H12 scenario suite
in `scenarios.py` (do not confuse the two layers):

  - H01-H12 (scenarios.py) exercise the REAL A2A HTTP stack end to end
    (Python test client -> TS coordinator -> Python expense agent). For
    H06/H07 specifically, the thing that was actually observed blocking the
    SECOND request was the SDK's own `ActiveTask.start()` terminal-state
    guard (`UnsupportedOperationError: task is in a terminal state`), which
    fires BEFORE the request ever reaches `executor.py` / `approval_gate.py`
    a second time. That is a real, valid, independently-useful SDK-layer
    guard -- but it means `approval_gate.py`'s OWN `used`-flag and
    digest-mismatch checks were never actually exercised end-to-end for a
    race/tamper case that happens WHILE the task is still non-terminal.

  - THIS file closes that gap. It calls `ApprovalGate.execute_protected_submit()`
    directly, in-process, with no A2A/HTTP/executor layer involved at all.
    `execute_protected_submit` never reads or depends on any A2A TaskState
    (by design -- see the module's own docstring and the `ApprovalGate`
    class docstring: "Nothing here imports a2a.types or a2a.server.* for its
    authorization decision"). That means a concurrent call against the same
    challenge_id races PURELY against `approval_gate.py`'s own
    `threading.RLock` + `used` flag / digest check, with no SDK terminal-task
    guard anywhere in the path to "help" -- there is no task object, no
    executor, no SDK admission control involved at all in this file. This is
    the cleanest possible way to prove the application gate's own invariants
    hold on their own merits, independent of (not reinforced by) the SDK
    layer.

  - These are explicitly APPLICATION-POLICY tests, not new A2A protocol
    requirements. No new TaskState values or protocol behavior are
    introduced or asserted anywhere in this file.

Each test is structured to FAIL if the specific invariant under test were
removed or broken in `approval_gate.py` (see the inline comments on each
assertion for exactly which line of `approval_gate.py` the test depends on),
which is how we know the test is not vacuously passing for an unrelated
reason (e.g. an exception elsewhere, or a test that never actually reaches
the code path it claims to).

`approval_gate.py`'s own enforcement logic is NOT modified anywhere in this
file or by this phase of work -- this file only calls into the existing,
unmodified `ApprovalGate` class.

Run directly:
    ../../py_venv/bin/python approval_policy_focused_tests.py
(from the `test_client/` directory; no servers need to be running, since
this talks to `approval_gate.py` in-process.)
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

# Import the real, unmodified application module under test.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "expense_agent"))
import approval_gate as ag  # noqa: E402

EVIDENCE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "evidence", "approval-policy-race-tests.jsonl"
)


def record(test_id: str, expected: str, actual: str, status: str, note: str, detail: dict | None = None) -> None:
    """Same record()-style evidence writer as scenarios.py, writing to a
    SEPARATE evidence file so these application-layer results are never
    merged with the full-stack H01-H12 `scenario-results.jsonl` file."""
    row = {
        "test": test_id,
        "layer": "application-approval-gate (in-process, no A2A/HTTP stack)",
        "expected": expected,
        "actual": actual,
        "status": status,
        "note": note,
        "detail": detail or {},
        "at": time.time(),
    }
    os.makedirs(os.path.dirname(EVIDENCE_PATH), exist_ok=True)
    with open(EVIDENCE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")
    print(f"[{status}] {test_id}: {note}")


def test_replay_safety_concurrent_duplicate_approval() -> bool:
    """P1: Replay safety -- racing duplicate approvals before termination.

    Constructs a fresh ApprovalGate (NOT the module-level singleton `GATE`,
    so this test cannot be polluted by, or pollute, anything else that
    imports `approval_gate`), creates one challenge, records a single
    genuine APPROVED decision for it, then fires TWO threads that both call
    `execute_protected_submit()` for the SAME challenge_id at effectively the
    same instant using a `threading.Barrier` to maximize the actual race
    window (both threads block until both are ready, then both proceed
    immediately). There is no A2A task, no executor, no HTTP server anywhere
    in this path -- the only thing that can decide the outcome is
    `approval_gate.py`'s own `self._lock` (a `threading.RLock`) and the
    `ch.used` flag checked/set inside `execute_protected_submit`
    (approval_gate.py lines ~189 and ~204).

    Asserts:
      - the ledger has EXACTLY ONE entry (not zero, not two)
      - exactly one of the two calls returned executed=True, reason="executed"
      - the OTHER call returned executed=False, reason="challenge_already_used"
        (the exact string literal returned by approval_gate.py's own
        `if ch.used: return False, "challenge_already_used"` check)

    Why this would FAIL if the single-use check were removed/broken: if
    `execute_protected_submit` did not check/set `ch.used` atomically under
    the lock (e.g. if the `if ch.used: return False, ...` line were deleted,
    or if the lock were removed so both threads could interleave between
    reading and setting `ch.used`), both concurrent calls could observe
    `ch.used == False` and both append to `self._ledger`, producing a
    ledger of length 2 and two "executed" results instead of one "executed"
    + one "challenge_already_used". This test's assertions directly check
    for that failure mode (ledger length == 1, exactly one "executed").
    """
    test_id = "APPROVAL-POLICY-01-replay-safety"
    gate = ag.ApprovalGate()  # fresh, isolated instance -- not the module GATE singleton
    action = {"amountMinor": 8000, "currency": "USD", "beneficiary": "race-vendor", "expenseRef": "RACE-001"}
    ch = gate.create_challenge(task_id="race-task-1", context_id="race-ctx-1", action=action)
    gate.record_decision(
        challenge_id=ch.challenge_id,
        decision="APPROVED",
        principal=ag.TRUSTED_PRINCIPAL,
        task_is_canceled=False,
    )

    results: list[tuple[bool, str]] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def attempt():
        # Wait for both threads to be ready, then both proceed at once --
        # this maximizes the actual race window against the SAME
        # challenge_id, entirely within approval_gate.py's own lock, with no
        # SDK/task/executor layer anywhere in this call path.
        barrier.wait()
        outcome = gate.execute_protected_submit(
            challenge_id=ch.challenge_id,
            current_task_state="irrelevant-to-this-gate",  # proves: this param is not even consulted for this invariant
            current_action=action,
        )
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    ledger = gate.ledger_snapshot()
    executed_results = [r for r in results if r[0] is True]
    blocked_results = [r for r in results if r[0] is False]

    ok = (
        len(results) == 2
        and len(ledger) == 1
        and len(executed_results) == 1
        and executed_results[0] == (True, "executed")
        and len(blocked_results) == 1
        and blocked_results[0] == (False, "challenge_already_used")
    )

    record(
        test_id,
        expected="ledger has EXACTLY 1 entry; exactly one concurrent call returns executed=True; "
        "the other returns executed=False, reason='challenge_already_used'",
        actual=f"results={results}, ledger_len={len(ledger)}",
        status="PASS" if ok else "FAIL",
        note="Two threads call GATE.execute_protected_submit() for the SAME challenge_id via a "
        "threading.Barrier to race them against each other, racing PURELY against approval_gate.py's "
        "own lock + used-flag (no A2A task/executor/HTTP layer involved at all in this call path).",
        detail={
            "challenge_id": ch.challenge_id,
            "results": [list(r) for r in results],
            "ledger": ledger,
        },
    )
    return ok


def test_action_binding_under_pre_terminal_tamper() -> bool:
    """P2: Action binding under a pre-terminal tamper.

    A challenge is created for action A ($80 to "vendor-a"). A genuine
    APPROVED decision is recorded for that exact challenge (the decision
    itself is NOT tampered -- this is deliberately distinct from H05, which
    tests "the approval itself claimed different terms"). Then, while the
    challenge is fresh (no task involved at all, so there is no notion of
    'terminal state' in this call path to even race against), an attempt is
    made to execute the protected tool with a DIFFERENT action B ($100 to
    the same vendor) against the SAME challenge_id -- modeling "a legitimate
    approval was granted for X, but the caller tries to get the protected
    effect to fire for Y instead, using the approved challenge_id as its
    ticket."

    Asserts:
      - execute_protected_submit returns executed=False,
        reason="action_digest_mismatch_post_approval_tampering" (the exact
        string literal from approval_gate.py's own
        `if canonical_digest(current_action) != ch.action_digest: return False, "action_digest_mismatch_post_approval_tampering"`
        check)
      - the ledger is completely unaffected (still empty)
      - a SUBSEQUENT, correctly-matched execute_protected_submit call for
        the SAME challenge with the ORIGINAL action A still succeeds
        afterward (proves the tampering attempt did not corrupt or consume
        the challenge's `used` flag -- the digest check runs and rejects
        BEFORE `ch.used` is ever set, per approval_gate.py's own check
        ordering)

    Why this would FAIL if the digest check were removed/broken: if
    `execute_protected_submit` did not call `canonical_digest(current_action)`
    and compare it against `ch.action_digest` (or if that comparison were
    buggy, e.g. comparing the wrong fields or always matching), the tampered
    $100 submission would be allowed to execute, appending an entry to the
    ledger for an action that was never actually approved. This test
    directly checks for that failure mode (ledger stays empty after the
    tamper attempt, and the returned reason string is exactly the
    digest-mismatch one, not "executed").
    """
    test_id = "APPROVAL-POLICY-02-action-binding-tamper"
    gate = ag.ApprovalGate()  # fresh, isolated instance
    approved_action = {"amountMinor": 8000, "currency": "USD", "beneficiary": "vendor-a", "expenseRef": "BIND-001"}
    tampered_action = {"amountMinor": 10000, "currency": "USD", "beneficiary": "vendor-a", "expenseRef": "BIND-001"}
    assert ag.canonical_digest(approved_action) != ag.canonical_digest(tampered_action), (
        "test setup bug: the two actions must actually digest differently"
    )

    ch = gate.create_challenge(task_id="bind-task-1", context_id="bind-ctx-1", action=approved_action)
    gate.record_decision(
        challenge_id=ch.challenge_id,
        decision="APPROVED",
        principal=ag.TRUSTED_PRINCIPAL,
        task_is_canceled=False,
    )

    # The task is still non-terminal in every sense that matters to this
    # call path: there is no A2A Task object at all here, so there is
    # nothing an SDK-layer terminal-state guard could even intercept.
    # Only approval_gate.py's own digest check stands between this tamper
    # attempt and the ledger.
    tamper_executed, tamper_reason = gate.execute_protected_submit(
        challenge_id=ch.challenge_id,
        current_task_state="irrelevant-to-this-gate",
        current_action=tampered_action,
    )
    ledger_after_tamper = gate.ledger_snapshot()

    # Follow-up: the ORIGINAL, correctly-matched action should still be
    # executable afterward -- proving the tamper attempt was rejected
    # before `ch.used` was set (i.e. the digest check runs ahead of the
    # single-use consumption in approval_gate.py's check ordering), not
    # that the challenge was silently consumed/poisoned by the bad attempt.
    genuine_executed, genuine_reason = gate.execute_protected_submit(
        challenge_id=ch.challenge_id,
        current_task_state="irrelevant-to-this-gate",
        current_action=approved_action,
    )
    ledger_after_genuine = gate.ledger_snapshot()

    ok = (
        tamper_executed is False
        and tamper_reason == "action_digest_mismatch_post_approval_tampering"
        and len(ledger_after_tamper) == 0
        and genuine_executed is True
        and genuine_reason == "executed"
        and len(ledger_after_genuine) == 1
        and ledger_after_genuine[0]["action"] == approved_action
    )

    record(
        test_id,
        expected="tampered $100 submission rejected with reason='action_digest_mismatch_post_approval_tampering', "
        "ledger unaffected (0 entries); SAME challenge_id with ORIGINAL $80 action still succeeds afterward "
        "(1 entry, matching the originally-approved action)",
        actual=f"tamper=({tamper_executed!r},{tamper_reason!r}) ledger_after_tamper_len={len(ledger_after_tamper)}; "
        f"genuine=({genuine_executed!r},{genuine_reason!r}) ledger_after_genuine_len={len(ledger_after_genuine)}",
        status="PASS" if ok else "FAIL",
        note="Challenge approved for $80/vendor-a; attempt made to execute_protected_submit with $100/vendor-a "
        "against the SAME challenge_id, with no A2A task/executor/HTTP layer in the path at all (no terminal "
        "state exists to race against here). Then confirms the original $80 action is still honorable afterward.",
        detail={
            "challenge_id": ch.challenge_id,
            "approved_action": approved_action,
            "tampered_action": tampered_action,
            "tamper_result": [tamper_executed, tamper_reason],
            "genuine_result": [genuine_executed, genuine_reason],
            "ledger_after_genuine": ledger_after_genuine,
        },
    )
    return ok


def main() -> int:
    os.makedirs(os.path.dirname(EVIDENCE_PATH), exist_ok=True)
    # Do NOT truncate the shared evidence/ directory's OTHER files (this
    # would be the H01-H12 scenario-results.jsonl's job, not ours) -- only
    # ever append to our OWN, separate evidence file. We do reset our own
    # file at the start of a fresh run so re-running this file doesn't
    # silently accumulate stale results from a previous run.
    if os.path.exists(EVIDENCE_PATH):
        os.remove(EVIDENCE_PATH)

    results = [
        test_replay_safety_concurrent_duplicate_approval(),
        test_action_binding_under_pre_terminal_tamper(),
    ]
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"\n{passed}/{total} focused approval-policy tests passed.")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
