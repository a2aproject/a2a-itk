"""The whole stack against a fake agent: pipeline, dispatcher, runner, assertions.

Every other ACTS test isolates one layer. This one runs the *real* pipeline,
the *real* JSON-RPC dispatcher and the *real* runner over the mirrored corpus,
against an in-process agent that behaves correctly, and asserts the tests come
out **pass**.

That direction matters. A runner that fails everything looks healthy under a
suite that only ever checks failures, and the other corpus test, which runs
against a SUT that answers `{}`, can only show that nothing crashes. This one
shows the stack can recognize a conforming implementation, which is the thing
a conformance runner is actually for. The broken-agent controls below show
the other half: the same stack notices when the agent misbehaves.

The agent is :class:`tests.acts_fake_agent.FakeAgent`. It keeps real task
state and runs on virtual time, so a corpus that polls for seconds and holds
tasks for seconds runs in well under one real second. Only the tested SUT is
replaced: the webhook receiver is the real `notifications_app`, in process.
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import pytest

import acts_runner
from notifications_app import create_notifications_app
from pyproto import a2a_pb2 as pb
from test_suite.acts import load_suite
from test_suite.acts.dispatcher import JsonRpcDispatcher
from test_suite.acts.runner import VERSION_HEADER, Outcome, Runner, TestResult
from test_suite.acts.schema import ErrorType, Operation, RunnerRequirement, TransportBinding
from tests.acts_fake_agent import (
    CARD_PATH,
    UNSUPPORTED_OPERATION,
    AgentError,
    FakeAgent,
    VirtualTimeLoop,
    run_virtual,
)


CORPUS = (
    Path(__file__).resolve().parent.parent / 'scenarios' / 'acts' / 'suite.acts.yaml'
)

#: The three agents a run meets: the ordinary one, and the two it is
#: restarted as for the tests the ordinary one has to skip.
MAIN = 'http://sut.test'
REDUCED = 'http://reduced.sut.test'
AUTH = 'http://auth.sut.test'

WEBHOOK = 'http://webhook.test'


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


@pytest.fixture(scope='module')
def behaviors_path(tmp_path_factory) -> Path:
    """The fake agent's behavior contract, as the file an SDK would ship."""
    path = tmp_path_factory.mktemp('fake-agent') / 'sut-behaviors.yaml'
    path.write_text(FakeAgent().behaviors_file(), encoding='utf-8')
    return path


def conformance_run(
    behaviors_path: Path,
    *,
    spec_version: str = '1.0',
    test_ids: list[str] | None = None,
    suite_path: Path = CORPUS,
    customize: Callable[[FakeAgent], None] | None = None,
    versions: tuple[str, ...] = ('1.0', '1.1'),
) -> acts_runner.ActsRun:
    """One conformance run over JSON-RPC, through `acts_runner.run`.

    The pass that starts a SUT, fetches its card and starts the webhook
    receiver is replaced: the three SUTs are fake agents in this process,
    reached through a mock transport. Test selection, the dispatcher, the
    deviation passes and the splicing of their verdicts are the pipeline's
    own.

    ``customize`` is handed the ordinary agent before the run, to break it.
    """

    async def go() -> acts_runner.ActsRun:
        loop = asyncio.get_running_loop()
        receiver = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_notifications_app()),
            base_url=WEBHOOK,
        )

        async def push(url: str, headers: dict[str, str], body: dict[str, Any]) -> None:
            await receiver.post(url, headers=headers, content=json.dumps(body))

        async def read_webhook(task_id: str) -> list[dict[str, Any]]:
            reply = await receiver.get(f'/{task_id}/notifications')
            return reply.json()['notifications']

        agents = {
            MAIN: FakeAgent(base_url=MAIN, versions=versions, push_sender=push),
            REDUCED: FakeAgent(
                base_url=REDUCED, versions=versions, push_sender=push,
                reduced_capabilities=True,
            ),
            AUTH: FakeAgent(
                base_url=AUTH, versions=versions, push_sender=push, require_auth=True
            ),
        }
        if customize is not None:
            customize(agents[MAIN])

        async def route(request: httpx.Request) -> httpx.Response:
            return await agents[f'{request.url.scheme}://{request.url.host}'].handle(request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(route))

        async def one_pass(
            suite, *, transport, spec_version, variables, capabilities, declared,
            sut_url, **_,
        ) -> tuple[list[TestResult], dict[str, Any]]:
            """What `acts_runner._run_pass` does, minus starting a process."""
            reply = await client.get(
                f'{sut_url}{CARD_PATH}', headers={VERSION_HEADER: spec_version}
            )
            card = reply.json()
            dispatcher = acts_runner.build_dispatcher(card, transport, sut_url, spec_version)
            runner = Runner(
                dispatcher,
                variables={**variables, 'webhookUrl': f'{WEBHOOK}/notifications'},
                spec_version=spec_version,
                agent_card=card,
                sut_behaviors=declared,
                capabilities=[*capabilities, RunnerRequirement.WEBHOOK_ENDPOINT],
                read_webhook=read_webhook,
                clock=loop.time,
            )
            return await runner.run_suite(suite), card

        # The dispatcher the pipeline builds, over the mock transport.
        over_the_mock = functools.partial(acts_runner.for_binding, client=client)
        try:
            with (
                mock.patch.object(acts_runner, '_run_pass', one_pass),
                mock.patch.object(acts_runner, 'for_binding', over_the_mock),
            ):
                return await acts_runner.run(
                    transport=TransportBinding.JSONRPC,
                    spec_version=spec_version,
                    sut_url=MAIN,
                    suite_path=suite_path,
                    test_ids=test_ids,
                    behaviors_path=behaviors_path,
                    deviation_urls={
                        acts_runner.REDUCED_CAPABILITIES_ENV: REDUCED,
                        acts_runner.AUTH_ENFORCED_ENV: AUTH,
                    },
                )
        finally:
            for agent in agents.values():
                await agent.aclose()
            await client.aclose()
            await receiver.aclose()

    return run_virtual(go())


def not_passed(run: acts_runner.ActsRun) -> dict[str, tuple[str, str | None]]:
    """Every result that is not a pass, with why."""
    return {
        r.id: (r.result.value, r.failure.message if r.failure else r.skip_reason)
        for r in run.results
        if r.result is not Outcome.PASS
    }


@pytest.fixture(scope='module')
def whole_corpus(behaviors_path) -> acts_runner.ActsRun:
    """The mirrored corpus at A2A 1.0, run once for the module."""
    return conformance_run(behaviors_path)


# ---------------------------------------------------------------------------
# A conforming agent passes
# ---------------------------------------------------------------------------


class TestAConformingAgentPasses:
    def test_every_test_of_the_corpus_passes(self, whole_corpus):
        """Kept as "nothing but passes" and not as a count: a regression then
        shows up as a named test with its reason."""
        assert not_passed(whole_corpus) == {}

    def test_the_run_covers_every_jsonrpc_test(self, whole_corpus):
        applicable = load_suite(CORPUS).for_transport(TransportBinding.JSONRPC)
        assert {r.id for r in whole_corpus.results} == {t.id for t in applicable}

    def test_every_step_actually_ran(self, whole_corpus):
        """A pass with no steps executed would be a skip in disguise."""
        for result in whole_corpus.results:
            assert result.steps, result.id
            assert all(s.result is Outcome.PASS for s in result.steps), result.id

    def test_assertions_were_actually_compared(self, whole_corpus):
        """`checks` guards against a pass that asserted nothing at all."""
        for result in whole_corpus.results:
            assert sum(s.checks for s in result.steps) > 0, result.id

    def test_the_deviation_tests_ran_against_the_deviated_agents(self, whole_corpus):
        """Tests the ordinary agent has to skip get their verdict from an
        agent started differently, and the verdict says which."""
        by_configuration: dict[str, set[str]] = {}
        for result in whole_corpus.results:
            if result.configuration:
                by_configuration.setdefault(result.configuration, set()).add(result.id)
        assert by_configuration == {
            acts_runner.REDUCED_CAPABILITIES_ENV: {
                'CORE-CAP-001', 'CORE-CAP-002', 'SEC-EXTCARD-003', 'PUSH-CFG-004',
            },
            acts_runner.AUTH_ENFORCED_ENV: {
                'SEC-AUTH-001', 'SEC-AUTH-002', 'SEC-AUTH-003', 'SEC-AUTH-004',
            },
        }

    def test_the_report_is_conformant(self, whole_corpus):
        report = acts_runner.to_report(whole_corpus, sdk_name='fake-agent')
        assert report['summary']['passed'] == len(whole_corpus.results)
        assert report['summary']['total'] == len(whole_corpus.results)
        assert report['spec_version'] == '1.0'
        assert report['environment']['interface_protocol_version'] == '1.0'


# ---------------------------------------------------------------------------
# A broken agent fails
# ---------------------------------------------------------------------------


class TestABrokenAgentFails:
    """The control: the same stack must notice when the agent misbehaves."""

    def run_one(self, behaviors_path, test_id, customize) -> TestResult:
        run = conformance_run(behaviors_path, test_ids=[test_id], customize=customize)
        (result,) = run.results
        return result

    def test_a_wrong_task_state_is_caught(self, behaviors_path):
        def fail_instead_of_completing(agent: FakeAgent) -> None:
            agent.behaviors['tck-complete-task'] = agent.behaviors['tck-task-failure']

        result = self.run_one(behaviors_path, 'CORE-SEND-001', fail_instead_of_completing)
        assert result.result is Outcome.FAIL
        assert 'TASK_STATE_COMPLETED' in str(result.failure.expected)
        assert 'TASK_STATE_FAILED' in str(result.failure.actual)

    def test_a_missing_error_is_caught(self, behaviors_path):
        """`CORE-ERR-001` expects `TaskNotFoundError` for an unknown task."""
        def invent_the_task(agent: FakeAgent) -> None:
            async def get_task(params, version):
                return {'id': params.get('id'), 'status': {'state': 'TASK_STATE_WORKING'}}

            agent._rpc_get_task = get_task

        result = self.run_one(behaviors_path, 'CORE-ERR-001', invent_the_task)
        assert result.result is Outcome.FAIL
        assert 'expected an error' in result.failure.message

    def test_the_wrong_error_is_caught(self, behaviors_path):
        """An error is not enough; it has to be the one the test names."""
        def refuse_with_another_error(agent: FakeAgent) -> None:
            def find(task_id):
                raise AgentError(UNSUPPORTED_OPERATION, 'this operation is not supported')

            agent._find = find

        result = self.run_one(behaviors_path, 'CORE-ERR-001', refuse_with_another_error)
        assert result.result is Outcome.FAIL
        assert 'TaskNotFoundError' in str(result.failure.expected)
        assert 'UnsupportedOperationError' in str(result.failure.actual)

    def test_a_stream_that_regresses_is_caught(self, behaviors_path):
        """Nothing may return to SUBMITTED once the task has left it."""
        def regress(agent: FakeAgent) -> None:
            async def stream_basic(record, request):
                agent._set_status(record, pb.TASK_STATE_WORKING)
                agent._set_status(record, pb.TASK_STATE_SUBMITTED)
                agent._set_status(record, pb.TASK_STATE_COMPLETED)

            agent.behaviors['tck-stream-basic'] = stream_basic

        result = self.run_one(behaviors_path, 'STREAM-SSE-001', regress)
        assert result.result is Outcome.FAIL
        assert 'TASK_STATE_SUBMITTED' in result.failure.message

    def test_a_notification_without_its_credential_is_caught(self, behaviors_path):
        """`SEC-PUSH-001` reads the delivery off the real receiver."""
        def forget_the_credential(agent: FakeAgent) -> None:
            send = agent._push_sender

            async def push(url, headers, body):
                await send(url, {k: v for k, v in headers.items() if k != 'Authorization'}, body)

            agent._push_sender = push

        result = self.run_one(behaviors_path, 'SEC-PUSH-001', forget_the_credential)
        assert result.result is Outcome.FAIL
        assert 'Authorization' in result.failure.assertion_path


class TestBehaviorGating:
    def test_an_undeclared_tck_behavior_fails_the_test(self, tmp_path):
        """With a contract in hand, a lagging SUT is visible rather than skipped."""
        contract = tmp_path / 'sut-behaviors.yaml'
        contract.write_text(
            'acts_version: "1.0"\n'
            'behaviors:\n'
            '  - prefix: "tck-cancel"\n'
            '    description: "the only behavior this agent declares"\n',
            encoding='utf-8',
        )
        run = conformance_run(contract, test_ids=['CORE-SEND-001'])
        (result,) = run.results
        assert result.result is Outcome.FAIL
        assert 'does not declare behavior' in result.failure.message


# ---------------------------------------------------------------------------
# The run version, end to end
# ---------------------------------------------------------------------------

VERSION_PROBE = '''\
acts_version: "1.0"
spec_version: "1.1"
suites:
  - id: probe
    name: Probe
    tests:
      - id: PROBE-NOT-FOUND
        name: A task that does not exist is not found
        level: must
        min_spec_version: "1.1"
        steps:
          - id: get
            operation: get_task
            params: {id: "00000000-0000-0000-0000-000000000000"}
            expect_error: {error_type: TaskNotFoundError}
      - id: PROBE-RAW-VERSION
        name: A raw request carries the run version
        level: must
        min_spec_version: "1.1"
        transport: [jsonrpc]
        steps:
          - id: get
            raw:
              method: POST
              path: /
              headers:
                Content-Type: application/json
                A2A-Version: "{{specVersion}}"
              body:
                jsonrpc: "2.0"
                id: probe
                method: GetTask
                params: {id: "00000000-0000-0000-0000-000000000000"}
            expect:
              status: 200
              body:
                error: {code: -32001}
'''


class TestTheRunVersionReachesTheAgent:
    @pytest.fixture
    def probe(self, tmp_path) -> Path:
        path = tmp_path / 'probe.acts.yaml'
        path.write_text(VERSION_PROBE, encoding='utf-8')
        return path

    def test_a_1_1_run_dials_the_1_1_interface_and_speaks_1_1(self, behaviors_path, probe):
        seen: list[str | None] = []

        def record_versions(agent: FakeAgent) -> None:
            negotiate = agent._negotiate

            def recording(header):
                seen.append(header)
                return negotiate(header)

            agent._negotiate = recording

        run = conformance_run(
            behaviors_path, spec_version='1.1', suite_path=probe, customize=record_versions
        )
        assert not_passed(run) == {}
        # Once from the operation the runner built, once from the raw step
        # that wrote the header itself.
        assert seen == ['1.1', '1.1']
        report = acts_runner.to_report(run, sdk_name='fake-agent')
        assert report['spec_version'] == '1.1'
        assert report['environment']['interface_protocol_version'] == '1.1'

    def test_an_agent_without_a_1_1_interface_stops_the_run(self, behaviors_path, probe):
        """A configuration error, and not a report in which every test failed
        because a 1.0 interface refused the version."""
        with pytest.raises(acts_runner.ActsRunError) as exc:
            conformance_run(
                behaviors_path, spec_version='1.1', suite_path=probe, versions=('1.0',)
            )
        assert 'no jsonrpc interface for A2A 1.1' in str(exc.value)


# ---------------------------------------------------------------------------
# Generation, end to end
# ---------------------------------------------------------------------------
#
# `generation` is a 64-bit integer, which a JSON binding sends as a string of
# digits. These run tests that compare it, capture it from a response and from
# the first event of a stream, and compare later values with the captured one.

GENERATION_PROBE = '''\
acts_version: "1.0"
spec_version: "1.1"
suites:
  - id: generation-probe
    name: Generation probe
    tests:
      - id: PROBE-GENERATION-ADVANCES
        name: A client message advances the generation
        level: must
        min_spec_version: "1.1"
        requires_behaviors: [tck-multi-turn]
        steps:
          - id: turn1
            operation: send_message
            params:
              message:
                role: ROLE_USER
                parts: [{text: "tck-multi-turn generation probe"}]
            expect:
              status: 200
              body:
                task:
                  generation: {gte: 1}
            capture: {taskId: task.id, generation: task.generation}
          - id: turn2
            operation: send_message
            params:
              message:
                role: ROLE_USER
                taskId: "{{turn1.taskId}}"
                parts: [{text: more input}]
            expect:
              status: 200
              body:
                task:
                  generation: {gt: "{{turn1.generation}}"}
      - id: PROBE-RESUME
        name: A subscriber that resumes is sent only what is newer
        level: must
        min_spec_version: "1.1"
        requires_behaviors: [tck-hold-working]
        runner_requirements: [stream_disconnect]
        preconditions:
          capabilities: {streaming: true}
        steps:
          - id: start
            operation: send_message
            params:
              message:
                role: ROLE_USER
                parts: [{text: "tck-hold-working 8000 resume probe"}]
              configuration: {returnImmediately: true}
            expect: {status: 200}
            capture: {taskId: task.id}
          - id: interrupted
            operation: subscribe_to_task
            params: {id: "{{start.taskId}}"}
            expect_stream:
              min_count: 1
              streams:
                - disconnect_after: 1
            capture: {generation: task.generation}
          - id: resubscribe
            operation: subscribe_to_task
            params:
              id: "{{start.taskId}}"
              lastSeenGeneration: "{{interrupted.generation}}"
            expect_stream:
              timeout_ms: 20000
              final_event:
                status: {state: TASK_STATE_COMPLETED}
              each_event:
                status_update:
                  generation: {gt: "{{interrupted.generation}}"}
'''


class TestGenerationEndToEnd:
    @pytest.fixture
    def probe(self, tmp_path) -> Path:
        path = tmp_path / 'generation.acts.yaml'
        path.write_text(GENERATION_PROBE, encoding='utf-8')
        return path

    def run(self, behaviors_path, probe, customize=None) -> dict[str, TestResult]:
        run = conformance_run(
            behaviors_path, spec_version='1.1', suite_path=probe, customize=customize
        )
        return {r.id: r for r in run.results}

    def test_a_conforming_agent_passes(self, behaviors_path, probe):
        results = self.run(behaviors_path, probe)
        assert {
            r.id: r.failure.message for r in results.values() if r.result is not Outcome.PASS
        } == {}
        assert set(results) == {'PROBE-GENERATION-ADVANCES', 'PROBE-RESUME'}

    def test_a_generation_that_does_not_advance_is_caught(self, behaviors_path, probe):
        def stand_still(agent: FakeAgent) -> None:
            agent._advance = lambda record: record.task.generation

        result = self.run(behaviors_path, probe, stand_still)['PROBE-GENERATION-ADVANCES']
        assert result.result is Outcome.FAIL
        assert result.failure.step_id == 'turn2'
        assert result.failure.message == 'body.task.generation should be > 1, got 1'

    def test_an_agent_without_generation_fails_on_the_assertion(self, behaviors_path, probe):
        """Reported as the missing generation, which is what the agent got
        wrong, and not as a value the step could not capture."""
        def no_generation(agent: FakeAgent) -> None:
            view = agent._view

            def without(record, **kwargs):
                task = view(record, **kwargs)
                task.ClearField('generation')
                return task

            agent._view = without

        result = self.run(behaviors_path, probe, no_generation)['PROBE-GENERATION-ADVANCES']
        assert result.result is Outcome.FAIL
        assert result.failure.step_id == 'turn1'
        assert result.failure.assertion_path == 'body.task.generation'
        assert 'is missing' in result.failure.message

    def test_a_resume_that_starts_over_is_caught(self, behaviors_path, probe):
        """An agent that ignores the generation the client has and opens with
        the task again."""
        def start_over(agent: FakeAgent) -> None:
            subscribe = agent.subscribe_to_task

            def ignoring(request, version):
                request.ClearField('last_seen_generation')
                return subscribe(request, version)

            agent.subscribe_to_task = ignoring

        result = self.run(behaviors_path, probe, start_over)['PROBE-RESUME']
        assert result.result is Outcome.FAIL
        assert result.failure.step_id == 'resubscribe'
        assert result.failure.assertion_path.startswith('each_event[0]')


# ---------------------------------------------------------------------------
# What the fake agent does at A2A 1.1
# ---------------------------------------------------------------------------
#
# The mirrored corpus has no A2A 1.1 test yet, so nothing above reaches the
# agent's 1.1 behavior. These read it through the real dispatcher. They are
# about the fixture: they say what a corpus test for 1.1 will be run against.

AT_1_1 = {VERSION_HEADER: '1.1'}
AT_1_0 = {VERSION_HEADER: '1.0'}


def message(text: str, **fields: Any) -> dict[str, Any]:
    return {
        'messageId': str(uuid.uuid4()), 'role': 'ROLE_USER',
        'parts': [{'text': text}], **fields,
    }


def play(
    scenario: Callable[[FakeAgent, JsonRpcDispatcher], Awaitable[Any]], **agent_kwargs: Any
) -> Any:
    """Run ``scenario`` against a fresh agent, on virtual time."""

    async def go() -> Any:
        agent = FakeAgent(**agent_kwargs)
        client = httpx.AsyncClient(transport=httpx.MockTransport(agent.handle))
        dispatcher = JsonRpcDispatcher(
            agent.jsonrpc_url, rpc_path='/', client=client, agent_card_url=agent.base_url
        )
        try:
            return await scenario(agent, dispatcher)
        finally:
            await agent.aclose()
            await client.aclose()

    return run_virtual(go())


async def send(dispatcher, text: str, headers=AT_1_1, **params: Any):
    message_fields = {k: params.pop(k) for k in ('taskId', 'contextId') if k in params}
    return await dispatcher.dispatch(
        Operation.SEND_MESSAGE, {'message': message(text, **message_fields), **params}, headers
    )


async def collect(stream, *, patience: float | None = None) -> tuple[list[dict], bool]:
    """Read a stream to its end, or until nothing arrives for ``patience``
    seconds. Returns the events and whether the stream was still open."""
    events: list[dict] = []

    async def pump() -> None:
        async for event in stream:
            events.append(event.data)

    try:
        await asyncio.wait_for(pump(), patience)
    except (asyncio.TimeoutError, TimeoutError):
        return events, True
    return events, False


def generations(events: list[dict]) -> list[int]:
    """The generation each event carries, in stream order."""
    return [int(next(iter(event.values()))['generation']) for event in events]


def kinds(events: list[dict]) -> list[str]:
    return [next(iter(event)) for event in events]


class TestTheFakeAgentAt11:
    def test_every_mutation_is_one_event_one_generation_higher(self):
        async def scenario(agent, dispatcher):
            stream = dispatcher.stream(
                Operation.SEND_STREAMING_MESSAGE,
                {'message': message('tck-stream-chunked generation')}, AT_1_1,
            )
            return (await collect(stream))[0]

        events = play(scenario)
        assert kinds(events) == [
            'task', 'statusUpdate', 'artifactUpdate', 'artifactUpdate',
            'artifactUpdate', 'statusUpdate',
        ]
        # A new task is at 1; recording the message that created it makes 2.
        assert generations(events) == [2, 3, 4, 5, 6, 7]

    def test_a_64_bit_integer_is_a_string_on_the_wire(self):
        """As ProtoJSON writes it, which is what an assertion has to cope with."""
        async def scenario(agent, dispatcher):
            return (await send(dispatcher, 'tck-artifact-text wire form')).payload['task']

        task = play(scenario)
        assert task['generation'] == '5'
        assert task['artifacts'][0]['startGeneration'] == '4'
        assert task['artifacts'][0]['endGeneration'] == '4'

    def test_a_stale_generation_guard_is_refused(self):
        async def scenario(agent, dispatcher):
            first = (await send(dispatcher, 'tck-multi-turn guard')).payload['task']
            task_id, stale = first['id'], first['generation']
            await send(dispatcher, 'more input', taskId=task_id)
            refused = await send(
                dispatcher, 'guarded', taskId=task_id,
                configuration={'ifGenerationMatch': stale},
            )
            current = (await dispatcher.dispatch(
                Operation.GET_TASK, {'id': task_id}, AT_1_1
            )).payload['generation']
            accepted = await send(
                dispatcher, 'guarded', taskId=task_id,
                configuration={'ifGenerationMatch': current},
            )
            new_task = await send(
                dispatcher, 'tck-complete-task guard on a new task',
                configuration={'ifGenerationMatch': 424242},
            )
            return refused, accepted, new_task

        refused, accepted, new_task = play(scenario)
        assert refused.error.jsonrpc_code == -32010
        assert refused.error.reason == 'TASK_GENERATION_MISMATCH'
        assert accepted.error is None
        # There is no generation to compare with when the send creates the task.
        assert new_task.payload['task']['status']['state'] == 'TASK_STATE_COMPLETED'

    def test_a_long_poll_is_held_until_the_task_changes(self):
        async def scenario(agent, dispatcher):
            loop = asyncio.get_running_loop()
            started = (await send(
                dispatcher, 'tck-hold-working 5000 long poll',
                configuration={'returnImmediately': True},
            )).payload['task']
            await asyncio.sleep(1)
            working = (await dispatcher.dispatch(
                Operation.GET_TASK, {'id': started['id']}, AT_1_1
            )).payload

            before = loop.time()
            behind = await dispatcher.dispatch(
                Operation.GET_TASK, {'id': started['id'], 'currentGeneration': 0}, AT_1_1
            )
            at_once = loop.time() - before

            before = loop.time()
            held = await dispatcher.dispatch(
                Operation.GET_TASK,
                {'id': started['id'], 'currentGeneration': working['generation']},
                AT_1_1,
            )
            return working, behind.payload, at_once, held.payload, loop.time() - before

        working, behind, at_once, held, waited = play(scenario)
        assert working['status']['state'] == 'TASK_STATE_WORKING'
        assert behind['generation'] == working['generation']
        assert at_once < 0.1
        # Asked one second into a five second hold.
        assert 3.9 < waited < 4.1
        assert held['status']['state'] == 'TASK_STATE_COMPLETED'
        assert int(held['generation']) == int(working['generation']) + 1

    def test_a_long_poll_on_a_task_that_never_changes_is_answered_anyway(self):
        async def scenario(agent, dispatcher):
            loop = asyncio.get_running_loop()
            task = (await send(dispatcher, 'tck-multi-turn waits')).payload['task']
            before = loop.time()
            reply = await dispatcher.dispatch(
                Operation.GET_TASK,
                {'id': task['id'], 'currentGeneration': task['generation']}, AT_1_1,
            )
            return task, reply.payload, loop.time() - before

        task, reply, waited = play(scenario)
        assert reply['generation'] == task['generation']
        assert waited == pytest.approx(FakeAgent.long_poll_timeout_s, abs=0.1)

    def test_a_client_message_is_reported_to_a_1_1_subscriber_only(self):
        async def scenario(agent, dispatcher):
            task = (await send(
                dispatcher, 'tck-hold-working 8000 message update',
                configuration={'returnImmediately': True},
            )).payload['task']
            watching = [
                asyncio.create_task(collect(dispatcher.stream(
                    Operation.SUBSCRIBE_TO_TASK, {'id': task['id']}, headers
                )))
                for headers in (AT_1_1, AT_1_0)
            ]
            await asyncio.sleep(1)
            await send(
                dispatcher, 'a message while the task is working', taskId=task['id'],
                configuration={'returnImmediately': True},
            )
            (newer, _), (older, _) = await asyncio.gather(*watching)
            return newer, older

        newer, older = play(scenario)
        assert kinds(newer) == ['task', 'messageUpdate', 'statusUpdate']
        assert newer[1]['messageUpdate']['message']['parts'] == [
            {'text': 'a message while the task is working'}
        ]
        # The older client sees the gap the message left in the generations.
        assert kinds(older) == ['task', 'statusUpdate']
        assert generations(newer) == [3, 4, 5]
        assert generations(older) == [3, 5]

    def test_a_stream_stays_open_while_the_task_waits_for_input(self):
        """From 1.1 a state that waits for the client does not end a stream;
        before 1.1 it did."""
        async def scenario(agent, dispatcher):
            out = {}
            for name, headers in (('1.1', AT_1_1), ('1.0', AT_1_0)):
                stream = dispatcher.stream(
                    Operation.SEND_STREAMING_MESSAGE,
                    {'message': message('tck-multi-turn stays open')}, headers,
                )
                out[name] = await collect(stream, patience=3)
            return out

        out = play(scenario)
        for events, _ in out.values():
            assert events[-1]['statusUpdate']['status']['state'] == 'TASK_STATE_INPUT_REQUIRED'
        assert out['1.1'][1] is True
        assert out['1.0'][1] is False

    def test_the_timeline_records_messages_and_statuses_in_generation_order(self):
        async def scenario(agent, dispatcher):
            task = (await send(dispatcher, 'tck-multi-turn first')).payload['task']
            await send(dispatcher, 'second', taskId=task['id'])

            async def get(**params):
                reply = await dispatcher.dispatch(
                    Operation.GET_TASK, {'id': task['id'], **params}, AT_1_1
                )
                return reply.payload

            return await get(), await get(timelineLength=0), await get(timelineLength=1)

        full, none, one = play(scenario)
        timeline = full['timeline']
        assert [next(k for k in entry if k != 'generation') for entry in timeline] == [
            'message', 'status', 'status', 'message', 'status', 'status',
        ]
        assert [int(entry['generation']) for entry in timeline] == [2, 3, 4, 5, 6, 7]
        # Client messages are entries of their own; what the agent says is
        # carried inside its status.
        assert {e['message']['role'] for e in timeline if 'message' in e} == {'ROLE_USER'}
        assert timeline[-1]['status']['message']['role'] == 'ROLE_AGENT'
        # The deprecated history is still populated for older clients.
        assert len(full['history']) == 4
        assert 'timeline' not in none
        assert one['timeline'] == timeline[-1:]

    def test_a_client_written_artifact_is_stored_as_the_clients(self):
        async def scenario(agent, dispatcher):
            created = (await send(
                dispatcher, 'tck-user-artifact-echo',
                artifactUpdates=[{
                    'generation': '9000000000000000000',
                    'artifact': {'artifactId': 'input', 'parts': [{'text': 'client input'}]},
                }],
            )).payload['task']
            # An artifact on its own, to the task that now exists.
            await dispatcher.dispatch(
                Operation.SEND_MESSAGE,
                {'artifactUpdates': [{
                    'taskId': created['id'],
                    'artifact': {'artifactId': 'more', 'parts': [{'text': 'more input'}]},
                }]},
                AT_1_1,
            )
            after = (await dispatcher.dispatch(
                Operation.GET_TASK, {'id': created['id']}, AT_1_1
            )).payload
            orphan = await dispatcher.dispatch(
                Operation.SEND_MESSAGE,
                {'artifactUpdates': [{'artifact': {'artifactId': 'orphan'}}]}, AT_1_1,
            )
            return created, after, orphan

        created, after, orphan = play(scenario)
        (artifact,) = created['artifacts']
        assert artifact['role'] == 'ROLE_USER'
        # The generation is the server's, whatever the client sent.
        assert artifact['startGeneration'] == '3'
        assert created['status']['message']['parts'] == [{'text': 'artifact-received:client input'}]
        assert [a['artifactId'] for a in after['artifacts']] == ['input', 'more']
        assert after['status']['message']['parts'] == [{'text': 'artifact-received:more input'}]
        # Without a message there is no task for the artifact to belong to.
        assert orphan.error.error_type is ErrorType.INVALID_PARAMS

    def test_an_agent_without_the_capability_refuses_client_artifacts(self):
        async def scenario(agent, dispatcher):
            card = (await dispatcher.dispatch(Operation.GET_AGENT_CARD, {}, AT_1_1)).payload
            reply = await send(
                dispatcher, 'tck-complete-task with an artifact',
                artifactUpdates=[{'artifact': {'artifactId': 'input'}}],
            )
            return card, reply

        card, reply = play(scenario, reduced_capabilities=True)
        assert card['capabilities']['userArtifacts'] is False
        assert reply.error.error_type is ErrorType.UNSUPPORTED_OPERATION

    def test_a_subscriber_resuming_is_replayed_what_it_missed(self):
        async def scenario(agent, dispatcher):
            task = (await send(
                dispatcher, 'tck-hold-working 8000 resume',
                configuration={'returnImmediately': True},
            )).payload['task']
            await asyncio.sleep(1)
            seen = (await dispatcher.dispatch(
                Operation.GET_TASK, {'id': task['id']}, AT_1_1
            )).payload['generation']
            await send(
                dispatcher, 'added while away', taskId=task['id'],
                configuration={'returnImmediately': True},
            )
            stream = dispatcher.stream(
                Operation.SUBSCRIBE_TO_TASK,
                {'id': task['id'], 'lastSeenGeneration': seen}, AT_1_1,
            )
            return int(seen), (await collect(stream))[0]

        seen, events = play(scenario)
        # No task snapshot: the client said what it already has.
        assert kinds(events) == ['messageUpdate', 'statusUpdate']
        assert generations(events) == [seen + 1, seen + 2]

    @pytest.mark.parametrize('header', [{VERSION_HEADER: '99.0'}, {VERSION_HEADER: '1.99'}, {}])
    def test_a_version_the_agent_does_not_serve_is_refused(self, header):
        """An absent header means 0.3, which this agent does not serve either."""
        async def scenario(agent, dispatcher):
            return await send(dispatcher, 'tck-complete-task version', headers=header)

        reply = play(scenario)
        assert reply.error.error_type is ErrorType.VERSION_NOT_SUPPORTED

    def test_an_agent_can_be_limited_to_one_version(self):
        async def scenario(agent, dispatcher):
            card = (await dispatcher.dispatch(Operation.GET_AGENT_CARD, {}, AT_1_0)).payload
            return card, await send(dispatcher, 'tck-complete-task at 1.1')

        card, reply = play(scenario, versions=('1.0',))
        assert [i['protocolVersion'] for i in card['supportedInterfaces']] == ['1.0']
        assert 'userArtifacts' not in card['capabilities']
        assert reply.error.error_type is ErrorType.VERSION_NOT_SUPPORTED


class TestWhatTheCorpusDoesNotReachOverJsonRpc:
    """Agent behavior no JSON-RPC test of the mirrored corpus can tell apart
    from its absence, checked here so the fixture is not carrying dead code.

    The extended card's credential check is tested by the corpus over REST
    only. `CORE-LIST-002` asks for a listing without artifacts of a task that
    has none, so it passes whether or not the agent leaves them out.
    """

    def test_the_extended_card_is_shown_only_to_an_authenticated_client(self):
        async def scenario(agent, dispatcher):
            replies = []
            for credential in (None, 'itk-insufficient-token', acts_runner.ACTS_AUTH_TOKEN):
                headers = dict(AT_1_0)
                if credential:
                    headers['Authorization'] = f'Bearer {credential}'
                replies.append(
                    await dispatcher.dispatch(Operation.GET_EXTENDED_AGENT_CARD, {}, headers)
                )
            public = await dispatcher.dispatch(Operation.GET_AGENT_CARD, {}, AT_1_0)
            return replies, public.payload

        (anonymous, insufficient, authenticated), public = play(scenario)
        assert anonymous.status == 401
        assert insufficient.status == 403
        assert authenticated.status == 200
        assert len(authenticated.payload['skills']) == len(public['skills']) + 1
        # The card names the scheme the extended card is guarded with, and
        # does not require it for anything else.
        assert 'bearerAuth' in public['securitySchemes']
        assert 'securityRequirements' not in public

    def test_a_listing_leaves_artifacts_out_unless_asked(self):
        async def scenario(agent, dispatcher):
            task = (await send(dispatcher, 'tck-artifact-text listed', AT_1_0)).payload['task']

            async def listed(**params):
                reply = await dispatcher.dispatch(
                    Operation.LIST_TASKS, {'contextId': task['contextId'], **params}, AT_1_0
                )
                return reply.payload

            return task, await listed(), await listed(includeArtifacts=True)

        task, without, with_artifacts = play(scenario)
        assert task['artifacts']
        assert 'artifacts' not in without['tasks'][0]
        assert with_artifacts['tasks'][0]['artifacts'] == task['artifacts']
        # Always present, and empty on the last page.
        assert without['nextPageToken'] == ''

    def test_a_listing_is_newest_first(self):
        async def scenario(agent, dispatcher):
            context = str(uuid.uuid4())
            sent = [
                (await send(dispatcher, f'tck-complete-task {n}', AT_1_0, contextId=context))
                .payload['task']['id']
                for n in range(3)
            ]
            reply = await dispatcher.dispatch(Operation.LIST_TASKS, {'contextId': context}, AT_1_0)
            return sent, [task['id'] for task in reply.payload['tasks']]

        sent, listed = play(scenario)
        assert listed == sent[::-1]

    def test_a_shortened_history_keeps_the_most_recent_messages(self):
        async def scenario(agent, dispatcher):
            task = (await send(dispatcher, 'tck-complete-task', AT_1_0)).payload['task']
            reply = await dispatcher.dispatch(
                Operation.GET_TASK, {'id': task['id'], 'historyLength': 1}, AT_1_0
            )
            return task['history'], reply.payload['history']

        whole, shortened = play(scenario)
        assert len(whole) > 1
        assert shortened == whole[-1:]

    def test_a_client_that_hangs_up_is_no_longer_sent_events(self):
        """Also one that hangs up before it reads anything."""
        async def scenario(agent, dispatcher):
            watching = dispatcher.stream(
                Operation.SEND_STREAMING_MESSAGE, {'message': message('tck-cancel')}, AT_1_0
            )
            task_id = (await anext(watching)).data['task']['id']
            record = agent._tasks[task_id]
            unread = await agent.handle(httpx.Request(
                'POST', agent.jsonrpc_url, headers=AT_1_0,
                json={
                    'jsonrpc': '2.0', 'id': 1, 'method': 'SubscribeToTask',
                    'params': {'id': task_id},
                },
            ))
            following = [len(record.subscribers)]
            await unread.aclose()
            following.append(len(record.subscribers))
            await watching.aclose()
            # The dispatcher closes its connection a moment after its stream.
            await asyncio.sleep(0.001)
            following.append(len(record.subscribers))
            return following

        assert play(scenario) == [2, 1, 0]


# ---------------------------------------------------------------------------
# Virtual time
# ---------------------------------------------------------------------------


class TestVirtualTime:
    def test_waiting_takes_no_real_time(self):
        async def an_hour() -> float:
            loop = asyncio.get_running_loop()
            assert isinstance(loop, VirtualTimeLoop)
            await asyncio.sleep(3600)
            return loop.time()

        started = time.perf_counter()
        assert run_virtual(an_hour()) == pytest.approx(3600)
        assert time.perf_counter() - started < 5

    def test_everything_ready_runs_before_the_clock_moves(self):
        """What makes the order of events independent of the machine."""
        async def scenario() -> list[str]:
            order: list[str] = []

            async def chain() -> None:
                for step in range(10):
                    await asyncio.sleep(0)
                    time.sleep(0.002)  # Real time passes; the virtual clock stays.
                    order.append(f'ready work {step}')

            background = asyncio.create_task(chain())
            await asyncio.sleep(0.001)
            order.append('a millisecond later')
            await background
            return order

        assert run_virtual(scenario()) == [
            *(f'ready work {step}' for step in range(10)), 'a millisecond later',
        ]

    def test_a_wait_that_can_never_end_is_reported_and_does_not_hang(self):
        async def forever() -> None:
            await asyncio.Event().wait()

        with pytest.raises(RuntimeError, match='will never happen'):
            run_virtual(forever())
