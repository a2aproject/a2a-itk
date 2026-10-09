"""Scenario driver for the real-SDK HIL integration.

Test Client -> Coordinator (TS, @a2a-js/sdk) -> Expense Agent (Python, a2a-sdk)

Uses the real a2a-sdk==1.2.2 Python client (A2ACardResolver / create_client)
against the coordinator's REST interface (same transport binding the
coordinator itself uses to call the expense agent), per task brief's "be
consistent" instruction.

Each scenario appends raw results to evidence/scenario-results.jsonl and
prints a PASS/FAIL line. This file does not fabricate any SDK responses --
every assertion is made against objects actually returned by the real
client calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid

import httpx

from a2a.client import create_client
from a2a.client.client import ClientConfig
from a2a.types.a2a_pb2 import (
    CancelTaskRequest,
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    TaskState,
)
from a2a.utils.constants import TransportProtocol

COORDINATOR_URL = os.environ.get("COORDINATOR_URL", "http://127.0.0.1:8301")
EXPENSE_URL = os.environ.get("EXPENSE_URL", "http://127.0.0.1:8201")
RESULTS_PATH = os.path.join(os.path.dirname(__file__), "..", "evidence", "scenario-results.jsonl")


def record(scenario_id: str, expected: str, actual: str, status: str, note: str, detail: dict | None = None) -> None:
    row = {
        "scenario": scenario_id,
        "expected": expected,
        "actual": actual,
        "status": status,
        "note": note,
        "detail": detail or {},
        "at": time.time(),
    }
    os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)
    with open(RESULTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")
    print(f"[{status}] {scenario_id}: {note}")
    if status != "PASS":
        raise AssertionError(f"{scenario_id} failed: {actual}")


def make_message(text: str, task_id: str | None = None, context_id: str | None = None) -> Message:
    return Message(
        message_id="msg-" + str(uuid.uuid4()),
        role=Role.ROLE_USER,
        parts=[Part(text=text)],
        task_id=task_id or "",
        context_id=context_id or "",
    )


async def new_client(httpx_client: httpx.AsyncClient, base_url: str = COORDINATOR_URL):
    config = ClientConfig(
        httpx_client=httpx_client,
        supported_protocol_bindings=[TransportProtocol.HTTP_JSON],
        streaming=False,
    )
    return await create_client(base_url, client_config=config)


def expense_payload(amount=15000, currency="USD", beneficiary="acme", expense_ref=None):
    return json.dumps(
        {
            "amountMinor": amount,
            "currency": currency,
            "beneficiary": beneficiary,
            "expenseRef": expense_ref or ("SYNTH-" + str(uuid.uuid4())[:8]),
        }
    )


def child_task_id_from(task) -> str | None:
    """Extracts the coordinator-assigned child (expense-agent) task_id from
    a parent (coordinator) task's status message metadata. The challenge
    recorded by approval_gate.py lives on the CHILD task, since the
    coordinator mints its own distinct parent task_id -- so any scenario
    that talks to the coordinator (i.e. uses the default COORDINATOR_URL
    client, which is all of H01/H02/H04/H05/H06/H07/H10/H11/H12) must
    resolve this before calling record_human_decision(), which operates
    against the expense agent's /__human__/* surface directly."""
    meta = dict(task.status.message.metadata) if task.status.HasField("message") else {}
    return meta.get("coordinator.childTaskId")


def record_human_decision(
    expense_task_id: str,
    decision: str,
    principal: str = "human-auditor",
    task_is_canceled: bool = False,
    expense_url: str = None,
) -> dict:
    """Calls the out-of-band `/__human__/decision` endpoint DIRECTLY over
    HTTP -- simulating a human-approval-simulator client (a human/approval
    UI stand-in), NOT an A2A message. This is the only way a decision is
    ever recorded under the corrected flow (see executor.py's module
    docstring "APPROVAL-AUTHORITY BOUNDARY"). Looks up the challenge_id via
    `/__human__/pending/{expense_task_id}` first (this is also a GET against
    the same NOT-A2A `/__human__/*` surface, not an A2A call).

    `expense_task_id` must be the CHILD (expense-agent) task_id -- the
    challenge lives on the expense agent, not the coordinator's parent task
    -- which is why callers going through the coordinator (H04 et al) must
    first resolve the child task id (see scenario_h08's pattern for how the
    child id is obtained from the status message's metadata)."""
    url = expense_url or EXPENSE_URL
    pending = httpx.get(f"{url}/__human__/pending/{expense_task_id}").json()
    challenge_id = pending.get("challengeId")
    resp = httpx.post(
        f"{url}/__human__/decision",
        json={
            "challengeId": challenge_id,
            "decision": decision,
            "principal": principal,
            "taskIsCanceled": task_is_canceled,
        },
    )
    resp.raise_for_status()
    return resp.json()


async def send_and_collect(client, text, task_id=None, context_id=None):
    req = SendMessageRequest(
        message=make_message(text, task_id=task_id, context_id=context_id),
        configuration=SendMessageConfiguration(return_immediately=False),
    )
    last_task = None
    async for resp in client.send_message(req):
        if resp.HasField("task"):
            last_task = resp.task
    return last_task


async def scenario_h01(httpx_client: httpx.AsyncClient) -> None:
    sid = "H01"
    client = await new_client(httpx_client)
    try:
        task = await send_and_collect(client, expense_payload())
        assert task is not None, "no parent task returned"
        parent_task_id = task.id
        state1 = TaskState.Name(task.status.state)

        got = await client.get_task(GetTaskRequest(id=parent_task_id))
        state1_confirmed = TaskState.Name(got.status.state)

        # Out-of-band step: the human-approval-simulator client calls
        # /__human__/decision DIRECTLY (not via A2A) against the CHILD
        # (expense-agent) task_id to record APPROVED, simulating a
        # human/approval-UI acting outside the protocol surface entirely.
        # Only after a decision is actually recorded does the resume
        # message mean anything.
        child_task_id = child_task_id_from(task)
        assert child_task_id, "H01 requires the coordinator's child-task correlation metadata"
        record_human_decision(child_task_id, "APPROVED")

        resume_task = await send_and_collect(client, "__RESUME_APPROVED__", task_id=parent_task_id, context_id=task.context_id)
        final_state = TaskState.Name(resume_task.status.state) if resume_task else None
        if final_state is None:
            got2 = await client.get_task(GetTaskRequest(id=parent_task_id))
            final_state = TaskState.Name(got2.status.state)

        ledger = httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"]

        ok = (
            state1 == "TASK_STATE_AUTH_REQUIRED"
            and state1_confirmed == "TASK_STATE_AUTH_REQUIRED"
            and final_state == "TASK_STATE_COMPLETED"
            and len(ledger) == 1
        )
        record(
            sid,
            expected="AUTH_REQUIRED -> human approves (via /__human__/decision, out-of-band) -> resume checks in -> COMPLETED, ledger has exactly 1 entry",
            actual=f"first_state={state1}, confirmed={state1_confirmed}, final_state={final_state}, ledger_len={len(ledger)}",
            status="PASS" if ok else "FAIL",
            note="Full happy path through coordinator with real SDK events.",
            detail={"parent_task_id": parent_task_id},
        )
    finally:
        await client.close()


async def scenario_h02(httpx_client: httpx.AsyncClient) -> None:
    sid = "H02"
    client = await new_client(httpx_client)
    try:
        task = await send_and_collect(client, expense_payload(amount=20000, beneficiary="denied-vendor"))
        parent_task_id = task.id
        state1 = TaskState.Name(task.status.state)

        ledger_before = len(httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"])

        child_task_id = child_task_id_from(task)
        assert child_task_id, "H02 requires the coordinator's child-task correlation metadata"
        record_human_decision(child_task_id, "DENIED")

        resume_task = await send_and_collect(client, "__RESUME_DENIED__", task_id=parent_task_id, context_id=task.context_id)
        final_state = TaskState.Name(resume_task.status.state) if resume_task else None
        if final_state is None:
            got2 = await client.get_task(GetTaskRequest(id=parent_task_id))
            final_state = TaskState.Name(got2.status.state)

        ledger_after = len(httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"])

        ok = (
            state1 == "TASK_STATE_AUTH_REQUIRED"
            and final_state == "TASK_STATE_REJECTED"
            and ledger_after == ledger_before
        )
        record(
            sid,
            expected="AUTH_REQUIRED -> human denies (via /__human__/decision, out-of-band) -> REJECTED, ledger unchanged, protected tool never runs",
            actual=f"first_state={state1}, final_state={final_state}, ledger_before={ledger_before}, ledger_after={ledger_after}",
            status="PASS" if ok else "FAIL",
            note="Denial path through coordinator with real SDK events.",
            detail={"parent_task_id": parent_task_id},
        )
    finally:
        await client.close()


async def scenario_h04(httpx_client: httpx.AsyncClient, label: str = "H04") -> dict:
    """Approval arrives after cancellation. Returns a result dict for the
    caller (also used for the cross-SDK-version comparison in Step 4)."""
    client = await new_client(httpx_client)
    try:
        task = await send_and_collect(client, expense_payload(amount=30000, beneficiary="late-approval-vendor"))
        parent_task_id = task.id
        state1 = TaskState.Name(task.status.state)
        ledger_before = len(httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"])

        # This scenario goes THROUGH the coordinator, which mints its own
        # child task_id on the expense agent distinct from the parent
        # (coordinator) task_id -- the challenge/ledger live on the CHILD
        # task. Resolve the child task_id from the status message's
        # correlation metadata, same pattern as scenario_h08.
        child_task_id = child_task_id_from(task)
        assert child_task_id, "H04 requires the coordinator's child-task correlation metadata"

        # Client cancels the PARENT (coordinator) task. The coordinator's
        # cancelTask forwards this to the child expense-agent task.
        canceled_task = await client.cancel_task(CancelTaskRequest(id=parent_task_id))
        canceled_state = TaskState.Name(canceled_task.status.state)

        # LATE approval decision arrives after cancellation: the human-
        # approval-simulator client calls /__human__/decision DIRECTLY
        # (out-of-band, not via A2A) against the CHILD task_id, with
        # taskIsCanceled=True -- genuinely modeling a human decision that
        # physically arrives after the task was already canceled. This
        # actually exercises approval_gate.py's own
        # `task_canceled_at_decision_time` invariant inside
        # execute_protected_submit, rather than merely observing "no
        # decision was ever recorded" (a different, weaker condition).
        record_human_decision(child_task_id, "APPROVED", task_is_canceled=True)

        # Resume message (sent to the PARENT/coordinator task_id, as before)
        # simply checks in on that already-recorded decision; the
        # coordinator forwards it to the (now canceled) child task.
        late_effect_observed = False
        late_error = None
        try:
            late_task = await send_and_collect(
                client, "__RESUME_APPROVED__", task_id=parent_task_id, context_id=task.context_id
            )
            late_state = TaskState.Name(late_task.status.state) if late_task else None
        except Exception as e:  # noqa: BLE001
            late_task = None
            late_state = None
            late_error = str(e)

        # Caller-visible check #1: does get_task (via coordinator) ever show
        # the parent task un-canceled?
        got_after = await client.get_task(GetTaskRequest(id=parent_task_id))
        got_after_state = TaskState.Name(got_after.status.state)
        parent_resurrected = got_after_state not in ("TASK_STATE_CANCELED",)

        # Caller-visible check #2: did the protected tool execute a second
        # time (ledger grew) as a result of the late approval?
        ledger_after = len(httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"])
        tool_executed_from_late_approval = ledger_after > ledger_before

        ok = (
            state1 == "TASK_STATE_AUTH_REQUIRED"
            and canceled_state == "TASK_STATE_CANCELED"
            and not parent_resurrected
            and not tool_executed_from_late_approval
        )

        result = {
            "scenario": label,
            "parent_task_id": parent_task_id,
            "state1": state1,
            "canceled_state": canceled_state,
            "late_state": late_state,
            "late_error": late_error,
            "got_after_state": got_after_state,
            "parent_resurrected": parent_resurrected,
            "ledger_before": ledger_before,
            "ledger_after": ledger_after,
            "tool_executed_from_late_approval": tool_executed_from_late_approval,
            "ok": ok,
        }
        record(
            label,
            expected="Late approval after cancellation cannot resurrect task or re-trigger protected tool",
            actual=json.dumps(result),
            status="PASS" if ok else "FAIL",
            note="Caller-visible check via get_task + ledger size, through full real-SDK stack.",
            detail=result,
        )
        return result
    finally:
        await client.close()


async def scenario_h08(httpx_client: httpx.AsyncClient) -> None:
    sid = "H08"
    client = await new_client(httpx_client)
    try:
        task = await send_and_collect(client, expense_payload(amount=12300, beneficiary="nested-corr-vendor"))
        parent_task_id = task.id
        parent_context_id = task.context_id
        parent_state = TaskState.Name(task.status.state)

        # Read the propagated status message's metadata for child correlation
        # (coordinator attaches coordinator.childTaskId/childContextId).
        meta = dict(task.status.message.metadata) if task.status.HasField("message") else {}
        child_task_id = meta.get("coordinator.childTaskId")
        child_context_id = meta.get("coordinator.childContextId")

        # Independently verify the child task really exists on the expense
        # agent and is ALSO AUTH_REQUIRED (not silently absorbed/misrepresented).
        child_pending = httpx.get(f"{EXPENSE_URL}/__human__/pending/{child_task_id}").json() if child_task_id else {"found": False}

        ok = (
            parent_state == "TASK_STATE_AUTH_REQUIRED"
            and bool(child_task_id)
            and child_task_id != parent_task_id
            and bool(child_context_id)
            and child_context_id != parent_context_id
            and child_pending.get("found") is True
        )
        record(
            sid,
            expected="Parent (coordinator) task AUTH_REQUIRED correlates to distinct child (expense) task, also AUTH_REQUIRED",
            actual=f"parent_state={parent_state}, child_task_id={child_task_id}, child_pending_found={child_pending.get('found')}",
            status="PASS" if ok else "FAIL",
            note="Verifies parent/child correlation + faithful AUTH_REQUIRED propagation, not misrepresented.",
            detail={"parent_task_id": parent_task_id, "child_task_id": child_task_id},
        )
    finally:
        await client.close()


async def scenario_h09(httpx_client: httpx.AsyncClient) -> None:
    sid = "H09"
    client_a = await new_client(httpx_client)
    try:
        task = await send_and_collect(client_a, expense_payload(amount=9900, beneficiary="reconnect-vendor"))
        parent_task_id = task.id
        state_a = TaskState.Name(task.status.state)

        # Fresh client instance simulating a reconnect (new httpx.AsyncClient,
        # new A2A client -- not reusing client_a's connection/session state).
        async with httpx.AsyncClient() as fresh_httpx:
            client_b = await new_client(fresh_httpx)
            try:
                got = await client_b.get_task(GetTaskRequest(id=parent_task_id))
                state_b = TaskState.Name(got.status.state)
            finally:
                await client_b.close()

        ok = state_a == "TASK_STATE_AUTH_REQUIRED" and state_b == "TASK_STATE_AUTH_REQUIRED"
        record(
            sid,
            expected="Fresh client reconnecting via get_task observes current AUTH_REQUIRED state, not stale/default data",
            actual=f"state_from_original_client={state_a}, state_from_fresh_client={state_b}",
            status="PASS" if ok else "FAIL",
            note="New A2A client/connection queries same task_id while pending.",
            detail={"parent_task_id": parent_task_id},
        )
    finally:
        await client_a.close()


async def scenario_h05(httpx_client: httpx.AsyncClient) -> None:
    """H05 -- Approval content modified: the human decision claims approval
    for a DIFFERENT amount than the challenge was actually created for. The
    original challenge's action digest must not match the claimed-approved
    action, so execute_protected_submit must refuse
    (action_digest_mismatch_post_approval_tampering), and the protected tool
    must never fire with the wrong amount."""
    sid = "H05"
    client = await new_client(httpx_client)
    try:
        task = await send_and_collect(client, expense_payload(amount=8000, beneficiary="h05-vendor"))
        parent_task_id = task.id
        state1 = TaskState.Name(task.status.state)
        ledger_before = len(httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"])

        # The human genuinely approves (out-of-band, via /__human__/decision)
        # -- the decision itself is NOT tampered. The tamper happens in what
        # the resume message CLAIMS the approved action was (see
        # modified_action below), which execute_protected_submit's own
        # digest check must independently catch.
        child_task_id = child_task_id_from(task)
        assert child_task_id, "H05 requires the coordinator's child-task correlation metadata"
        record_human_decision(child_task_id, "APPROVED")

        modified_action = json.dumps(
            {
                "operation": "submit_reimbursement",
                "amountMinor": 10000,  # claimed-approved amount != 8000 actually challenged
                "currency": "USD",
                "beneficiary": "h05-vendor",
                "expenseRef": "SYNTH-H05-TAMPERED",
            }
        )
        resume_task = await send_and_collect(
            client,
            "__RESUME_APPROVED_MODIFIED__" + modified_action,
            task_id=parent_task_id,
            context_id=task.context_id,
        )
        final_state = TaskState.Name(resume_task.status.state) if resume_task else None
        if final_state is None:
            got2 = await client.get_task(GetTaskRequest(id=parent_task_id))
            final_state = TaskState.Name(got2.status.state)

        ledger_after = len(httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"])

        ok = (
            state1 == "TASK_STATE_AUTH_REQUIRED"
            and final_state == "TASK_STATE_REJECTED"
            and ledger_after == ledger_before
        )
        record(
            sid,
            expected="Approval claims terms different from the challenged action -> digest mismatch, REJECTED, no ledger effect",
            actual=f"first_state={state1}, final_state={final_state}, ledger_before={ledger_before}, ledger_after={ledger_after}",
            status="PASS" if ok else "FAIL",
            note="Approval itself was for different content than requested (action_digest_mismatch_post_approval_tampering).",
            detail={"parent_task_id": parent_task_id},
        )
    finally:
        await client.close()


async def scenario_h06(httpx_client: httpx.AsyncClient) -> None:
    """H06 -- Duplicate approval response: the SAME approval decision is sent
    twice for the same task/challenge. The protected tool must fire at most
    once (single-use `used` flag); the second attempt must be independently
    blocked by challenge_already_used."""
    sid = "H06"
    client = await new_client(httpx_client)
    try:
        task = await send_and_collect(client, expense_payload(amount=5500, beneficiary="h06-vendor"))
        parent_task_id = task.id
        context_id = task.context_id
        state1 = TaskState.Name(task.status.state)
        ledger_before = len(httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"])

        # A SINGLE genuine out-of-band decision is recorded. Both resend
        # attempts below are just the resume message checking in on that
        # one recorded decision twice -- the duplicate-resend protection
        # (single-use `used` flag / SDK terminal-task guard) is what this
        # scenario actually tests, not a double-recording of the decision.
        child_task_id = child_task_id_from(task)
        assert child_task_id, "H06 requires the coordinator's child-task correlation metadata"
        record_human_decision(child_task_id, "APPROVED")

        first = await send_and_collect(client, "__RESUME_APPROVED__", task_id=parent_task_id, context_id=context_id)
        first_state = TaskState.Name(first.status.state) if first else None
        ledger_after_first = len(httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"])

        # The duplicate resend targets a task that is now already terminal
        # (COMPLETED) after the first approval. The real SDK's own
        # request-handler layer independently refuses to re-admit a message
        # for a terminal task (UnsupportedOperationError raised client-side
        # as an HTTP error), which is itself a legitimate, SDK-level
        # manifestation of "duplicate decision has no further effect" -- in
        # addition to (not instead of) approval_gate.py's own `used` flag,
        # which would otherwise independently block it if the SDK layer
        # didn't. Either outcome is acceptable here: what matters is the
        # ledger does not grow a second time.
        second_state = None
        second_error = None
        try:
            second = await send_and_collect(client, "__RESUME_APPROVED__", task_id=parent_task_id, context_id=context_id)
            second_state = TaskState.Name(second.status.state) if second else None
        except Exception as e:  # noqa: BLE001
            second_error = str(e)
        if second_state is None and second_error is None:
            got2 = await client.get_task(GetTaskRequest(id=parent_task_id))
            second_state = TaskState.Name(got2.status.state)
        ledger_after_second = len(httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"])

        duplicate_blocked = (second_error is not None) or (
            second_state in ("TASK_STATE_FAILED", "TASK_STATE_REJECTED", "TASK_STATE_COMPLETED")
        )
        ok = (
            state1 == "TASK_STATE_AUTH_REQUIRED"
            and first_state == "TASK_STATE_COMPLETED"
            and ledger_after_first == ledger_before + 1
            and duplicate_blocked
            and ledger_after_second == ledger_after_first
        )
        record(
            sid,
            expected="First approval executes exactly once; duplicate resend does not grow the ledger again",
            actual=(
                f"first_state={first_state}, ledger_after_first={ledger_after_first}, "
                f"second_state={second_state}, second_error={second_error!r}, ledger_after_second={ledger_after_second}"
            ),
            status="PASS" if ok else "FAIL",
            note=(
                "Exercises ApprovalChallenge.used single-use flag (challenge_already_used), reinforced here by "
                "the SDK's own terminal-task guard rejecting the duplicate resend outright before the executor "
                "even runs again -- defense in depth, same pattern as H04's late-approval probe."
            ),
            detail={"parent_task_id": parent_task_id},
        )
    finally:
        await client.close()


async def scenario_h07(httpx_client: httpx.AsyncClient) -> None:
    """H07 -- Requested action changes AFTER a genuine approval was already
    recorded/consumed: a buggy or malicious resubmission tries to reuse the
    same (already-used) challenge with different terms. Distinct from H05
    (where the approval itself was for mismatched content): here the FIRST
    approval is entirely legitimate and matches the original challenge; the
    attack/bug happens on a SECOND attempt against the same challenge_id."""
    sid = "H07"
    client = await new_client(httpx_client)
    try:
        task = await send_and_collect(client, expense_payload(amount=4200, beneficiary="h07-vendor"))
        parent_task_id = task.id
        context_id = task.context_id
        state1 = TaskState.Name(task.status.state)
        ledger_before = len(httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"])

        # Genuine out-of-band decision recorded first.
        child_task_id = child_task_id_from(task)
        assert child_task_id, "H07 requires the coordinator's child-task correlation metadata"
        record_human_decision(child_task_id, "APPROVED")

        # Genuine, correctly-matched approval -- legitimately executes once.
        approved = await send_and_collect(client, "__RESUME_APPROVED__", task_id=parent_task_id, context_id=context_id)
        approved_state = TaskState.Name(approved.status.state) if approved else None
        ledger_after_genuine = len(httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"])

        # Now something tries to resubmit the SAME challenge with CHANGED
        # terms, claiming the same prior approval still applies.
        changed_action = json.dumps(
            {
                "operation": "submit_reimbursement",
                "amountMinor": 999900,
                "currency": "USD",
                "beneficiary": "h07-vendor-CHANGED",
                "expenseRef": "SYNTH-H07-CHANGED",
            }
        )
        # The resubmission also targets an already-terminal (COMPLETED) task,
        # so the real SDK's own request-handler layer may refuse it outright
        # (UnsupportedOperationError), same as H06/H04's pattern -- which is
        # itself a legitimate, defense-in-depth way for "post-approval action
        # change against the same challenge" to be blocked, in addition to
        # approval_gate.py's own digest-mismatch/single-use checks.
        resubmit_state = None
        resubmit_error = None
        try:
            resubmit = await send_and_collect(
                client,
                "__RESUME_RESUBMIT_MODIFIED__" + changed_action,
                task_id=parent_task_id,
                context_id=context_id,
            )
            resubmit_state = TaskState.Name(resubmit.status.state) if resubmit else None
        except Exception as e:  # noqa: BLE001
            resubmit_error = str(e)
        if resubmit_state is None and resubmit_error is None:
            got_r = await client.get_task(GetTaskRequest(id=parent_task_id))
            resubmit_state = TaskState.Name(got_r.status.state)
        ledger_after_resubmit = len(httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"])

        resubmit_blocked = (resubmit_error is not None) or (
            resubmit_state in ("TASK_STATE_FAILED", "TASK_STATE_REJECTED", "TASK_STATE_COMPLETED")
        )
        ok = (
            state1 == "TASK_STATE_AUTH_REQUIRED"
            and approved_state == "TASK_STATE_COMPLETED"
            and ledger_after_genuine == ledger_before + 1
            and resubmit_blocked
            and ledger_after_resubmit == ledger_after_genuine
        )
        record(
            sid,
            expected="Genuine approval executes once; post-approval resubmission with changed terms against same challenge is blocked, no second ledger effect",
            actual=(
                f"approved_state={approved_state}, ledger_after_genuine={ledger_after_genuine}, "
                f"resubmit_state={resubmit_state}, resubmit_error={resubmit_error!r}, ledger_after_resubmit={ledger_after_resubmit}"
            ),
            status="PASS" if ok else "FAIL",
            note="The requested action changed AFTER a genuine approval was already recorded -- exercises digest-mismatch/single-use jointly, from the 'action changed post-approval' angle (vs H05's 'approval itself was for different content').",
            detail={"parent_task_id": parent_task_id},
        )
    finally:
        await client.close()


async def scenario_h11(httpx_client: httpx.AsyncClient) -> None:
    """H11 -- Mock principal-claim allowlist check (NOT real authentication):
    a decision is recorded via the out-of-band /__human__/decision endpoint
    claiming APPROVED, but with a `principal` string other than
    GATE.TRUSTED_PRINCIPAL ("human-auditor"). /__human__/decision performs
    NO cryptographic or session-based identity verification whatsoever --
    it accepts whatever principal string the caller supplies; that is
    exactly what this test demonstrates. The only check being verified here
    is execute_protected_submit's allowlist comparison (decided_by !=
    TRUSTED_PRINCIPAL), which correctly refuses a claim that does not match
    the single allowlisted string. This must NOT be read as "unauthorized
    principal detection" in any real-authentication sense -- see
    approval_gate.py's module docstring NON-PRODUCTION notice."""
    sid = "H11"
    client = await new_client(httpx_client)
    try:
        task = await send_and_collect(client, expense_payload(amount=6700, beneficiary="h11-vendor"))
        parent_task_id = task.id
        state1 = TaskState.Name(task.status.state)
        ledger_before = len(httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"])

        # Out-of-band: a caller claims to be "random-user" (not the
        # allowlisted TRUSTED_PRINCIPAL) and records an APPROVED decision.
        # /__human__/decision accepts this with no verification at all --
        # the mock-allowlist point this scenario makes.
        child_task_id = child_task_id_from(task)
        assert child_task_id, "H11 requires the coordinator's child-task correlation metadata"
        record_human_decision(child_task_id, "APPROVED", principal="random-user")

        resume_task = await send_and_collect(
            client,
            "__RESUME_APPROVED_AS__random-user",
            task_id=parent_task_id,
            context_id=task.context_id,
        )
        final_state = TaskState.Name(resume_task.status.state) if resume_task else None
        if final_state is None:
            got2 = await client.get_task(GetTaskRequest(id=parent_task_id))
            final_state = TaskState.Name(got2.status.state)

        ledger_after = len(httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"])

        ok = (
            state1 == "TASK_STATE_AUTH_REQUIRED"
            and final_state == "TASK_STATE_REJECTED"
            and ledger_after == ledger_before
        )
        record(
            sid,
            expected=(
                "Mock principal-claim allowlist check (not real authentication): a decision recorded "
                "out-of-band claiming APPROVED from 'random-user' (not the allowlisted TRUSTED_PRINCIPAL) "
                "-> execute_protected_submit's allowlist comparison rejects it, no ledger effect"
            ),
            actual=f"first_state={state1}, final_state={final_state}, ledger_before={ledger_before}, ledger_after={ledger_after}",
            status="PASS" if ok else "FAIL",
            note=(
                "Exercises execute_protected_submit's untrusted_principal check (decided_by != TRUSTED_PRINCIPAL). "
                "This is a MOCK allowlist check: /__human__/decision performs no cryptographic or session-based "
                "identity verification -- it is a single hardcoded string compared with '=='. See "
                "approval_gate.py's module docstring NON-PRODUCTION notice."
            ),
            detail={"parent_task_id": parent_task_id},
        )
    finally:
        await client.close()


async def scenario_h10(httpx_client: httpx.AsyncClient) -> None:
    """H10 -- Client incorrectly treats waiting as automatic retry: a naive
    client resends the identical original expense request message on the
    SAME task_id/context_id while the task is in AUTH_REQUIRED. This must
    NOT bypass the human gate, NOT execute the protected tool, and NOT
    silently complete the task."""
    sid = "H10"
    client = await new_client(httpx_client)
    try:
        original_text = expense_payload(amount=3300, beneficiary="h10-vendor")
        task = await send_and_collect(client, original_text)
        parent_task_id = task.id
        context_id = task.context_id
        state1 = TaskState.Name(task.status.state)
        ledger_before = len(httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"])

        # Naive retry: resend the SAME original request text on the SAME
        # task/context, as a client that doesn't understand AUTH_REQUIRED
        # means "blocked on human input" might do.
        retry_task = await send_and_collect(client, original_text, task_id=parent_task_id, context_id=context_id)
        retry_state = TaskState.Name(retry_task.status.state) if retry_task else None
        if retry_state is None:
            got2 = await client.get_task(GetTaskRequest(id=parent_task_id))
            retry_state = TaskState.Name(got2.status.state)

        ledger_after = len(httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"])

        # Acceptable outcomes: task remains AUTH_REQUIRED (ignored/no-op), or
        # an explicit non-success terminal state (REJECTED/FAILED). What is
        # NOT acceptable: COMPLETED (silent bypass of the human gate) or any
        # ledger growth.
        ok = (
            state1 == "TASK_STATE_AUTH_REQUIRED"
            and retry_state in ("TASK_STATE_AUTH_REQUIRED", "TASK_STATE_REJECTED", "TASK_STATE_FAILED")
            and ledger_after == ledger_before
        )
        record(
            sid,
            expected="Naive retry of the original request while AUTH_REQUIRED does not bypass the gate, execute the tool, or silently complete",
            actual=f"first_state={state1}, retry_state={retry_state}, ledger_before={ledger_before}, ledger_after={ledger_after}",
            status="PASS" if ok else "FAIL",
            note="AUTH_REQUIRED means blocked-on-human-input; a client retry is not a human decision and must not be treated as one.",
            detail={"parent_task_id": parent_task_id},
        )
    finally:
        await client.close()


async def scenario_h03(httpx_client: httpx.AsyncClient) -> None:
    """H03 -- No human response / approval expires. Nothing happens for
    longer than CHALLENGE_TTL_SECONDS. This is an APPLICATION-POLICY test,
    not an A2A protocol one: A2A itself defines no task-expiry behavior.
    Verify: (a) the task remains observably AUTH_REQUIRED via get_task while
    no decision has been made, even well past the TTL -- it must NOT
    auto-transition on its own; (b) only once an explicit resume message is
    sent (modeling the application's own expiry-check policy, which is
    exercised inside execute_protected_submit) does the task move to a
    terminal state; (c) the protected tool never fires."""
    sid = "H03"
    client = await new_client(httpx_client)
    try:
        task = await send_and_collect(client, expense_payload(amount=7100, beneficiary="h03-vendor"))
        parent_task_id = task.id
        state1 = TaskState.Name(task.status.state)
        ledger_before = len(httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"])

        # Confirm via get_task, BEFORE advancing the clock, that nothing
        # auto-transitions merely from the passage of real time either.
        got_before = await client.get_task(GetTaskRequest(id=parent_task_id))
        state_before_advance = TaskState.Name(got_before.status.state)

        # Advance the gate's simulated clock past CHALLENGE_TTL_SECONDS (300s)
        # via the test-only endpoint -- deterministic, no real sleep needed.
        adv = httpx.post(f"{EXPENSE_URL}/__test__/advance_clock", json={"seconds": 301})
        adv.raise_for_status()

        # The task MUST still be observably AUTH_REQUIRED purely from time
        # having passed -- A2A defines no autonomous task-expiry transition.
        got_after_advance = await client.get_task(GetTaskRequest(id=parent_task_id))
        state_after_advance_no_decision = TaskState.Name(got_after_advance.status.state)

        # Only now does an explicit decision arrive (simulating the human
        # auditor finally trying to approve, long after the window, via the
        # out-of-band /__human__/decision endpoint). The application's own
        # expiry policy inside execute_protected_submit independently finds
        # the challenge expired and refuses, moving the task to a real
        # terminal TaskState (not a fabricated "expired" one).
        child_task_id = child_task_id_from(task)
        assert child_task_id, "H03 requires the coordinator's child-task correlation metadata"
        record_human_decision(child_task_id, "APPROVED")

        resume_task = await send_and_collect(client, "__RESUME_APPROVED__", task_id=parent_task_id, context_id=task.context_id)
        final_state = TaskState.Name(resume_task.status.state) if resume_task else None
        if final_state is None:
            got_final = await client.get_task(GetTaskRequest(id=parent_task_id))
            final_state = TaskState.Name(got_final.status.state)

        ledger_after = len(httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"])

        ok = (
            state1 == "TASK_STATE_AUTH_REQUIRED"
            and state_before_advance == "TASK_STATE_AUTH_REQUIRED"
            and state_after_advance_no_decision == "TASK_STATE_AUTH_REQUIRED"
            and final_state in ("TASK_STATE_REJECTED", "TASK_STATE_FAILED")
            and ledger_after == ledger_before
        )
        record(
            sid,
            expected=(
                "No auto-transition from passage of time alone (stays AUTH_REQUIRED past TTL); "
                "only an explicit application-policy expiry decision moves it to a terminal state; "
                "protected tool never fires"
            ),
            actual=(
                f"state1={state1}, state_before_advance={state_before_advance}, "
                f"state_after_advance_no_decision={state_after_advance_no_decision}, "
                f"final_state={final_state}, ledger_before={ledger_before}, ledger_after={ledger_after}"
            ),
            status="PASS" if ok else "FAIL",
            note=(
                "APPLICATION-POLICY test, not an A2A protocol one: A2A/TaskState defines no autonomous "
                "expiry transition. GATE.set_clock (via /__test__/advance_clock) used for deterministic "
                "TTL simulation instead of racing real wall-clock time."
            ),
            detail={"parent_task_id": parent_task_id},
        )
    finally:
        await client.close()
        # Reset the gate's clock to real time so subsequent scenarios in the
        # same server process are not affected by this scenario's time travel.
        httpx.post(f"{EXPENSE_URL}/__test__/advance_clock", json={"seconds": 0})


async def scenario_h12(httpx_client: httpx.AsyncClient) -> None:
    """H12 -- Approval channel failure: the human's decision is recorded
    out-of-band (via /__human__/decision) BEFORE the simulated outage
    begins. The outage models the IN-BAND A2A resume/check-in path (the
    executor's ability to look up and act on that already-recorded
    decision) becoming unreachable -- NOT a failed HTTP request to record
    the human's original decision in the first place. This is distinct
    from H03's "nothing ever arrives" (no decision exists at all) and
    deliberately does not model the out-of-band recording channel itself
    failing. Verify the task remains observably AUTH_REQUIRED (not
    silently COMPLETED or a fabricated state) and the protected tool does
    not fire while the channel is down; then confirm normal operation
    resumes once the channel is restored."""
    sid = "H12"
    client = await new_client(httpx_client)
    try:
        task = await send_and_collect(client, expense_payload(amount=2200, beneficiary="h12-vendor"))
        parent_task_id = task.id
        context_id = task.context_id
        state1 = TaskState.Name(task.status.state)
        ledger_before = len(httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"])

        # The human genuinely approves out-of-band (via /__human__/decision)
        # BEFORE the channel outage begins -- modeling that the decision
        # itself was already made; what's down is the IN-BAND A2A resume
        # path's ability to check in on / act on that decision, not the
        # out-of-band recording channel itself (these are deliberately
        # separate channels in this fixture -- see approval_gate.py).
        child_task_id = child_task_id_from(task)
        assert child_task_id, "H12 requires the coordinator's child-task correlation metadata"
        record_human_decision(child_task_id, "APPROVED")

        down_resp = httpx.post(f"{EXPENSE_URL}/__test__/channel", json={"down": True})
        down_resp.raise_for_status()

        # Attempt to resume (check in on the already-recorded decision)
        # while the channel is simulated down. The executor must not
        # fabricate a protocol transition -- execute() returns without
        # publishing any status update, so the SDK's send_message will
        # observe whatever the task's last known state was (still
        # AUTH_REQUIRED), not a new terminal state.
        during_outage_task = await send_and_collect(client, "__RESUME_APPROVED__", task_id=parent_task_id, context_id=context_id)
        state_during_outage = TaskState.Name(during_outage_task.status.state) if during_outage_task else None
        if state_during_outage is None:
            got_outage = await client.get_task(GetTaskRequest(id=parent_task_id))
            state_during_outage = TaskState.Name(got_outage.status.state)

        ledger_during_outage = len(httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"])

        # Independently confirm via get_task that the task is still
        # observable and in AUTH_REQUIRED, not misrepresented.
        got_after_outage_attempt = await client.get_task(GetTaskRequest(id=parent_task_id))
        state_confirmed = TaskState.Name(got_after_outage_attempt.status.state)

        # Restore the channel and confirm normal operation resumes (the
        # outage was transient, not a permanent corruption of the task).
        up_resp = httpx.post(f"{EXPENSE_URL}/__test__/channel", json={"down": False})
        up_resp.raise_for_status()

        recovered_task = await send_and_collect(client, "__RESUME_APPROVED__", task_id=parent_task_id, context_id=context_id)
        recovered_state = TaskState.Name(recovered_task.status.state) if recovered_task else None
        ledger_after_recovery = len(httpx.get(f"{EXPENSE_URL}/__human__/ledger").json()["ledger"])

        ok = (
            state1 == "TASK_STATE_AUTH_REQUIRED"
            and state_during_outage == "TASK_STATE_AUTH_REQUIRED"
            and state_confirmed == "TASK_STATE_AUTH_REQUIRED"
            and ledger_during_outage == ledger_before
            and recovered_state == "TASK_STATE_COMPLETED"
            and ledger_after_recovery == ledger_before + 1
        )
        record(
            sid,
            expected=(
                "While the out-of-band approval channel is simulated down, the task stays observably "
                "AUTH_REQUIRED (no fabricated state) and the protected tool never fires; after the channel "
                "is restored, a normal approval executes exactly once"
            ),
            actual=(
                f"state1={state1}, state_during_outage={state_during_outage}, state_confirmed={state_confirmed}, "
                f"ledger_before={ledger_before}, ledger_during_outage={ledger_during_outage}, "
                f"recovered_state={recovered_state}, ledger_after_recovery={ledger_after_recovery}"
            ),
            status="PASS" if ok else "FAIL",
            note="Channel actively down (vs H03's silence) via /__test__/channel fault injection; distinct from H03's 'no response ever sent'.",
            detail={"parent_task_id": parent_task_id},
        )
    finally:
        await client.close()
        # Ensure channel is left up for subsequent scenarios.
        httpx.post(f"{EXPENSE_URL}/__test__/channel", json={"down": False})


async def main():
    if os.path.exists(RESULTS_PATH):
        os.remove(RESULTS_PATH)
    # Each scenario gets its own httpx.AsyncClient since the A2A `Client`
    # created from it is closed at the end of each scenario (client.close()
    # closes the underlying httpx client too).
    # Full regression: all twelve scenarios (H01-H12), in ID order. The five
    # originally-passing scenarios (H01/H02/H04/H08/H09) are run completely
    # unmodified -- same functions, same assertions -- alongside the seven
    # new ones (H03/H05/H06/H07/H10/H11/H12), per the Phase 2 requirement
    # that old assertions not be weakened to accommodate new work.
    scenario_fns = [
        scenario_h01,
        scenario_h02,
        scenario_h03,
        scenario_h04,
        scenario_h05,
        scenario_h06,
        scenario_h07,
        scenario_h08,
        scenario_h09,
        scenario_h10,
        scenario_h11,
        scenario_h12,
    ]
    for fn in scenario_fns:
        async with httpx.AsyncClient(timeout=30.0) as c:
            await fn(c)


if __name__ == "__main__":
    asyncio.run(main())
