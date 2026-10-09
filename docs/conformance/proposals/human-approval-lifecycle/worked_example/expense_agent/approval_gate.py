"""Application-level human approval gate + protected mock expense tool.

This module is DELIBERATELY NOT an A2A protocol concept. It models:
  - the out-of-band "human approval authority" that a real system would put
    behind some separate channel (Slack approval, a ticketing system, etc).
  - the protected mock expense ledger / tool that must never fire without an
    independently-validated authorization.

Architectural separation from A2A:
  - Nothing here imports a2a.types or a2a.server.* for its authorization
    decision. The approval gate's invariants (challenge validity, single use,
    exact action-digest match, correct principal, non-expired, and a
    simulator-supplied canceled-at-decision-time flag) are checked using
    plain Python data. This gate does NOT independently query A2A task state
    from the task store; the fixture tests SDK task-state handling separately.
  - The expense agent's AgentExecutor calls into this module, but this
    module does not call back into A2A machinery. This keeps "the task is in
    AUTH_REQUIRED" (protocol state) separate from "this specific challenge
    has been validated" (application state), per the task brief's
    requirement #3.

HTTP endpoints exposing this gate are mounted under `/__human__/*` on the
SAME expense-agent FastAPI app, but are clearly marked as NOT A2A: they are
plain non-A2A-versioned JSON endpoints that a human-approval UI/bot would
call. (Convention ported from agent04_hil/expense_agent.py's `/__human__/*`
prefix -- logic rewritten fresh against real invariants, not copy-pasted.)

NON-PRODUCTION / SIMULATOR-ONLY NOTICE
=======================================
`record_decision()` is the ONLY place a human decision is ever recorded, and
it must ONLY ever be called from the separate `/__human__/decision` HTTP
endpoint in `server.py` -- i.e. from a call that stands in for a human
approval UI/bot acting out-of-band, never from `executor.py`'s A2A
message-handling code. `executor.py`'s resume handling only ever READS an
already-recorded decision via `get_challenge_for_task()`; it must never call
`record_decision()` itself. (See `executor.py`'s own module docstring for
the full before/after of this separation.)

Even with that wiring correct, this fixture demonstrates NO real principal
authentication and NO independent human-identity assertion anywhere. The
"trusted human-auditor principal" (`TRUSTED_PRINCIPAL` below) is a single
hardcoded, allowlisted string compared with `==`. There is no session,
token, certificate, or cryptographic signature backing it: whichever HTTP
caller supplies that exact string as the `principal` field is treated as
"the human," full stop. This must NEVER be mistaken for, or presented as,
a real authorization/authentication system. It is a synthetic simulator
built to test the APPLICATION-LEVEL invariants around a human-approval
step (digest binding, single-use, expiry, principal allowlisting) in
isolation from any real identity system. A production implementation would
replace the out-of-band channel with a genuine authenticated
human-approval mechanism (e.g. a signed callback from an internal
approvals UI, OAuth-authenticated human session, etc) -- building that is
explicitly OUT OF SCOPE here; see H11 in `executor.py`/`TEST_RESULTS.md`
for how this is now framed as a mock principal-claim allowlist check, not
as "unauthorized principal detection" in any real-auth sense.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


def canonical_digest(action: dict[str, Any]) -> str:
    """Stable SHA-256 digest of an action payload, used for exact-match checks."""
    blob = json.dumps(action, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


TRUSTED_PRINCIPAL = "human-auditor"  # the one synthetic principal allowed to approve
CHALLENGE_TTL_SECONDS = 300


@dataclass
class ApprovalChallenge:
    challenge_id: str
    task_id: str
    context_id: str
    action: dict[str, Any]
    action_digest: str
    created_at: float
    expires_at: float
    # Application-level outcome tracking, entirely separate from A2A TaskState.
    decision: str | None = None  # None | "APPROVED" | "DENIED"
    decided_by: str | None = None
    decided_at: float | None = None
    used: bool = False  # single-use: the protected tool may consume this exactly once
    task_canceled_at_decision_time: bool = False


class ApprovalGate:
    """In-memory out-of-band approval authority + protected ledger tool.

    Thread-safe (the SDK server runs in an asyncio loop in one process, but we
    still guard with a lock since the FastAPI app and any polling thread could
    both touch this).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._challenges: dict[str, ApprovalChallenge] = {}
        self._ledger: list[dict[str, Any]] = []
        # A pluggable clock lets tests simulate "late approval after expiry"
        # deterministically instead of racing real wall-clock time.
        self._clock = time.time
        # H12 fault injection: models an outage in the A2A resumption /
        # recorded-decision check path AFTER an out-of-band decision exists.
        # When True, the executor cannot act on the recorded decision and
        # leaves the task AUTH_REQUIRED. The separate /__human__/decision
        # endpoint is not blocked by this synthetic fault flag.
        self._channel_down = False

    def set_clock(self, clock) -> None:
        self._clock = clock

    def set_channel_down(self, down: bool) -> None:
        with self._lock:
            self._channel_down = down

    def is_channel_down(self) -> bool:
        with self._lock:
            return self._channel_down

    def create_challenge(self, task_id: str, context_id: str, action: dict[str, Any]) -> ApprovalChallenge:
        now = self._clock()
        ch = ApprovalChallenge(
            challenge_id="challenge-" + str(uuid.uuid4()),
            task_id=task_id,
            context_id=context_id,
            action=action,
            action_digest=canonical_digest(action),
            created_at=now,
            expires_at=now + CHALLENGE_TTL_SECONDS,
        )
        with self._lock:
            self._challenges[ch.challenge_id] = ch
        return ch

    def get_challenge(self, challenge_id: str) -> ApprovalChallenge | None:
        with self._lock:
            return self._challenges.get(challenge_id)

    def get_challenge_for_task(self, task_id: str) -> ApprovalChallenge | None:
        with self._lock:
            for ch in self._challenges.values():
                if ch.task_id == task_id:
                    return ch
        return None

    def record_decision(
        self,
        challenge_id: str,
        decision: str,
        principal: str,
        task_is_canceled: bool,
    ) -> ApprovalChallenge | None:
        """Records a human decision. This does NOT execute the protected tool --
        it only stores the out-of-band decision. The protected tool call is a
        separate step, with its own independent validation (see
        `execute_protected_submit`).
        """
        with self._lock:
            ch = self._challenges.get(challenge_id)
            if ch is None:
                return None
            # Out-of-band decisions can arrive at any time (that's the whole
            # point of H04). We record what we observed, including whether the
            # A2A task was already canceled at decision time, so the later
            # protected-tool call can independently refuse to act on it.
            ch.decision = decision
            ch.decided_by = principal
            ch.decided_at = self._clock()
            ch.task_canceled_at_decision_time = task_is_canceled
            return ch

    def ledger_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._ledger]

    def execute_protected_submit(
        self,
        challenge_id: str,
        current_task_state: str,
        current_action: dict[str, Any],
    ) -> tuple[bool, str]:
        """The protected mock expense tool.

        Independently validates EVERY invariant needed before allowing the
        ledger write, regardless of what any convenience bookkeeping (e.g. an
        AgentExecutor's local variable) might claim. Returns (executed, reason).

        Invariants checked (ported conceptually from agent04_hil's synthetic
        fixture, not copied):
          1. challenge exists
          2. challenge has a recorded APPROVED decision
          3. decision was made by the trusted principal
          4. challenge not expired (checked against current gate clock)
          5. challenge not already used (single-use / no replay)
          6. the action being submitted now digests identically to the
             action that was actually approved (no post-approval tampering)
          7. the A2A task was not already canceled at decision time in a way
             that governs this challenge (defense in depth; this check does
             NOT rely on fetching current protocol task state here, since
             that could itself be subject to the TaskManager-overwrite bug
             under investigation in H04 -- it relies on what was captured at
             decision time, stored as application data above).
        """
        with self._lock:
            ch = self._challenges.get(challenge_id)
            if ch is None:
                return False, "no_such_challenge"
            if ch.used:
                return False, "challenge_already_used"
            if ch.decision != "APPROVED":
                return False, f"not_approved(decision={ch.decision!r})"
            if ch.decided_by != TRUSTED_PRINCIPAL:
                return False, "untrusted_principal"
            now = self._clock()
            if now > ch.expires_at:
                return False, "challenge_expired"
            if ch.task_canceled_at_decision_time:
                return False, "task_was_canceled_at_decision_time"
            if canonical_digest(current_action) != ch.action_digest:
                return False, "action_digest_mismatch_post_approval_tampering"

            # All independent checks passed -- execute exactly once.
            ch.used = True
            effect = {
                "taskId": ch.task_id,
                "challengeId": ch.challenge_id,
                "actionDigest": ch.action_digest,
                "action": dict(ch.action),
                "executedAt": now,
            }
            self._ledger.append(effect)
            return True, "executed"


GATE = ApprovalGate()
