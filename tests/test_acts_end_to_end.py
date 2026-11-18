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
from google.protobuf.json_format import MessageToDict, ParseDict

import acts_runner
from notifications_app import create_notifications_app
from pyproto import a2a_pb2 as pb
from test_suite.acts import load_suite
from test_suite.acts import report as report_writer
from test_suite.acts.dispatcher import JsonRpcDispatcher
from test_suite.acts.runner import VERSION_HEADER, Outcome, Runner, TestResult
from test_suite.acts.schema import ErrorType, Operation, RunnerRequirement, TransportBinding
from tests import acts_fake_agent
from tests.acts_fake_agent import (
    CARD_PATH,
    UNSUPPORTED_OPERATION,
    AgentError,
    FakeAgent,
    VirtualTimeLoop,
    _Stream,
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
                dispatcher_for_version=acts_runner.OtherVersionInterfaces(
                    card, transport, sut_url, spec_version, dispatcher
                ),
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


@pytest.fixture(scope='module')
def whole_corpus_at_1_1(behaviors_path) -> acts_runner.ActsRun:
    """The mirrored corpus at A2A 1.1, run once for the module."""
    return conformance_run(behaviors_path, spec_version='1.1')


# ---------------------------------------------------------------------------
# A conforming agent passes
# ---------------------------------------------------------------------------


class TestAConformingAgentPasses:
    def test_every_test_of_the_corpus_passes(self, whole_corpus):
        """Kept as "nothing but passes" and not as a count: a regression then
        shows up as a named test with its reason."""
        assert not_passed(whole_corpus) == {}

    def test_the_run_covers_every_jsonrpc_test_for_1_0(self, whole_corpus):
        applicable = {
            loaded.id for loaded in load_suite(CORPUS).for_spec_version('1.0')
            if loaded.test.applies_to(TransportBinding.JSONRPC)
        }
        assert {r.id for r in whole_corpus.results} == applicable

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


class TestAConformingAgentPassesAt11:
    """The same corpus, in a run at A2A 1.1: the tests for 1.0 that still
    apply, and the tests that start at 1.1."""

    #: The live exchange tests that target JSON-RPC. The other four are for
    #: gRPC only.
    LIVE = {
        'STREAM-LIVE-001', 'STREAM-LIVE-002', 'STREAM-LIVE-003',
        'STREAM-LIVE-004', 'STREAM-LIVE-009', 'STREAM-LIVE-010',
    }

    def test_every_test_passes(self, whole_corpus_at_1_1):
        assert not_passed(whole_corpus_at_1_1) == {}

    def test_the_live_exchange_tests_are_among_them(self, whole_corpus_at_1_1):
        ran = {r.id for r in whole_corpus_at_1_1.results if r.result is Outcome.PASS}
        assert self.LIVE <= ran

    def test_the_run_covers_every_jsonrpc_test_for_1_1(self, whole_corpus_at_1_1):
        applicable = {
            loaded.id for loaded in load_suite(CORPUS).for_spec_version('1.1')
            if loaded.test.applies_to(TransportBinding.JSONRPC)
        }
        assert {r.id for r in whole_corpus_at_1_1.results} == applicable
        assert self.LIVE < applicable

    def test_it_takes_the_tests_for_1_0_as_well(self, whole_corpus, whole_corpus_at_1_1):
        """Every JSON-RPC test of a run at 1.0 is also in the run at 1.1, and
        passes there too: an agent that serves 1.1 still does what 1.0 asked."""
        at_1_0 = {r.id for r in whole_corpus.results}
        at_1_1 = {r.id: r.result for r in whole_corpus_at_1_1.results}
        assert at_1_0 < set(at_1_1)
        assert all(at_1_1[test_id] is Outcome.PASS for test_id in at_1_0)

    def test_every_step_of_a_passing_test_ran_and_compared_something(
        self, whole_corpus_at_1_1
    ):
        for result in whole_corpus_at_1_1.results:
            assert result.steps, result.id
            assert all(s.result is Outcome.PASS for s in result.steps), result.id
            assert sum(s.checks for s in result.steps) > 0, result.id

    def test_the_deviation_tests_ran_against_the_deviated_agents(
        self, whole_corpus_at_1_1
    ):
        """Two more than at 1.0: the tests that an agent which does not
        advertise `userArtifacts` refuses an artifact from the client, and
        that one which does not advertise `streaming` refuses a live
        exchange."""
        by_configuration: dict[str, set[str]] = {}
        for result in whole_corpus_at_1_1.results:
            if result.configuration:
                by_configuration.setdefault(result.configuration, set()).add(result.id)
        assert by_configuration == {
            acts_runner.REDUCED_CAPABILITIES_ENV: {
                'CORE-CAP-001', 'CORE-CAP-002', 'SEC-EXTCARD-003', 'PUSH-CFG-004',
                'CORE-USERART-001', 'STREAM-LIVE-009',
            },
            acts_runner.AUTH_ENFORCED_ENV: {
                'SEC-AUTH-001', 'SEC-AUTH-002', 'SEC-AUTH-003', 'SEC-AUTH-004',
            },
        }

    def test_the_report_is_conformant(self, whole_corpus_at_1_1):
        report = acts_runner.to_report(whole_corpus_at_1_1, sdk_name='fake-agent')
        assert report['spec_version'] == '1.1'
        assert report['environment']['interface_protocol_version'] == '1.1'
        ran = len(whole_corpus_at_1_1.results)
        assert report['summary']['total'] == report['summary']['passed'] == ran
        assert report['summary']['skipped'] == 0
        assert report['summary']['failed'] == report['summary']['errors'] == 0
        assert report_writer.is_conformant(report)


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

    def test_a_malformed_artifact_update_delivery_is_caught(self, behaviors_path):
        """`PUSH-DELIV-003` is about the format of what is delivered. The task
        it uses produces an artifact, so one delivery is an artifact update.
        The receiver has to store that kind for the test to see it: a
        delivery it refused could be anything."""
        def without_the_artifact_id(agent: FakeAgent) -> None:
            post = agent._post

            def stripped(config, event):
                if event.WhichOneof('payload') == 'artifact_update':
                    copy = pb.StreamResponse()
                    copy.CopyFrom(event)
                    copy.artifact_update.artifact.ClearField('artifact_id')
                    event = copy
                post(config, event)

            agent._post = stripped

        result = self.run_one(behaviors_path, 'PUSH-DELIV-003', without_the_artifact_id)
        assert result.result is Outcome.FAIL
        assert result.failure.step_id == 'delivered-payload-is-a2a-shaped'
        assert 'matched none of the 3 alternatives' in result.failure.message

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
      - id: PROBE-STREAM-GENERATION
        name: A stream reports every change, one generation at a time
        level: must
        min_spec_version: "1.1"
        requires_behaviors: [tck-stream-chunked]
        preconditions:
          capabilities: {streaming: true}
        steps:
          - id: stream
            operation: send_streaming_message
            params:
              message:
                role: ROLE_USER
                parts: [{text: "tck-stream-chunked generation probe"}]
            expect_stream:
              min_count: 3
              ordering: [monotonic_state, contiguous_generation]
              final_event:
                status: {state: TASK_STATE_COMPLETED}
      - id: PROBE-MESSAGE-UPDATE
        name: A client message is reported to a subscriber as a message update
        level: must
        min_spec_version: "1.1"
        requires_behaviors: [tck-hold-working]
        preconditions:
          capabilities: {streaming: true}
        steps:
          - id: start
            operation: send_message
            params:
              message:
                role: ROLE_USER
                parts: [{text: "tck-hold-working 8000 message update probe"}]
              configuration: {returnImmediately: true}
            expect: {status: 200}
            capture: {taskId: task.id}
          - id: working
            operation: get_task
            params: {id: "{{start.taskId}}"}
            expect: {status: 200}
            capture: {generation: generation}
            repeat: {until: "status.state == TASK_STATE_WORKING", max_attempts: 10, delay_ms: 200}
          - id: add-message
            operation: send_message
            params:
              message:
                role: ROLE_USER
                taskId: "{{start.taskId}}"
                parts: [{text: a message while the task is working}]
              configuration: {returnImmediately: true}
            expect: {status: 200}
          - id: resume
            operation: subscribe_to_task
            params:
              id: "{{start.taskId}}"
              lastSeenGeneration: "{{working.generation}}"
            expect_stream:
              timeout_ms: 30000
              ordering: contiguous_generation
              events:
                - index: 0
                  message_update:
                    taskId: "{{start.taskId}}"
                    generation: {gt: "{{working.generation}}"}
                    message:
                      role: ROLE_USER
                      parts: [{text: a message while the task is working}]
              final_event:
                status: {state: TASK_STATE_COMPLETED}
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
        assert set(results) == {
            'PROBE-GENERATION-ADVANCES', 'PROBE-RESUME',
            'PROBE-STREAM-GENERATION', 'PROBE-MESSAGE-UPDATE',
        }

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

    def test_a_change_reported_without_its_own_generation_is_caught(
        self, behaviors_path, probe
    ):
        """An agent that raises the generation once for a chunked artifact and
        reports every chunk at that value."""
        def one_generation_for_all_chunks(agent: FakeAgent) -> None:
            add, advance = agent._add_artifact, agent._advance

            def add_chunk(record, chunk, *, append=False, **kwargs):
                if append:
                    # A further chunk of the same artifact: leave the
                    # generation where the first chunk put it.
                    agent._advance = lambda record: record.task.generation
                try:
                    add(record, chunk, append=append, **kwargs)
                finally:
                    agent._advance = advance

            agent._add_artifact = add_chunk

        result = self.run(
            behaviors_path, probe, one_generation_for_all_chunks
        )['PROBE-STREAM-GENERATION']
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path.endswith('.generation')
        assert 'not greater than' in result.failure.message

    def test_a_1_1_subscriber_served_as_a_1_0_one_is_caught(self, behaviors_path, probe):
        """A client of A2A 1.0 is not sent message updates, so an agent that
        ignores the version a subscriber asked for leaves the update out."""
        def as_1_0(agent: FakeAgent) -> None:
            subscribe = agent.subscribe_to_task
            agent.subscribe_to_task = lambda request, version: subscribe(request, (1, 0))

        result = self.run(behaviors_path, probe, as_1_0)['PROBE-MESSAGE-UPDATE']
        assert result.result is Outcome.FAIL
        assert result.failure.step_id == 'resume'
        # The first event is a status update, so nothing of the message
        # update the test describes is there.
        assert result.failure.assertion_path.startswith('events[0].message_update.')
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
# Versions and timing, end to end
# ---------------------------------------------------------------------------
#
# A step sent as a client of another A2A version, a response that has to be
# held or has to come at once, a stream that has to stay open, and the A2A 1.1
# error for a stale generation.

VERSION_AND_TIMING_PROBE = '''\
acts_version: "1.0"
spec_version: "1.1"
suites:
  - id: version-and-timing-probe
    name: Version and timing probe
    tests:
      - id: PROBE-STALE-GUARD
        name: A send guarded by a stale generation is refused
        level: must
        min_spec_version: "1.1"
        requires_behaviors: [tck-multi-turn]
        steps:
          - id: turn1
            operation: send_message
            params:
              message:
                role: ROLE_USER
                parts: [{text: "tck-multi-turn stale guard probe"}]
            expect: {status: 200}
            capture: {taskId: task.id, generation: task.generation}
          - id: turn2
            operation: send_message
            params:
              message:
                role: ROLE_USER
                taskId: "{{turn1.taskId}}"
                parts: [{text: more input}]
            expect: {status: 200}
          - id: stale
            operation: send_message
            params:
              message:
                role: ROLE_USER
                taskId: "{{turn1.taskId}}"
                parts: [{text: guarded by a stale generation}]
              configuration: {ifGenerationMatch: "{{turn1.generation}}"}
            expect_error: {error_type: TaskGenerationMismatchError}
      - id: PROBE-HELD
        name: A read naming the current generation is held until the task changes
        level: should
        min_spec_version: "1.1"
        requires_behaviors: [tck-hold-working]
        steps:
          - id: start
            operation: send_message
            params:
              message:
                role: ROLE_USER
                parts: [{text: "tck-hold-working 5000 held probe"}]
              configuration: {returnImmediately: true}
            expect: {status: 200}
            capture: {taskId: task.id}
          - id: working
            operation: get_task
            params: {id: "{{start.taskId}}"}
            expect: {status: 200}
            capture: {generation: generation}
            repeat: {until: "status.state == TASK_STATE_WORKING", max_attempts: 10, delay_ms: 200}
          - id: long-poll
            operation: get_task
            params: {id: "{{start.taskId}}", currentGeneration: "{{working.generation}}"}
            min_duration_ms: 1000
            expect:
              status: 200
              body:
                generation: {gte: "{{working.generation}}"}
      - id: PROBE-AT-ONCE
        name: A read that is behind the task is answered at once
        level: must
        min_spec_version: "1.1"
        requires_behaviors: [tck-hold-working]
        steps:
          - id: start
            operation: send_message
            params:
              message:
                role: ROLE_USER
                parts: [{text: "tck-hold-working 8000 at once probe"}]
              configuration: {returnImmediately: true}
            expect: {status: 200}
            capture: {taskId: task.id}
          - id: behind
            operation: get_task
            params: {id: "{{start.taskId}}", currentGeneration: 0}
            max_duration_ms: 3000
            expect: {status: 200}
      - id: PROBE-STAYS-OPEN
        name: A stream stays open while the task waits for input
        level: should
        min_spec_version: "1.1"
        requires_behaviors: [tck-multi-turn]
        preconditions:
          capabilities: {streaming: true}
        steps:
          - id: stream
            operation: send_streaming_message
            params:
              message:
                role: ROLE_USER
                parts: [{text: "tck-multi-turn stays open probe"}]
            expect_stream:
              stays_open_ms: 3000
              final_event:
                status: {state: TASK_STATE_INPUT_REQUIRED}
      - id: PROBE-UNSUPPORTED-VERSION
        name: A version the agent does not serve is refused
        level: must
        min_spec_version: "1.1"
        steps:
          - id: as-1-99
            operation: send_message
            a2a_version: "1.99"
            params:
              message:
                role: ROLE_USER
                parts: [{text: "tck-complete-task unsupported version probe"}]
            expect_error: {error_type: VersionNotSupportedError}
      - id: PROBE-SERVED-AS-1-0
        name: An agent that lists a 1.0 interface serves a request sent as 1.0
        level: must
        min_spec_version: "1.1"
        requires_behaviors: [tck-multi-turn]
        preconditions:
          protocol_versions: ["1.0"]
          capabilities: {streaming: true}
        steps:
          - id: as-1-0
            operation: send_streaming_message
            a2a_version: "1.0"
            params:
              message:
                role: ROLE_USER
                parts: [{text: "tck-multi-turn served as 1.0 probe"}]
            expect_stream:
              timeout_ms: 10000
              final_event:
                status: {state: TASK_STATE_INPUT_REQUIRED}
'''


class TestVersionsAndTimingEndToEnd:
    @pytest.fixture
    def probe(self, tmp_path) -> Path:
        path = tmp_path / 'version-and-timing.acts.yaml'
        path.write_text(VERSION_AND_TIMING_PROBE, encoding='utf-8')
        return path

    def run(self, behaviors_path, probe, customize=None, **kwargs) -> dict[str, TestResult]:
        run = conformance_run(
            behaviors_path, spec_version='1.1', suite_path=probe,
            customize=customize, **kwargs,
        )
        return {r.id: r for r in run.results}

    def test_a_conforming_agent_passes(self, behaviors_path, probe):
        results = self.run(behaviors_path, probe)
        assert {
            r.id: r.failure.message if r.failure else r.skip_reason
            for r in results.values() if r.result is not Outcome.PASS
        } == {}
        assert len(results) == 6

    def test_an_agent_that_ignores_the_generation_guard_is_caught(
        self, behaviors_path, probe
    ):
        def ignore_the_guard(agent: FakeAgent) -> None:
            accept = agent._accept

            def unguarded(request):
                request.configuration.ClearField('if_generation_match')
                return accept(request)

            agent._accept = unguarded

        result = self.run(behaviors_path, probe, ignore_the_guard)['PROBE-STALE-GUARD']
        assert result.result is Outcome.FAIL
        assert result.failure.step_id == 'stale'
        assert 'expected an error, but the call succeeded' in result.failure.message

    def test_an_agent_that_does_not_hold_a_long_poll_is_caught(
        self, behaviors_path, probe
    ):
        def answer_at_once(agent: FakeAgent) -> None:
            agent.long_poll_timeout_s = 0

        result = self.run(behaviors_path, probe, answer_at_once)['PROBE-HELD']
        assert result.result is Outcome.FAIL
        assert result.failure.step_id == 'long-poll'
        # The response itself is in order: an agent may answer a long poll
        # early with the unchanged task. Only its timing shows it never held.
        assert result.failure.assertion_path == 'duration'
        assert 'requires that it take at least 1000ms' in result.failure.message

    def test_an_agent_that_holds_a_read_it_should_answer_is_caught(
        self, behaviors_path, probe
    ):
        """One that waits for the next change whatever generation the client
        has. The task changes nothing for eight seconds."""
        def always_wait_for_a_change(agent: FakeAgent) -> None:
            get_task = agent.get_task

            async def waiting(request):
                if request.HasField('current_generation'):
                    record = agent._find(request.id)
                    at = record.task.generation
                    await agent._until(record, lambda: record.task.generation > at)
                return await get_task(request)

            agent.get_task = waiting

        result = self.run(
            behaviors_path, probe, always_wait_for_a_change
        )['PROBE-AT-ONCE']
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path == 'duration'
        assert 'requires that it take at most 3000ms' in result.failure.message

    def test_an_agent_that_closes_a_stream_at_input_required_is_caught(
        self, behaviors_path, probe
    ):
        """As A2A 1.0 had it, and as 1.1 no longer does."""
        def as_1_0(agent: FakeAgent) -> None:
            stream = agent.send_streaming_message
            agent.send_streaming_message = lambda request, version: stream(request, (1, 0))

        result = self.run(behaviors_path, probe, as_1_0)['PROBE-STAYS-OPEN']
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path == 'stream'
        assert 'the SUT closed the stream' in result.failure.message

    def test_an_agent_that_serves_any_version_is_caught(self, behaviors_path, probe):
        def serve_anything(agent: FakeAgent) -> None:
            agent._negotiate = lambda header: (1, 1)

        result = self.run(behaviors_path, probe, serve_anything)['PROBE-UNSUPPORTED-VERSION']
        assert result.result is Outcome.FAIL
        assert 'expected an error, but the call succeeded' in result.failure.message

    def test_the_step_reaches_the_agent_as_the_version_it_names(
        self, behaviors_path, probe
    ):
        """Served as 1.0, the stream ends when the task waits for input. Sent
        as the run's 1.1 it would stay open, and the step would time out."""
        seen: list[str | None] = []

        def record_versions(agent: FakeAgent) -> None:
            negotiate = agent._negotiate

            def recording(header):
                seen.append(header)
                return negotiate(header)

            agent._negotiate = recording

        results = self.run(behaviors_path, probe, record_versions)
        assert results['PROBE-SERVED-AS-1-0'].result is Outcome.PASS
        assert results['PROBE-UNSUPPORTED-VERSION'].result is Outcome.PASS
        assert seen.count('1.0') == 1
        assert seen.count('1.99') == 1
        assert set(seen) == {'1.0', '1.1', '1.99'}

    def test_a_test_for_a_version_the_agent_does_not_serve_is_skipped(
        self, behaviors_path, probe
    ):
        """An agent that serves only 1.1 owes a 1.0 request nothing but a
        refusal, so the test does not apply to it."""
        results = self.run(behaviors_path, probe, versions=('1.1',))
        skipped_test = results['PROBE-SERVED-AS-1-0']
        assert skipped_test.result is Outcome.SKIP
        assert skipped_test.skip_reason == (
            'agent card lists no jsonrpc interface for A2A 1.0; it lists 1.1'
        )
        assert results['PROBE-UNSUPPORTED-VERSION'].result is Outcome.PASS


# ---------------------------------------------------------------------------
# Background steps, end to end
# ---------------------------------------------------------------------------
#
# A request that stays open while the steps after it run: a subscription that
# is being read while a message is sent to its task, a read that is held until
# another request changes the task, and a stream whose first event names the
# task the later steps act on.

BACKGROUND_PROBE = '''\
acts_version: "1.0"
spec_version: "1.1"
suites:
  - id: background-probe
    name: Background probe
    tests:
      - id: PROBE-WATCHED
        name: A message sent while a subscription is open reaches the subscriber
        level: must
        min_spec_version: "1.1"
        requires_behaviors: [tck-hold-working]
        runner_requirements: [concurrent_requests]
        preconditions:
          capabilities: {streaming: true}
        steps:
          - id: start
            operation: send_message
            params:
              message:
                role: ROLE_USER
                parts: [{text: "tck-hold-working 8000 watched probe"}]
              configuration: {returnImmediately: true}
            expect: {status: 200}
            capture: {taskId: task.id}
          - id: watch
            operation: subscribe_to_task
            background: true
            params: {id: "{{start.taskId}}"}
            expect_stream:
              timeout_ms: 30000
              events:
                - match: any_position
                  message_update:
                    taskId: "{{start.taskId}}"
                    message:
                      role: ROLE_USER
                      parts: [{text: a message while the subscription is open}]
              final_event:
                status: {state: TASK_STATE_COMPLETED}
          - id: add-message
            operation: send_message
            params:
              message:
                role: ROLE_USER
                taskId: "{{start.taskId}}"
                parts: [{text: a message while the subscription is open}]
              configuration: {returnImmediately: true}
            expect: {status: 200}
      - id: PROBE-WOKEN
        name: A held read returns when another request changes the task
        level: should
        min_spec_version: "1.1"
        requires_behaviors: [tck-multi-turn]
        runner_requirements: [concurrent_requests]
        steps:
          - id: start
            operation: send_message
            params:
              message:
                role: ROLE_USER
                parts: [{text: "tck-multi-turn woken probe"}]
            expect: {status: 200}
            capture: {taskId: task.id, generation: task.generation}
          - id: long-poll
            operation: get_task
            background: true
            params:
              id: "{{start.taskId}}"
              currentGeneration: "{{start.generation}}"
            min_duration_ms: 300
            expect:
              status: 200
              body:
                generation: {gt: "{{start.generation}}"}
          - id: answer
            delay_ms: 500
            operation: send_message
            params:
              message:
                role: ROLE_USER
                taskId: "{{start.taskId}}"
                parts: [{text: done}]
            expect: {status: 200}
      - id: PROBE-STREAM-NAMES-THE-TASK
        name: A stream stays open while the client answers the task it opened
        level: should
        min_spec_version: "1.1"
        requires_behaviors: [tck-stream-input-required]
        runner_requirements: [concurrent_requests]
        preconditions:
          capabilities: {streaming: true}
        steps:
          - id: stream
            operation: send_streaming_message
            background: true
            params:
              message:
                role: ROLE_USER
                parts: [{text: "tck-stream-input-required background probe"}]
            capture: {taskId: task.id}
            expect_stream:
              timeout_ms: 30000
              ordering: monotonic_state
              final_event:
                status: {state: TASK_STATE_COMPLETED}
          - id: wait
            operation: get_task
            params: {id: "{{stream.taskId}}"}
            expect: {status: 200}
            repeat:
              until: status.state == TASK_STATE_INPUT_REQUIRED
              max_attempts: 15
              delay_ms: 1000
          - id: answer
            operation: send_message
            params:
              message:
                role: ROLE_USER
                taskId: "{{stream.taskId}}"
                parts: [{text: the requested input}]
            expect: {status: 200}
'''


class TestBackgroundStepsEndToEnd:
    @pytest.fixture
    def probe(self, tmp_path) -> Path:
        path = tmp_path / 'background.acts.yaml'
        path.write_text(BACKGROUND_PROBE, encoding='utf-8')
        return path

    def run(self, behaviors_path, probe, customize=None) -> dict[str, TestResult]:
        run = conformance_run(
            behaviors_path, spec_version='1.1', suite_path=probe, customize=customize
        )
        return {r.id: r for r in run.results}

    def test_a_conforming_agent_passes(self, behaviors_path, probe):
        """Run through the pipeline, so this also shows that the pipeline
        grants the runner requirement the tests declare."""
        results = self.run(behaviors_path, probe)
        assert {
            r.id: r.failure.message if r.failure else r.skip_reason
            for r in results.values() if r.result is not Outcome.PASS
        } == {}
        assert len(results) == 3

    def test_every_step_is_reported_in_the_order_the_test_writes_it(
        self, behaviors_path, probe
    ):
        results = self.run(behaviors_path, probe)
        assert [s.id for s in results['PROBE-WATCHED'].steps] == [
            'start', 'watch', 'add-message',
        ]
        # The subscription was compared with something.
        watch = results['PROBE-WATCHED'].steps[1]
        assert watch.checks > 0

    def test_a_subscriber_that_is_not_sent_the_message_is_caught(
        self, behaviors_path, probe
    ):
        def as_1_0(agent: FakeAgent) -> None:
            subscribe = agent.subscribe_to_task
            agent.subscribe_to_task = lambda request, version: subscribe(request, (1, 0))

        result = self.run(behaviors_path, probe, as_1_0)['PROBE-WATCHED']
        assert result.result is Outcome.FAIL
        # Against the subscription, which is the step whose stream lacks it,
        # and not against the step that sent the message.
        assert result.failure.step_id == 'watch'
        assert result.failure.message == 'events[0]: no event among the 2 received matched'

    def test_a_read_that_is_not_held_is_caught(self, behaviors_path, probe):
        def answer_at_once(agent: FakeAgent) -> None:
            agent.long_poll_timeout_s = 0

        result = self.run(behaviors_path, probe, answer_at_once)['PROBE-WOKEN']
        assert result.result is Outcome.FAIL
        assert result.failure.step_id == 'long-poll'
        # Answered before the second request, so with the unchanged task.
        assert result.failure.assertion_path == 'body.generation'

    def test_a_read_that_the_change_does_not_wake_is_caught(self, behaviors_path, probe):
        """An agent that holds the read for its full patience whatever
        happens to the task, and answers with what it had at the start."""
        def sleep_through(agent: FakeAgent) -> None:
            get_task = agent.get_task

            async def stale(request):
                if not request.HasField('current_generation'):
                    return await get_task(request)
                before = agent._view(agent._find(request.id))
                await asyncio.sleep(agent.long_poll_timeout_s)
                return before

            agent.get_task = stale

        result = self.run(behaviors_path, probe, sleep_through)['PROBE-WOKEN']
        assert result.result is Outcome.FAIL
        assert result.failure.step_id == 'long-poll'
        assert result.failure.assertion_path == 'body.generation'

    def test_a_stream_that_closes_when_the_task_waits_is_caught(
        self, behaviors_path, probe
    ):
        def as_1_0(agent: FakeAgent) -> None:
            stream = agent.send_streaming_message
            agent.send_streaming_message = lambda request, version: stream(request, (1, 0))

        result = self.run(behaviors_path, probe, as_1_0)['PROBE-STREAM-NAMES-THE-TASK']
        assert result.result is Outcome.FAIL
        assert result.failure.step_id == 'stream'
        assert result.failure.assertion_path == 'final_event.status.state'
        # The steps that used what the stream captured ran all the same.
        assert [(s.id, s.result) for s in result.steps] == [
            ('stream', Outcome.FAIL), ('wait', Outcome.PASS), ('answer', Outcome.PASS),
        ]


# ---------------------------------------------------------------------------
# Deliveries read up to a named one, and a client fed a stream
# ---------------------------------------------------------------------------
#
# Two things a test could not do before. Assert on every delivery up to a
# given one, where a webhook step used to stop at the first. And have the
# agent's own client read a stream of events, where a client step used to
# carry one payload.

DELIVERY_AND_CLIENT_STREAM_PROBE = '''\
acts_version: "1.0"
spec_version: "1.1"
suites:
  - id: delivery-and-client-stream-probe
    name: Delivery and client stream probe
    tests:
      - id: PROBE-NOT-DELIVERED
        name: A configuration created for A2A 1.0 is not delivered a message update
        level: may
        min_spec_version: "1.1"
        requires_behaviors: [tck-multi-turn]
        runner_requirements: [webhook_endpoint]
        preconditions:
          capabilities: {pushNotifications: true}
        steps:
          - id: create
            operation: send_message
            params:
              message:
                role: ROLE_USER
                parts: [{text: "tck-multi-turn delivery probe"}]
            expect: {status: 200}
            capture: {taskId: task.id}
          - id: set-config
            operation: create_push_config
            params:
              taskId: "{{create.taskId}}"
              url: "{{webhookUrl}}"
              protocolVersion: "1.0"
            expect: {status: 200}
          - id: answer
            operation: send_message
            params:
              message:
                role: ROLE_USER
                taskId: "{{create.taskId}}"
                parts: [{text: done}]
            expect: {status: 200}
          - id: no-message-update
            expect_webhook:
              task_id: "{{create.taskId}}"
              events:
                - description: Deliveries are read up to the one that reports the completed task
                  match: any_position
                  status_update:
                    taskId: "{{create.taskId}}"
                    status: {state: TASK_STATE_COMPLETED}
              each_event:
                message_update: {absent: true}
      - id: PROBE-CLIENT-SKIPS-UNKNOWN
        name: The client skips a stream event of a kind it does not know
        level: must
        min_spec_version: "1.1"
        steps:
          - id: parse
            client_response:
              operation: send_streaming_message
              wire_stream:
                - jsonrpc: "2.0"
                  id: "1"
                  result:
                    task: {id: task-1, generation: "2"}
                - jsonrpc: "2.0"
                  id: "1"
                  result:
                    futureEvent: {taskId: task-1}
                - jsonrpc: "2.0"
                  id: "1"
                  result:
                    statusUpdate:
                      taskId: task-1
                      status: {state: TASK_STATE_COMPLETED}
                      generation: "3"
            expect_parsed:
              events:
                type: array
                count: 2
                items:
                  - task: {id: task-1}
                  - status_update:
                      status: {state: TASK_STATE_COMPLETED}
                      generation: {gte: 3, lte: 3}
      - id: PROBE-CLIENT-READS-A-LIVE-DOWNLINK
        name: The client reads a rejected request on a live exchange and goes on
        level: must
        min_spec_version: "1.1"
        steps:
          - id: parse
            client_response:
              operation: send_live_message
              wire_stream:
                - jsonrpc: "2.0"
                  id: "1"
                  result:
                    task: {id: task-2, generation: "2"}
                - jsonrpc: "2.0"
                  id: "1"
                  result:
                    error: {taskId: task-2, code: INVALID_ARGUMENT, referenceId: m-1}
                - jsonrpc: "2.0"
                  id: "1"
                  result:
                    statusUpdate:
                      taskId: task-2
                      status: {state: TASK_STATE_COMPLETED}
                      generation: "3"
            expect_parsed:
              events:
                count: 3
                items:
                  - task: {id: task-2}
                  - error: {code: INVALID_ARGUMENT, referenceId: m-1}
                  - status_update: {taskId: task-2}
'''


class TestDeliveriesAndClientStreamsEndToEnd:
    @pytest.fixture
    def probe(self, tmp_path) -> Path:
        path = tmp_path / 'delivery-and-client-stream.acts.yaml'
        path.write_text(DELIVERY_AND_CLIENT_STREAM_PROBE, encoding='utf-8')
        return path

    def run(self, behaviors_path, probe, customize=None) -> dict[str, TestResult]:
        run = conformance_run(
            behaviors_path, spec_version='1.1', suite_path=probe, customize=customize
        )
        return {r.id: r for r in run.results}

    def test_a_conforming_agent_passes(self, behaviors_path, probe):
        results = self.run(behaviors_path, probe)
        assert {
            r.id: r.failure.message if r.failure else r.skip_reason
            for r in results.values() if r.result is not Outcome.PASS
        } == {}
        assert len(results) == 3

    def test_an_agent_that_delivers_a_message_update_to_a_1_0_configuration_is_caught(
        self, behaviors_path, probe
    ):
        """The message update is delivered after the first status update, so a
        step that stopped at the first delivery would not see it. The receiver
        has to store it as well: one it refused could not be asserted on."""
        def deliver_every_kind(agent: FakeAgent) -> None:
            create = agent.create_push_config

            def as_1_1(config, version):
                stored = create(config, version)
                answer = pb.TaskPushNotificationConfig()
                answer.CopyFrom(stored)
                # Reports the version that was asked for, and delivers as 1.1.
                stored.protocol_version = '1.1'
                return answer

            agent.create_push_config = as_1_1

        result = self.run(behaviors_path, probe, deliver_every_kind)['PROBE-NOT-DELIVERED']
        assert result.result is Outcome.FAIL
        assert result.failure.step_id == 'no-message-update'
        assert result.failure.assertion_path.startswith('webhook.each_event[')
        assert result.failure.assertion_path.endswith('.message_update')

    def test_an_agent_that_never_reports_the_completed_task_is_caught(
        self, behaviors_path, probe
    ):
        """The delivery the step reads up to never comes."""
        def stop_delivering_at_the_end(agent: FakeAgent) -> None:
            post = agent._post

            def all_but_completed(config, event):
                if event.status_update.status.state != pb.TASK_STATE_COMPLETED:
                    post(config, event)

            agent._post = all_but_completed

        result = self.run(
            behaviors_path, probe, stop_delivering_at_the_end
        )['PROBE-NOT-DELIVERED']
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path == 'webhook.events[*]'
        assert 'read deliveries for 10000ms' in result.failure.message

    def test_a_client_that_fails_on_an_unknown_event_is_caught(
        self, behaviors_path, probe, monkeypatch
    ):
        def strict(operation, wire_stream):
            """Parses every event, and raises on a member it does not know."""
            return {'events': [
                MessageToDict(ParseDict(item['result'], pb.StreamResponse()))
                for item in wire_stream
            ]}

        monkeypatch.setattr(acts_fake_agent, '_client_read_stream', strict)
        results = self.run(behaviors_path, probe)
        result = results['PROBE-CLIENT-SKIPS-UNKNOWN']
        assert result.result is Outcome.FAIL
        assert result.failure.step_id == 'parse'
        # The same client reads a stream of kinds it knows.
        assert results['PROBE-CLIENT-READS-A-LIVE-DOWNLINK'].result is Outcome.PASS

    def test_a_client_that_ends_the_stream_at_a_rejection_is_caught(
        self, behaviors_path, probe, monkeypatch
    ):
        read = acts_fake_agent._client_read_stream

        def stop_at_error(operation, wire_stream):
            surfaced = read(operation, wire_stream)['events']
            at = next(
                (i for i, event in enumerate(surfaced) if 'error' in event), len(surfaced)
            )
            return {'events': surfaced[: at + 1]}

        monkeypatch.setattr(acts_fake_agent, '_client_read_stream', stop_at_error)
        results = self.run(behaviors_path, probe)
        result = results['PROBE-CLIENT-READS-A-LIVE-DOWNLINK']
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path == 'parsed.events'
        # A stream without a rejection is read to its end all the same.
        assert results['PROBE-CLIENT-SKIPS-UNKNOWN'].result is Outcome.PASS


# ---------------------------------------------------------------------------
# Live exchanges, end to end
# ---------------------------------------------------------------------------
#
# Over JSON-RPC a live exchange is several calls: one that opens it and is
# answered with the event stream, and one for each later request, answered
# with the task. The corpus tests of it run above with the rest of the corpus.
# Here they run against agents that get the exchange wrong.

LIVE_REJECTION_PROBE = '''\
acts_version: "1.0"
spec_version: "1.1"
suites:
  - id: live-rejection-probe
    name: Live rejection probe
    tests:
      - id: PROBE-LIVE-DECLINED
        name: A declined message is reported on the downlink and changes nothing
        level: should
        min_spec_version: "1.1"
        requires_behaviors: [tck-live-reject]
        runner_requirements: [live_stream]
        preconditions:
          capabilities: {streaming: true}
        steps:
          - id: exchange
            live:
              open:
                send_message:
                  message:
                    role: ROLE_USER
                    parts: [{text: "tck-live-reject probe"}]
              uplink:
                - id: declined
                  send_message:
                    message:
                      role: ROLE_USER
                      messageId: probe-declined
                      taskId: "{{exchange.taskId}}"
                      parts: [{text: "reject this one"}]
                  expect_ack:
                    id: "{{exchange.taskId}}"
                - id: accepted
                  after_event:
                    error: {referenceId: probe-declined}
                  send_message:
                    message:
                      role: ROLE_USER
                      taskId: "{{exchange.taskId}}"
                      parts: [{text: "accepted after the rejection"}]
                - id: finish
                  after_event:
                    status_update:
                      status:
                        message:
                          parts: [{text: "live-echo:1"}]
                  send_message:
                    message:
                      role: ROLE_USER
                      taskId: "{{exchange.taskId}}"
                      parts: [{text: done}]
            capture: {taskId: task.id}
            expect_stream:
              timeout_ms: 30000
              ordering: [monotonic_state, contiguous_generation]
              events:
                - description: The rejection names the message it refers to
                  match: any_position
                  error:
                    referenceId: probe-declined
                    code: {type: string}
              final_event:
                status: {state: TASK_STATE_COMPLETED}
'''


class TestLiveExchangesEndToEnd:
    def run_one(self, behaviors_path, test_id, customize=None, **kwargs) -> TestResult:
        run = conformance_run(
            behaviors_path, spec_version='1.1', test_ids=[test_id],
            customize=customize, **kwargs,
        )
        (result,) = run.results
        return result

    def test_every_step_of_an_exchange_is_counted(self, behaviors_path):
        """The acknowledgment, the events and the final event of
        `STREAM-LIVE-003` are each compared with something."""
        result = self.run_one(behaviors_path, 'STREAM-LIVE-003')
        assert result.result is Outcome.PASS, result.failure
        (exchange,) = result.steps
        assert exchange.checks >= 5

    def test_an_agent_that_answers_a_later_request_with_events_is_caught(
        self, behaviors_path
    ):
        """It takes every call for one that opens an exchange. The message
        still reaches the task, so the downlink looks right; what is wrong
        is the answer to the call."""
        def always_open(agent: FakeAgent) -> None:
            def as_a_new_exchange(record, send):
                agent._hand_over(agent._accept(send), send)
                return agent._follow(record, (1, 1), [agent._snapshot(record)], live=True)

            agent._live_uplink = as_a_new_exchange

        result = self.run_one(behaviors_path, 'STREAM-LIVE-003', always_open)
        assert result.result is Outcome.FAIL
        assert result.failure.step_id == 'exchange'
        assert result.failure.assertion_path == "uplink['acknowledged']"
        assert 'with an event stream' in result.failure.message

    def test_an_agent_that_acknowledges_with_another_task_is_caught(self, behaviors_path):
        def another_task(agent: FakeAgent) -> None:
            uplink = agent._live_uplink

            def renamed(record, send):
                task = uplink(record, send)
                task.id = 'some-other-task'
                return task

            agent._live_uplink = renamed

        result = self.run_one(behaviors_path, 'STREAM-LIVE-003', another_task)
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path == "uplink['acknowledged'].ack.id"

    def test_an_agent_that_does_not_answer_on_the_downlink_is_caught(self, behaviors_path):
        """It takes the messages and echoes none. The request held for the
        second echo is never sent, and the step says so."""
        def silent(agent: FakeAgent) -> None:
            async def never_echoes(record, request):
                agent._set_status(record, pb.TASK_STATE_WORKING)
                while True:
                    request = await agent._next_request(record)
                    if request.message.parts[0].text == 'done':
                        agent._set_status(record, pb.TASK_STATE_COMPLETED)
                        return

            agent.behaviors['tck-live-echo'] = never_echoes

        result = self.run_one(behaviors_path, 'STREAM-LIVE-002', silent)
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path == "uplink['finish']"
        assert result.failure.message.startswith(
            "uplink request 'finish' was never sent: no downlink event matched "
            "its `after_event`"
        )

    def test_a_downlink_that_does_not_open_with_the_task_is_caught(self, behaviors_path):
        """The requests of the test name the task the first event gives."""
        def no_snapshot(agent: FakeAgent) -> None:
            follow = agent._follow

            def without_first(record, version, first, *, live=False):
                return follow(record, version, [] if live else first, live=live)

            agent._follow = without_first

        result = self.run_one(behaviors_path, 'STREAM-LIVE-001', no_snapshot)
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path == 'capture'
        assert "cannot capture 'taskId'" in result.failure.message

    def test_an_agent_that_opens_an_exchange_for_a_task_it_does_not_have_is_caught(
        self, behaviors_path
    ):
        def any_task(agent: FakeAgent) -> None:
            subscribe = agent.subscribe_to_task

            def lenient(request, version, *, live=False):
                if request.id not in agent._tasks:
                    return _Stream([], None, lambda: None)
                return subscribe(request, version, live=live)

            agent.subscribe_to_task = lenient

        result = self.run_one(behaviors_path, 'STREAM-LIVE-010', any_task)
        assert result.result is Outcome.FAIL
        assert 'expected an error, but the stream opened' in result.failure.message

    # -- a request the agent declines ------------------------------------------------

    @pytest.fixture
    def probe(self, tmp_path) -> Path:
        path = tmp_path / 'live-rejection.acts.yaml'
        path.write_text(LIVE_REJECTION_PROBE, encoding='utf-8')
        return path

    def test_a_declined_message_is_reported_and_the_exchange_goes_on(
        self, behaviors_path, probe
    ):
        """Over JSON-RPC, where this agent reports the rejection on the
        downlink and still answers the call with the task. The corpus tests
        of rejections are for gRPC only, because A2A leaves it to each
        binding how a rejection reaches the caller."""
        result = self.run_one(behaviors_path, 'PROBE-LIVE-DECLINED', suite_path=probe)
        assert result.result is Outcome.PASS, result.failure

    def test_an_agent_that_never_reports_the_rejection_is_caught(
        self, behaviors_path, probe
    ):
        def swallow(agent: FakeAgent) -> None:
            agent._live_uplink = lambda record, send: agent._view(record)

        result = self.run_one(
            behaviors_path, 'PROBE-LIVE-DECLINED', swallow, suite_path=probe
        )
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path == "uplink['accepted']"

    def test_a_rejection_that_moves_the_task_on_is_caught(self, behaviors_path, probe):
        """A rejected request does not change the task, so the generation
        must not move: the events after it would otherwise skip a value."""
        def counts_it(agent: FakeAgent) -> None:
            uplink = agent._live_uplink

            def advancing(record, send):
                if send.message.message_id == 'probe-declined':
                    agent._advance(record)
                return uplink(record, send)

            agent._live_uplink = advancing

        result = self.run_one(
            behaviors_path, 'PROBE-LIVE-DECLINED', counts_it, suite_path=probe
        )
        assert result.result is Outcome.FAIL
        assert 'generation' in result.failure.message


# ---------------------------------------------------------------------------
# What the fake agent does at A2A 1.1
# ---------------------------------------------------------------------------
#
# These read the agent's 1.1 behavior through the real dispatcher, one piece
# at a time. They are about the fixture: they say what a corpus test for 1.1
# is run against, apart from whether any corpus test happens to reach it.

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
