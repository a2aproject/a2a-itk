"""Expense Agent server: real a2a-sdk==1.2.2 AgentExecutor + FastAPI/uvicorn,
plus a clearly-separate, NOT-A2A `/__human__/*` set of endpoints that model
the out-of-band human approval authority (per task brief: "a SEPARATE,
clearly-NOT-A2A endpoint/mechanism", convention ported from
agent04_hil/expense_agent.py's `/__human__/*` prefix).

Wire capture: a Starlette middleware logs every inbound request + outbound
response body to evidence/expense-wire.jsonl, satisfying the requirement to
capture actual HTTP traffic rather than hand-written logs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time

import uvicorn
from fastapi import FastAPI, Request

sys.path.insert(0, os.path.dirname(__file__))

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_rest_routes,
)
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
)

from approval_gate import GATE
from executor import ExpenseAgentExecutor, RESUME_APPROVED, RESUME_DENIED, RESUME_LATE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logger = logging.getLogger("expense_agent.server")

HOST = os.environ.get("EXPENSE_HOST", "127.0.0.1")
PORT = int(os.environ.get("EXPENSE_PORT", "8201"))
EVIDENCE_PATH = os.environ.get(
    "EXPENSE_WIRE_EVIDENCE",
    os.path.join(os.path.dirname(__file__), "..", "evidence", "expense-agent-wire.jsonl"),
)


def _trace(record: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(EVIDENCE_PATH)), exist_ok=True)
    with open(EVIDENCE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str, sort_keys=True) + "\n")


class WireCaptureMiddleware:
    """ASGI middleware (not BaseHTTPMiddleware) that logs raw request/response
    bytes at the transport boundary -- real wire traffic capture, not a
    hand-written claim of what traffic would look like.

    BaseHTTPMiddleware is deliberately NOT used here: it fully buffers
    `response.body_iterator` before returning, which is incompatible with
    this server's long-lived SSE (`message:stream`) responses -- it caused a
    `RuntimeError: Unexpected message received: http.request` and silently
    truncated the SSE stream after the first chunk. A raw ASGI middleware
    tees bytes as they pass through without buffering or blocking the
    stream.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        path = scope.get("path", "")
        query = scope.get("query_string", b"").decode("utf-8", errors="replace")
        start = time.time()

        req_chunks = []

        async def tee_receive():
            message = await receive()
            if message.get("type") == "http.request":
                req_chunks.append(message.get("body", b""))
            return message

        resp_chunks = []
        status_holder = {"code": None}

        async def tee_send(message):
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
            elif message["type"] == "http.response.body":
                resp_chunks.append(message.get("body", b""))
            await send(message)

        await self.app(scope, tee_receive, tee_send)

        _trace(
            {
                "kind": "wire_http",
                "server": "expense_agent",
                "method": method,
                "path": path,
                "query": query,
                "request_body": b"".join(req_chunks).decode("utf-8", errors="replace")[:8000],
                "status_code": status_holder["code"],
                "response_body": b"".join(resp_chunks).decode("utf-8", errors="replace")[:8000],
                "duration_ms": round((time.time() - start) * 1000, 2),
                "at": time.time(),
            }
        )


def build_agent_card(base_url: str) -> AgentCard:
    return AgentCard(
        name="Expense Agent (real SDK)",
        description="Reimbursement submission agent requiring human approval via AUTH_REQUIRED gate.",
        provider=AgentProvider(organization="A2A Conformance Testing", url="https://example.invalid"),
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["text", "application/json"],
        default_output_modes=["text", "application/json"],
        skills=[
            AgentSkill(
                id="submit_reimbursement",
                name="Submit Reimbursement",
                description="Submits an expense reimbursement after human approval.",
                tags=["expense", "finance", "hil"],
                examples=['{"amountMinor": 15000, "currency": "USD", "beneficiary": "acme"}'],
                input_modes=["text", "application/json"],
                output_modes=["text", "application/json"],
            )
        ],
        supported_interfaces=[
            AgentInterface(
                protocol_binding="HTTP+JSON",
                protocol_version="1.0",
                url=f"{base_url}/a2a/rest",
            )
        ],
    )


def build_app() -> FastAPI:
    base_url = f"http://{HOST}:{PORT}"
    agent_card = build_agent_card(base_url)
    task_store = InMemoryTaskStore()
    executor = ExpenseAgentExecutor()
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
        agent_card=agent_card,
    )

    app = FastAPI(title="Expense Agent")
    app.add_middleware(WireCaptureMiddleware)

    # --- NOT A2A: out-of-band human approval authority -------------------
    # Deliberately mounted under /__human__/* (ported naming convention from
    # agent04_hil/expense_agent.py) to make unmistakably clear this is not
    # part of the A2A protocol surface -- it's the separate channel a real
    # human-approval system (Slack bot, ticketing UI, etc) would use.
    #
    # Registered BEFORE add_a2a_routes_to_fastapi: that call appends a
    # catch-all `/{tenant}` route directly to app.routes (bypassing the
    # FastAPI router), which would otherwise shadow these paths since
    # Starlette matches routes in registration order.

    @app.get("/__human__/pending/{task_id}")
    async def human_pending(task_id: str):
        ch = GATE.get_challenge_for_task(task_id)
        if ch is None:
            return {"found": False}
        return {
            "found": True,
            "challengeId": ch.challenge_id,
            "taskId": ch.task_id,
            "action": ch.action,
            "actionDigest": ch.action_digest,
            "decision": ch.decision,
            "expiresAt": ch.expires_at,
        }

    @app.get("/__human__/ledger")
    async def human_ledger():
        return {"ledger": GATE.ledger_snapshot()}

    @app.post("/__human__/decision")
    async def human_decision(request: Request):
        """THE ONLY HTTP entry point that records a human approval/denial
        decision. This stands in for a real human-approval UI/bot acting
        out-of-band -- it is called DIRECTLY by a human-approval-simulator
        client, never via any A2A message. `executor.py`'s A2A-facing resume
        handling only ever READS a decision recorded here; it must never
        call `GATE.record_decision()` itself (see executor.py's module
        docstring for the full rationale).

        Body: {"challengeId": str, "decision": "APPROVED"|"DENIED",
               "principal": str, "taskIsCanceled": bool (optional)}

        NON-PRODUCTION / SIMULATOR-ONLY: this endpoint performs NO
        authentication of the caller and NO verification that the supplied
        `principal` string corresponds to any real, independently-verified
        human identity. It is a mock principal-claim allowlist: whichever
        caller supplies the exact string `approval_gate.TRUSTED_PRINCIPAL`
        is treated as "the trusted human," full stop. See
        `approval_gate.py`'s module docstring for the complete notice.
        """
        body = await request.json()
        challenge_id = body.get("challengeId")
        decision = body.get("decision")
        principal = body.get("principal", "")
        task_is_canceled = bool(body.get("taskIsCanceled", False))
        if not challenge_id or decision not in ("APPROVED", "DENIED"):
            return {"ok": False, "error": "challengeId and decision (APPROVED|DENIED) are required"}
        ch = GATE.record_decision(
            challenge_id,
            decision=decision,
            principal=principal,
            task_is_canceled=task_is_canceled,
        )
        if ch is None:
            return {"ok": False, "error": "no_such_challenge"}
        return {
            "ok": True,
            "challengeId": ch.challenge_id,
            "decision": ch.decision,
            "decidedBy": ch.decided_by,
        }

    # --- NOT A2A: test-only fault injection / clock control ---------------
    # These exist purely so the conformance test client can deterministically
    # drive H03 (expiry) and H12 (channel outage) without racing real wall-
    # clock time or needing an actual unreachable network dependency. They
    # are not part of the A2A protocol surface, same as /__human__/*, and are
    # equally clearly namespaced to make that unmistakable.

    @app.post("/__test__/advance_clock")
    async def advance_clock(request: Request):
        body = await request.json()
        seconds = float(body.get("seconds", 0))
        future_time = time.time() + seconds
        GATE.set_clock(lambda _t=future_time: _t)
        return {"ok": True, "advancedSeconds": seconds, "simulatedNow": future_time}

    @app.post("/__test__/channel")
    async def set_channel(request: Request):
        body = await request.json()
        down = bool(body.get("down", False))
        GATE.set_channel_down(down)
        return {"ok": True, "channelDown": down}

    @app.get("/__health__")
    async def health():
        return {"ok": True}

    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(agent_card=agent_card),
        rest_routes=create_rest_routes(request_handler=request_handler, path_prefix="/a2a/rest"),
    )

    return app


def main() -> None:
    app = build_app()
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="info", access_log=False)
    server = uvicorn.Server(config)
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
