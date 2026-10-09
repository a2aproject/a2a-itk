"""Real A2A AgentExecutor for the Expense Agent, built against the installed
a2a-sdk==1.2.2 package (protobuf-typed a2a.types, TaskUpdater, etc).

Behavioral contract (see BLOCKERS.md for why the server waits on an explicit
"resume" signal rather than blocking inside execute()):

  1. execute() is called once when the client sends the initial expense
     request. It creates the protocol task, moves it WORKING, creates an
     out-of-band ApprovalChallenge via approval_gate.GATE, and publishes
     TASK_STATE_AUTH_REQUIRED via TaskUpdater.requires_auth(). It then
     RETURNS (in-bound auth model, per the SDK's own AgentExecutor
     docstring: "The agent should return from execute(). The framework will
     call execute() again once the user response is received.")

  2. Resuming a task after human approval/denial is itself delegated to the
     A2A protocol: the test client (or coordinator) sends a FOLLOW-UP
     message on the SAME task_id/context_id once a human decision has been
     recorded out-of-band. execute() is invoked again by the SDK framework
     for that follow-up message; at that point this executor checks the
     approval_gate's independently-validated decision and either runs the
     protected tool (H01) or rejects (H02), or verifies the late-arrival
     safety net (H04).

This keeps "A2A task state" (TaskState enum transitions only) strictly
separate from "application approval state" (ApprovalChallenge fields),
per the task brief's requirement #3.

APPROVAL-AUTHORITY BOUNDARY (read before touching `_handle_resume`)
====================================================================
This module does NOT, and must NOT, ever call `GATE.record_decision()`.
That call is reserved exclusively for the separate, out-of-band
`/__human__/decision` HTTP endpoint in `server.py`, which stands in for a
real human-approval UI/bot acting outside the A2A protocol surface
entirely.

An earlier version of this fixture had `_handle_resume` call
`GATE.record_decision(..., principal="human-auditor", ...)` directly, as
part of handling the A2A `__RESUME_APPROVED__` resume token. That was a
layering bug: it meant the mere act of an A2A caller SENDING that resume
message was treated as proof a trusted human had approved the action --
there was no separate channel actually simulating a human decision at all,
just a hardcoded principal string asserted by the A2A-facing code itself.

The corrected flow is:
  1. A human-approval-simulator client (the test client, standing in for a
     human/approval-UI) calls `/__human__/decision` DIRECTLY over HTTP --
     NOT through any A2A message -- to record a decision against a
     challenge_id. This is the only call site for `record_decision()`.
  2. The A2A resume message (`__RESUME_APPROVED__` / `__RESUME_DENIED__`
     / etc) no longer ASSERTS a decision. It means "check whether a
     decision has been recorded yet, and act on it if so." This executor
     reads `challenge.decision` (via `GATE.get_challenge_for_task()`,
     already populated out-of-band if a decision exists) and proceeds
     based on THAT -- it never records one itself.
  3. If no decision has been recorded yet when a resume message arrives,
     that is now an honestly-observable "no decision yet" outcome: the
     task stays in AUTH_REQUIRED (or is rejected, depending on which
     resume token was used -- see `_handle_resume` below), exactly as it
     should for an A2A caller that has not actually obtained a human
     decision through the real out-of-band channel.

This is a pure refactor of WHERE/HOW `record_decision()` gets called from.
`approval_gate.py`'s own enforcement invariants (digest match, single-use,
expiry, trusted-principal-string check, all inside
`execute_protected_submit`) are completely unmodified and are still
independently enforced exactly as before.

NON-PRODUCTION / SIMULATOR-ONLY: see `approval_gate.py`'s module docstring.
No real principal authentication or independent human-identity assertion
is demonstrated anywhere in this fixture.
"""

from __future__ import annotations

import logging

from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types.a2a_pb2 import Part, Task, TaskState, TaskStatus

from approval_gate import GATE, canonical_digest

logger = logging.getLogger("expense_agent.executor")


def _text_part(text: str) -> Part:
    return Part(text=text)


def _data_part(data: dict, media_type: str = "application/json") -> Part:
    from google.protobuf.struct_pb2 import Value
    from google.protobuf.json_format import ParseDict

    value = Value()
    ParseDict(data, value)
    return Part(data=value, media_type=media_type)


RESUME_APPROVED = "__RESUME_APPROVED__"
RESUME_DENIED = "__RESUME_DENIED__"
RESUME_LATE = "__RESUME_LATE__"

# --- New resume-token conventions added for H05/H06/H07/H10/H11 -----------
# These EXTEND the existing in-bound-auth resume convention above; H01/H02/
# H04/H08/H09's tokens and code paths are untouched.
#
# H05: a human decision arrives claiming approval, but the action attached
# to that decision does NOT match the action the original challenge was
# created for (i.e. the approval itself was for different content than was
# actually requested/challenged). We deliberately do NOT update
# challenge.action to match -- that would fabricate consistency that never
# existed. The mismatch must be caught by execute_protected_submit's own
# independent digest check.
RESUME_APPROVED_MODIFIED_PREFIX = "__RESUME_APPROVED_MODIFIED__"

# H11: a decision arrives claiming APPROVED, but from a principal other than
# GATE.TRUSTED_PRINCIPAL. Lets the test client parameterize the principal.
RESUME_APPROVED_AS_PREFIX = "__RESUME_APPROVED_AS__"

# H07: models "the action changed after a genuine approval was already
# recorded" -- distinct angle from H05. The test client first sends a
# normal RESUME_APPROVED (which legitimately executes the protected tool
# once), then sends this token with a DIFFERENT action, attempting to reuse
# the same (now-used) challenge for a changed submission. This must be
# independently blocked -- either by the single-use `used` flag (if it fires
# first) or by the digest mismatch check; both are real invariants in
# approval_gate.py and this scenario intentionally exercises whichever one
# applies given the already-used challenge.
RESUME_RESUBMIT_MODIFIED_PREFIX = "__RESUME_RESUBMIT_MODIFIED__"


class ExpenseAgentExecutor(AgentExecutor):
    """Expense reimbursement agent with a mandatory human-approval gate."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        user_text = context.get_user_input()
        existing_task = context.current_task

        if existing_task is None:
            # Every streaming turn MUST begin with a Task or Message event
            # (SDK-enforced ordering contract). Publish the initial Task
            # snapshot ourselves before any TaskStatusUpdateEvent.
            initial_task = Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                history=[context.message] if context.message else [],
            )
            await event_queue.enqueue_event(initial_task)

            # --- First turn: new expense request. ---
            await self._handle_new_request(context, updater, user_text)
            return

        # --- Follow-up turn on an existing task. ---
        state = existing_task.status.state
        logger.info(
            "execute() follow-up for task=%s current_state=%s input=%r",
            context.task_id,
            TaskState.Name(state),
            user_text,
        )

        stripped = user_text.strip()
        is_resume_token = (
            stripped == RESUME_APPROVED
            or stripped == RESUME_DENIED
            or stripped.startswith(RESUME_LATE)
            or stripped.startswith(RESUME_APPROVED_MODIFIED_PREFIX)
            or stripped.startswith(RESUME_APPROVED_AS_PREFIX)
            or stripped.startswith(RESUME_RESUBMIT_MODIFIED_PREFIX)
        )
        if is_resume_token:
            await self._handle_resume(context, updater, state, stripped)
            return

        # Unknown follow-up message on a task already in AUTH_REQUIRED or
        # elsewhere: application policy is to reject rather than silently
        # resurrect/ignore. (No protocol state is invented; REJECTED is a
        # real TaskState value.)
        if state not in (
            TaskState.TASK_STATE_COMPLETED,
            TaskState.TASK_STATE_CANCELED,
            TaskState.TASK_STATE_FAILED,
            TaskState.TASK_STATE_REJECTED,
        ):
            await updater.reject(
                message=updater.new_agent_message(
                    parts=[_text_part("Unrecognized follow-up for an expense task awaiting approval.")]
                )
            )

    async def _handle_new_request(
        self, context: RequestContext, updater: TaskUpdater, user_text: str
    ) -> None:
        import json

        try:
            payload = json.loads(user_text) if user_text.strip().startswith("{") else {}
        except json.JSONDecodeError:
            payload = {}

        amount_minor = payload.get("amountMinor", 15000)
        currency = payload.get("currency", "USD")
        beneficiary = payload.get("beneficiary", "synthetic-vendor-co")
        expense_ref = payload.get("expenseRef", "SYNTH-001")

        if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0 or amount_minor > 10_000_000:
            await updater.start_work()
            await updater.reject(
                message=updater.new_agent_message(
                    parts=[_text_part("amountMinor must be a positive integer under the policy bound.")]
                )
            )
            return
        if currency != "USD" or not beneficiary:
            await updater.start_work()
            await updater.reject(
                message=updater.new_agent_message(
                    parts=[_text_part("currency must be USD and beneficiary is required.")]
                )
            )
            return

        action = {
            "operation": "submit_reimbursement",
            "amountMinor": amount_minor,
            "currency": currency,
            "beneficiary": str(beneficiary),
            "expenseRef": str(expense_ref),
        }

        await updater.start_work(
            message=updater.new_agent_message(
                parts=[_text_part("Expense prepared; no protected tool effect has occurred yet.")]
            )
        )

        challenge = GATE.create_challenge(context.task_id, context.context_id, action)

        await updater.requires_auth(
            message=updater.new_agent_message(
                parts=[
                    _text_part("Human consent for exact expense terms required."),
                    _data_part(
                        {
                            "schema": "https://example.invalid/hil-approval-real-sdk/v1",
                            "challengeId": challenge.challenge_id,
                            "expenseTaskId": context.task_id,
                            "action": action,
                            "actionDigest": challenge.action_digest,
                            "intendedApprover": "human-auditor",
                        }
                    ),
                ]
            )
        )
        logger.info(
            "task=%s challenge=%s action_digest=%s -> AUTH_REQUIRED (execute() returning, in-bound auth model)",
            context.task_id,
            challenge.challenge_id,
            challenge.action_digest,
        )
        # Return from execute(): in-bound auth model per AgentExecutor docstring.
        # The framework will invoke execute() again on the next message/send
        # for this task_id (approval, denial, or late/no-op message).

    async def _handle_resume(
        self,
        context: RequestContext,
        updater: TaskUpdater,
        current_state: "TaskState.ValueType",
        resume_token: str,
    ) -> None:
        """Handle an A2A follow-up ("resume") message on a task awaiting
        human approval.

        IMPORTANT (see module docstring "APPROVAL-AUTHORITY BOUNDARY"): this
        method NEVER calls `GATE.record_decision()`. A human decision, if
        one has actually been made, was already recorded out-of-band via a
        direct call to the `/__human__/decision` HTTP endpoint in
        `server.py` -- the only place that call is allowed to happen. All
        this method does is RETRIEVE the current state of the challenge
        (`GATE.get_challenge_for_task`) and act on whatever decision (if
        any) is already recorded there. If no decision has been recorded
        yet, that is an honest "no decision yet" outcome, not something
        this method may paper over by asserting one itself.
        """
        challenge = GATE.get_challenge_for_task(context.task_id)
        if challenge is None:
            await updater.failed(
                message=updater.new_agent_message(
                    parts=[_text_part("No approval challenge found for this task.")]
                )
            )
            return

        # H12: the human decision was ALREADY recorded out-of-band. The
        # synthetic outage (GATE.set_channel_down(True) via /__test__/channel)
        # prevents this A2A resume/check-in path from acting on that decision.
        # We MUST NOT fabricate a protocol transition: returning without
        # calling TaskUpdater leaves the task in AUTH_REQUIRED. The recorded
        # decision is preserved for a subsequent check-in after recovery.
        # This applies to genuine decision tokens only (APPROVED/DENIED and
        # their H05/H07/H11 variants) -- RESUME_LATE's own terminal-state
        # short-circuit above-and-below is unaffected.
        if GATE.is_channel_down() and not resume_token.startswith(RESUME_LATE):
            logger.warning(
                "task=%s A2A decision-check path simulated DOWN; recorded decision deferred, task state unchanged",
                context.task_id,
            )
            return

        # --- No decision recorded yet? ---------------------------------
        # Under the corrected flow, a resume message is just "check in on
        # whether a decision has landed," not an assertion of one. If the
        # test client sent a resume token WITHOUT first calling
        # `/__human__/decision` (simulating an A2A caller checking in before
        # any real human has acted), there is genuinely nothing to act on
        # yet. This is a MORE honest outcome than the old behavior (which
        # would have fabricated an APPROVED decision right here). We leave
        # the task in AUTH_REQUIRED -- the truthful observable state -- by
        # returning without any TaskUpdater call, mirroring the H12
        # channel-down short-circuit above. RESUME_LATE's own terminal-state
        # handling below still runs first in that branch (it only concerns
        # whether the TASK is terminal, not whether a decision was recorded).
        if (
            challenge.decision is None
            and not resume_token.startswith(RESUME_LATE)
            and not resume_token.startswith(RESUME_RESUBMIT_MODIFIED_PREFIX)
        ):
            logger.info(
                "task=%s resume token %r received but no human decision has been recorded yet "
                "via /__human__/decision -- task remains AUTH_REQUIRED (honest no-op)",
                context.task_id,
                resume_token,
            )
            return

        if resume_token.startswith(RESUME_LATE):
            # H04: a late approval/denial decision arrives AFTER this task may
            # already have been canceled by the client. The caller (test
            # client / coordinator) is simulating the out-of-band channel
            # delivering a decision after cancellation. We must NOT let this
            # resurrect a CANCELED task. The SDK's TaskUpdater enforces
            # terminal-state immutability (raises RuntimeError) -- but this
            # executor's job is to behave correctly even when invoked, so we
            # check current_state ourselves first and never attempt a
            # transition out of a terminal state.
            if current_state in (
                TaskState.TASK_STATE_CANCELED,
                TaskState.TASK_STATE_COMPLETED,
                TaskState.TASK_STATE_FAILED,
                TaskState.TASK_STATE_REJECTED,
            ):
                logger.info(
                    "task=%s late decision ignored: task already terminal (%s)",
                    context.task_id,
                    TaskState.Name(current_state),
                )
                # Do NOT call protected tool. Do NOT attempt any status
                # update (TaskUpdater would raise on a terminal task anyway;
                # this executor does not rely on that exception as its
                # safety mechanism -- see SDK_COMPARISON.md).
                return
            # Task not terminal yet somehow -- treat as a normal decision.
            resume_token = resume_token[len(RESUME_LATE):] or RESUME_APPROVED

        if resume_token.startswith(RESUME_APPROVED_MODIFIED_PREFIX):
            # H05: a genuine APPROVED decision from the trusted principal
            # (the "human" really did click approve, via a prior call to
            # /__human__/decision -- the test client is expected to have
            # made that call before sending this resume token) but the
            # action attached to that approval differs from the action the
            # challenge was actually created for. challenge.action is left
            # untouched -- only the value passed as current_action to
            # execute_protected_submit differs, so the mismatch is real.
            import json as _json

            raw = resume_token[len(RESUME_APPROVED_MODIFIED_PREFIX):]
            try:
                claimed_action = _json.loads(raw)
            except Exception:  # noqa: BLE001
                claimed_action = {}

            executed, reason = GATE.execute_protected_submit(
                challenge.challenge_id,
                current_task_state=TaskState.Name(current_state),
                current_action=claimed_action,
            )
            if not executed:
                logger.warning(
                    "task=%s H05 protected tool correctly refused modified-approval-content: %s",
                    context.task_id,
                    reason,
                )
                await updater.reject(
                    message=updater.new_agent_message(
                        parts=[_text_part(f"Protected tool refused to execute: {reason}")]
                    )
                )
                return
            # Should never happen if the gate's invariants are intact; if it
            # does, this is a real finding, not something to paper over.
            await updater.complete(
                message=updater.new_agent_message(
                    parts=[_text_part("UNEXPECTED: modified-content approval was executed.")]
                )
            )
            return

        if resume_token.startswith(RESUME_APPROVED_AS_PREFIX):
            # H11: a decision claiming APPROVED was recorded (via a prior
            # call to /__human__/decision, which the test client is expected
            # to have made) from a principal OTHER than GATE.TRUSTED_PRINCIPAL.
            # This is a MOCK PRINCIPAL-CLAIM ALLOWLIST check, not real
            # authentication: /__human__/decision performs no cryptographic
            # or session-based identity verification whatsoever -- it simply
            # accepts whatever `principal` string the caller supplies. This
            # test verifies only that execute_protected_submit's allowlist
            # comparison (decided_by != TRUSTED_PRINCIPAL) correctly refuses
            # a claim that does not match the single allowlisted string.
            principal = challenge.decided_by or "untrusted-principal"
            executed, reason = GATE.execute_protected_submit(
                challenge.challenge_id,
                current_task_state=TaskState.Name(current_state),
                current_action=challenge.action,
            )
            if not executed:
                logger.warning(
                    "task=%s H11 protected tool correctly refused untrusted principal-claim %r "
                    "(mock allowlist check, not real authentication): %s",
                    context.task_id,
                    principal,
                    reason,
                )
                await updater.reject(
                    message=updater.new_agent_message(
                        parts=[_text_part(f"Protected tool refused to execute: {reason}")]
                    )
                )
                return
            await updater.complete(
                message=updater.new_agent_message(
                    parts=[_text_part("UNEXPECTED: untrusted-principal approval was executed.")]
                )
            )
            return

        if resume_token.startswith(RESUME_RESUBMIT_MODIFIED_PREFIX):
            # H07: the action is claimed to have changed AFTER a genuine
            # approval was already recorded/consumed. The test client is
            # expected to have already sent a normal RESUME_APPROVED for
            # this task (which legitimately executed the protected tool
            # once); this token then attempts to resubmit the SAME challenge
            # with a different action. Whichever of the gate's own
            # independent invariants applies (single-use `used`, or digest
            # mismatch) must block it -- we do not special-case which one.
            import json as _json

            raw = resume_token[len(RESUME_RESUBMIT_MODIFIED_PREFIX):]
            try:
                changed_action = _json.loads(raw)
            except Exception:  # noqa: BLE001
                changed_action = {}

            executed, reason = GATE.execute_protected_submit(
                challenge.challenge_id,
                current_task_state=TaskState.Name(current_state),
                current_action=changed_action,
            )
            if not executed:
                logger.warning(
                    "task=%s H07 protected tool correctly refused post-approval action change: %s",
                    context.task_id,
                    reason,
                )
                await updater.reject(
                    message=updater.new_agent_message(
                        parts=[_text_part(f"Protected tool refused to execute: {reason}")]
                    )
                )
                return
            await updater.complete(
                message=updater.new_agent_message(
                    parts=[_text_part("UNEXPECTED: post-approval action change was executed.")]
                )
            )
            return

        if resume_token == RESUME_DENIED:
            # A DENIED decision was already recorded out-of-band via a prior
            # call to /__human__/decision. This resume message just checks
            # in on that recorded outcome and reflects it into A2A protocol
            # state (REJECTED).
            await updater.reject(
                message=updater.new_agent_message(
                    parts=[_text_part(f"Expense approval denied by {challenge.decided_by}.")]
                )
            )
            return

        # RESUME_APPROVED: an APPROVED decision was already recorded
        # out-of-band via a prior call to /__human__/decision (the only
        # place GATE.record_decision() is ever invoked). This resume message
        # does not assert approval itself -- it retrieves the
        # already-recorded decision (via `challenge` above) and attempts the
        # protected tool call, which independently re-validates every
        # invariant (decision value, trusted principal, digest, expiry,
        # single-use) regardless of what this resume token claims.
        executed, reason = GATE.execute_protected_submit(
            challenge.challenge_id,
            current_task_state=TaskState.Name(current_state),
            current_action=challenge.action,
        )

        if not executed:
            logger.warning("task=%s protected tool refused: %s", context.task_id, reason)
            await updater.failed(
                message=updater.new_agent_message(
                    parts=[_text_part(f"Protected tool refused to execute: {reason}")]
                )
            )
            return

        await updater.add_artifact(
            parts=[_data_part({"ledgerEntry": "written", "challengeId": challenge.challenge_id})],
            name="reimbursement-receipt",
        )
        await updater.complete(
            message=updater.new_agent_message(
                parts=[_text_part("Reimbursement submitted exactly once.")]
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel(
            message=updater.new_agent_message(
                parts=[_text_part("Task canceled by client request.")]
            )
        )
