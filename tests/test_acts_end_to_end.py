"""The whole stack against a fake agent: loader, dispatcher, runner, assertions.

Every other ACTS test isolates one layer. This one runs the *real* JSON-RPC
dispatcher and the *real* runner over real corpus tests, against an in-process
agent that behaves correctly, and asserts they come out **pass**.

That direction matters. A runner that fails everything looks healthy under a
suite that only ever checks failures, and the other corpus test — 111 tests
against a SUT that answers `{}` — can only show that nothing crashes. This one
shows the stack can recognize a conforming implementation, which is the thing
a conformance runner is actually for.

The agent is deliberately small: enough of `SendMessage`, `GetTask`,
`CancelTask`, `ListTasks` and `SendStreamingMessage` to satisfy the core
lifecycle, and no more.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from test_suite.acts import load_suite
from test_suite.acts.dispatcher import JsonRpcDispatcher
from test_suite.acts.runner import Outcome, Runner
from test_suite.acts.schema import TransportBinding


CORPUS = (
    Path(__file__).resolve().parent.parent / 'scenarios' / 'acts' / 'suite.acts.yaml'
)

AGENT_CARD: dict[str, Any] = {
    'name': 'fake',
    'protocolVersion': '1.0',
    'capabilities': {'streaming': True, 'pushNotifications': False},
    'supportedInterfaces': [{'protocolBinding': 'JSONRPC', 'url': 'http://sut.test'}],
    'skills': [{'id': 'echo', 'name': 'echo'}],
}


class FakeAgent:
    """A minimal, deliberately *conforming* A2A agent over JSON-RPC."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}

    # -- task construction --------------------------------------------------

    def _new_task(self, message: dict[str, Any], state: str) -> dict[str, Any]:
        task_id = str(uuid.uuid4())
        context_id = message.get('contextId') or str(uuid.uuid4())
        task = {
            'id': task_id,
            'contextId': context_id,
            'status': {'state': state},
            'history': [dict(message, taskId=task_id, contextId=context_id)],
            'artifacts': [
                {
                    'artifactId': str(uuid.uuid4()),
                    'parts': [{'text': 'done'}],
                }
            ],
        }
        self.tasks[task_id] = task
        return task

    def _continue(self, task: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
        task['history'].append(message)
        return task

    # -- JSON-RPC methods ---------------------------------------------------

    def send_message(self, params: dict[str, Any]) -> dict[str, Any]:
        message = params.get('message') or {}
        existing = self.tasks.get(message.get('taskId') or '')
        if existing is not None:
            return {'task': self._continue(existing, message)}
        return {'task': self._new_task(message, 'TASK_STATE_COMPLETED')}

    def get_task(self, params: dict[str, Any]) -> dict[str, Any]:
        task = self.tasks.get(params.get('id') or '')
        if task is None:
            raise JsonRpcError(-32001, 'task not found')
        return task

    def cancel_task(self, params: dict[str, Any]) -> dict[str, Any]:
        task = self.tasks.get(params.get('id') or '')
        if task is None:
            raise JsonRpcError(-32001, 'task not found')
        if task['status']['state'].startswith('TASK_STATE_COMPLETED'):
            raise JsonRpcError(-32002, 'task is in a terminal state')
        task['status']['state'] = 'TASK_STATE_CANCELED'
        return task

    def list_tasks(self, params: dict[str, Any]) -> dict[str, Any]:
        context = params.get('contextId')
        tasks = [
            task for task in self.tasks.values()
            if context is None or task['contextId'] == context
        ]
        return {'tasks': tasks}

    def stream(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        task = self._new_task(params.get('message') or {}, 'TASK_STATE_WORKING')
        return [
            {'task': task},
            {'statusUpdate': {
                'taskId': task['id'],
                'contextId': task['contextId'],
                'status': {'state': 'TASK_STATE_WORKING'},
            }},
            {'artifactUpdate': {
                'taskId': task['id'],
                'contextId': task['contextId'],
                'artifact': {'artifactId': 'a1', 'parts': [{'text': 'chunk'}]},
            }},
            {'statusUpdate': {
                'taskId': task['id'],
                'contextId': task['contextId'],
                'status': {'state': 'TASK_STATE_COMPLETED'},
            }},
        ]

    def subscribe(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Resubscription replays the current state, then runs to completion."""
        task = self.tasks.get(params.get('id') or '')
        if task is None:
            return []
        return [
            {'task': task},
            {'statusUpdate': {
                'taskId': task['id'],
                'contextId': task['contextId'],
                'status': {'state': 'TASK_STATE_COMPLETED'},
            }},
        ]

    METHODS = {
        'SendMessage': 'send_message',
        'GetTask': 'get_task',
        'CancelTask': 'cancel_task',
        'ListTasks': 'list_tasks',
    }

    # -- transport ----------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == '/.well-known/agent-card.json':
            return httpx.Response(200, json=AGENT_CARD)
        if request.method != 'POST' or request.url.path != '/':
            # The corpus's raw steps also probe REST paths and the extended
            # card, neither of which this JSON-RPC-only agent serves.
            return httpx.Response(404, json={'error': {'code': 404}})

        try:
            envelope = json.loads(request.content)
        except ValueError:
            # `JSONRPC-ERR-003` sends deliberately malformed JSON.
            return self._error(None, -32700, 'parse error')
        if not isinstance(envelope, dict):
            return self._error(None, -32600, 'invalid request')

        method, params = envelope.get('method'), envelope.get('params') or {}
        call_id = envelope.get('id')

        if method in ('SendStreamingMessage', 'SubscribeToTask'):
            events = (
                self.stream(params) if method == 'SendStreamingMessage'
                else self.subscribe(params)
            )
            body = ''.join(
                f'data: {json.dumps({"jsonrpc": "2.0", "id": call_id, "result": event})}\n\n'
                for event in events
            )
            return httpx.Response(
                200, text=body, headers={'Content-Type': 'text/event-stream'}
            )

        name = self.METHODS.get(method)
        if name is None:
            return self._error(call_id, -32601, f'no such method {method!r}')
        try:
            result = getattr(self, name)(params)
        except JsonRpcError as exc:
            return self._error(call_id, exc.code, exc.message)
        return httpx.Response(
            200, json={'jsonrpc': '2.0', 'id': call_id, 'result': result}
        )

    @staticmethod
    def _error(call_id: Any, code: int, message: str) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                'jsonrpc': '2.0',
                'id': call_id,
                'error': {'code': code, 'message': message},
            },
        )


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


#: Corpus tests this agent implements enough of to satisfy. Kept explicit
#: rather than "whatever passes": the point is that a named set of real
#: conformance tests goes green, so a regression shows up as a specific test
#: rather than as a number quietly dropping.
EXPECTED_PASSES = [
    'CORE-SEND-001',
    'CORE-GET-001',
    'CORE-ERR-001',
    'CORE-ERR-002',
    'STREAM-SSE-001',
    'STREAM-SSE-002',
    'STREAM-MULTI-001',
]


@pytest.fixture(scope='module')
def suite():
    return load_suite(CORPUS)


def run_tests(test_ids: list[str], suite) -> dict[str, Any]:
    agent = FakeAgent()

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(agent.handler))
        dispatcher = JsonRpcDispatcher('http://sut.test', client=client)
        runner = Runner(
            dispatcher,
            variables=dict(suite.variables),
            agent_card=AGENT_CARD,
            sut_behaviors=None,
            sleep=lambda _: asyncio.sleep(0),
        )
        async with dispatcher:
            return {
                test_id: await runner.run_test(suite.by_id(test_id).test)
                for test_id in test_ids
            }

    return asyncio.run(go())


class TestAConformingAgentPasses:
    def test_the_named_tests_pass(self, suite):
        results = run_tests(EXPECTED_PASSES, suite)
        failed = {
            test_id: (r.result.value, r.failure.message if r.failure else r.skip_reason)
            for test_id, r in results.items()
            if r.result is not Outcome.PASS
        }
        assert failed == {}

    def test_every_step_actually_ran(self, suite):
        """A pass with no steps executed would be a skip in disguise."""
        results = run_tests(EXPECTED_PASSES, suite)
        for test_id, result in results.items():
            assert result.steps, test_id
            assert all(s.result is Outcome.PASS for s in result.steps), test_id

    def test_assertions_were_actually_compared(self, suite):
        """`checks` guards against a pass that asserted nothing at all."""
        results = run_tests(EXPECTED_PASSES, suite)
        for test_id, result in results.items():
            assert sum(s.checks for s in result.steps) > 0, test_id


class TestABrokenAgentFails:
    """The control: the same stack must notice when the agent misbehaves."""

    def test_a_wrong_task_state_is_caught(self, suite):
        agent = FakeAgent()
        original = agent.send_message

        def wrong(params):
            result = original(params)
            result['task']['status']['state'] = 'TASK_STATE_WORKING'
            return result

        agent.send_message = wrong
        result = self._run_one(agent, suite, 'CORE-SEND-001')
        assert result.result is Outcome.FAIL
        assert 'TASK_STATE_COMPLETED' in str(result.failure.expected)

    def test_a_missing_error_is_caught(self, suite):
        """`CORE-ERR-001` expects `TaskNotFoundError` for an unknown task."""
        agent = FakeAgent()
        agent.get_task = lambda params: {'id': 'invented', 'status': {'state': 'X'}}
        result = self._run_one(agent, suite, 'CORE-ERR-001')
        assert result.result is Outcome.FAIL
        assert 'expected an error' in result.failure.message

    def test_a_stream_that_regresses_is_caught(self, suite):
        agent = FakeAgent()
        agent.stream = lambda params: [
            {'statusUpdate': {'status': {'state': 'TASK_STATE_COMPLETED'}}},
            {'statusUpdate': {'status': {'state': 'TASK_STATE_WORKING'}}},
        ]
        result = self._run_one(agent, suite, 'STREAM-SSE-001')
        assert result.result is Outcome.FAIL

    @staticmethod
    def _run_one(agent: FakeAgent, suite, test_id: str):
        async def go():
            client = httpx.AsyncClient(transport=httpx.MockTransport(agent.handler))
            dispatcher = JsonRpcDispatcher('http://sut.test', client=client)
            runner = Runner(
                dispatcher,
                variables=dict(suite.variables),
                agent_card=AGENT_CARD,
                sleep=lambda _: asyncio.sleep(0),
            )
            async with dispatcher:
                return await runner.run_test(suite.by_id(test_id).test)

        return asyncio.run(go())


class TestBehaviorGating:
    def test_an_undeclared_tck_behavior_fails_the_test(self, suite):
        """With a contract in hand, a lagging SUT is visible rather than skipped."""
        agent = FakeAgent()

        async def go():
            client = httpx.AsyncClient(transport=httpx.MockTransport(agent.handler))
            dispatcher = JsonRpcDispatcher('http://sut.test', client=client)
            runner = Runner(
                dispatcher,
                variables=dict(suite.variables),
                agent_card=AGENT_CARD,
                sut_behaviors=frozenset(),
                sleep=lambda _: asyncio.sleep(0),
            )
            async with dispatcher:
                return await runner.run_test(suite.by_id('CORE-SEND-001').test)

        result = asyncio.run(go())
        assert result.result is Outcome.FAIL
        assert 'does not declare behavior' in result.failure.message


class TestTheSuiteRunsAsAWhole:
    def test_a_full_pass_over_the_corpus_produces_a_report(self, suite):
        """Not about conformance — about the run completing and tallying."""
        agent = FakeAgent()

        async def go():
            client = httpx.AsyncClient(transport=httpx.MockTransport(agent.handler))
            dispatcher = JsonRpcDispatcher('http://sut.test', client=client)
            runner = Runner(
                dispatcher,
                variables={
                    **suite.variables,
                    'insufficientAuthToken': 'nope',
                    'otherUserTaskId': 'nope',
                },
                agent_card=AGENT_CARD,
                sleep=lambda _: asyncio.sleep(0),
            )
            async with dispatcher:
                return await runner.run_suite(suite)

        results = asyncio.run(go())
        assert len(results) == len(suite.tests)
        assert not any(r.result is Outcome.ERROR for r in results), [
            (r.id, r.failure.message) for r in results if r.result is Outcome.ERROR
        ]
        assert sum(r.result is Outcome.PASS for r in results) >= len(EXPECTED_PASSES)

    def test_the_dispatcher_reports_the_right_binding(self, suite):
        async def go():
            client = httpx.AsyncClient(transport=httpx.MockTransport(FakeAgent().handler))
            async with JsonRpcDispatcher('http://sut.test', client=client) as dispatcher:
                return Runner(dispatcher).binding

        assert asyncio.run(go()) is TransportBinding.JSONRPC
