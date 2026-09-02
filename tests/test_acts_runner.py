"""The runner: sequencing, gating, polling and the result model.

Driven against a scripted dispatcher rather than a server, so every assertion
is about what the *runner* decided given a reply, not about any transport.
Tests are sync and call ``asyncio.run``, matching the rest of the suite — the
repo has no async pytest plugin.

The distinctions worth guarding are the ones a report depends on: fail means
the SUT was wrong, error means the run was, and skip means neither. Getting
those confused would put our own bugs into somebody else's conformance record.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

import pytest

from test_suite.acts import load_suite
from test_suite.acts.dispatcher.base import (
    DispatchError,
    Dispatcher,
    StreamEvent,
    WireError,
    WireResponse,
)
from test_suite.acts.runner import (
    DEFAULT_DELAY_MS,
    DEFAULT_MAX_ATTEMPTS,
    VERSION_HEADER,
    FailureDetail,
    Outcome,
    RunError,
    Runner,
    is_conformant,
    summarize,
)
from test_suite.acts.schema import (
    ErrorType,
    Level,
    Operation,
    RawBlock,
    RunnerRequirement,
    Step,
    Test,
    TransportBinding,
)


CORPUS = (
    Path(__file__).resolve().parent.parent / 'scenarios' / 'acts' / 'suite.acts.yaml'
)


@pytest.fixture(scope='module')
def suite():
    return load_suite(CORPUS)


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


class FakeDispatcher(Dispatcher):
    """Replays scripted replies and records what it was asked for."""

    def __init__(
        self,
        replies: Any = None,
        *,
        binding: TransportBinding = TransportBinding.JSONRPC,
        raises: Exception | None = None,
    ) -> None:
        self.binding = binding
        if replies is None:
            replies = WireResponse(status=200, payload={})
        self._replies = replies if isinstance(replies, list) else [replies]
        self._raises = raises
        self.calls: list[tuple[Operation, Mapping[str, Any], Mapping[str, str]]] = []

    async def dispatch(self, operation, params=None, headers=None) -> WireResponse:
        self.calls.append((operation, dict(params or {}), dict(headers or {})))
        if self._raises is not None:
            raise self._raises
        index = min(len(self.calls) - 1, len(self._replies) - 1)
        return self._replies[index]

    async def dispatch_raw(self, raw, headers=None) -> WireResponse:  # pragma: no cover
        raise AssertionError('the non-raw path must not dispatch raw requests')

    def stream(self, operation, params=None, headers=None) -> AsyncIterator[StreamEvent]:
        raise AssertionError('the non-streaming path must not stream')


def ok(payload: Any = None, status: int = 200) -> WireResponse:
    return WireResponse(status=status, payload=payload if payload is not None else {})


def failed(
    error_type: ErrorType | None = ErrorType.TASK_NOT_FOUND,
    *,
    status: int = 404,
    message: str = 'no such task',
    **kwargs: Any,
) -> WireResponse:
    return WireResponse(
        status=status,
        error=WireError(message=message, error_type=error_type, **kwargs),
    )


def a_test(*steps: Step, **kwargs: Any) -> Test:
    fields: dict[str, Any] = {
        'id': 'T-001',
        'name': 'a test',
        'level': Level.MUST,
        'steps': list(steps),
    }
    fields.update(kwargs)
    return Test(**fields)


def get_task(step_id: str = 'get', **kwargs: Any) -> Step:
    return Step(id=step_id, operation=Operation.GET_TASK, params={'id': 'T1'}, **kwargs)


def run(runner: Runner, test: Test, **kwargs: Any):
    return asyncio.run(runner.run_test(test, **kwargs))


def runner_for(dispatcher: FakeDispatcher, **kwargs: Any) -> Runner:
    """A runner whose sleeps are instant and whose clock does not advance."""
    kwargs.setdefault('sleep', _record_sleep([]))
    kwargs.setdefault('clock', lambda: 0.0)
    kwargs.setdefault('new_uuid', lambda: 'generated-id')
    return Runner(dispatcher, **kwargs)


def _record_sleep(into: list[float]):
    async def sleep(seconds: float) -> None:
        into.append(seconds)

    return sleep


# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_a_passing_step(self):
        dispatcher = FakeDispatcher(ok({'id': 'T1'}))
        result = run(
            runner_for(dispatcher),
            a_test(get_task(expect={'status': 200, 'body': {'id': 'T1'}})),
        )
        assert result.result is Outcome.PASS
        assert result.steps[0].result is Outcome.PASS

    def test_a_body_mismatch_fails_and_locates_itself(self):
        dispatcher = FakeDispatcher(ok({'id': 'OTHER'}))
        result = run(
            runner_for(dispatcher),
            a_test(get_task(expect={'body': {'id': 'T1'}})),
        )
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path == 'body.id'
        assert result.failure.step_id == 'get'

    def test_a_status_mismatch_fails(self):
        dispatcher = FakeDispatcher(ok({}, status=500))
        result = run(runner_for(dispatcher), a_test(get_task(expect={'status': 200})))
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path == 'status'

    def test_steps_run_in_order_and_share_a_scope(self):
        dispatcher = FakeDispatcher([ok({'task': {'id': 'T9'}}), ok({'id': 'T9'})])
        test = a_test(
            Step(
                id='send',
                operation=Operation.SEND_MESSAGE,
                params={'message': {'role': 'ROLE_USER', 'parts': [{'text': 'hi'}]}},
                capture={'taskId': 'task.id'},
            ),
            Step(
                id='get',
                operation=Operation.GET_TASK,
                params={'id': '{{send.taskId}}'},
                expect={'body': {'id': '{{send.taskId}}'}},
            ),
        )
        assert run(runner_for(dispatcher), test).result is Outcome.PASS
        assert dispatcher.calls[1][1] == {'id': 'T9'}

    def test_a_failing_step_stops_the_test(self):
        """Later steps read this one's captures; continuing only adds noise."""
        dispatcher = FakeDispatcher(ok({'id': 'WRONG'}))
        test = a_test(
            get_task('first', expect={'body': {'id': 'T1'}}),
            get_task('second', expect={'body': {'id': 'T1'}}),
        )
        result = run(runner_for(dispatcher), test)
        assert result.result is Outcome.FAIL
        assert [s.id for s in result.steps] == ['first']


class TestScopeIsolation:
    def test_each_test_gets_a_fresh_scope(self):
        dispatcher = FakeDispatcher([ok({'task': {'id': 'T1'}}), ok({'id': 'T1'})])
        runner = runner_for(dispatcher)
        capturing = a_test(
            Step(
                id='send',
                operation=Operation.SEND_MESSAGE,
                params={'message': {'role': 'ROLE_USER'}},
                capture={'taskId': 'task.id'},
            )
        )
        assert run(runner, capturing).result is Outcome.PASS

        borrowing = a_test(
            Step(id='get', operation=Operation.GET_TASK, params={'id': '{{send.taskId}}'})
        )
        result = run(runner, borrowing)
        assert result.result is Outcome.ERROR
        assert 'no step' in result.failure.message


class TestErrorsVersusFailures:
    def test_a_dispatch_error_is_an_error_not_a_failure(self):
        """The SUT never answered, so it has not been shown non-conformant."""
        dispatcher = FakeDispatcher(raises=DispatchError('connection refused'))
        result = run(runner_for(dispatcher), a_test(get_task(expect={'status': 200})))
        assert result.result is Outcome.ERROR
        assert 'connection refused' in result.failure.message

    def test_an_undefined_variable_is_an_error(self):
        dispatcher = FakeDispatcher(ok())
        step = Step(id='get', operation=Operation.GET_TASK, params={'id': '{{nope}}'})
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.ERROR

    def test_a_capture_the_response_does_not_carry_is_a_failure(self):
        """The path is fine; the SUT answered with an unexpected shape."""
        dispatcher = FakeDispatcher(ok({'nothing': 'here'}))
        step = Step(
            id='send',
            operation=Operation.SEND_MESSAGE,
            params={'message': {'role': 'ROLE_USER'}},
            capture={'taskId': 'task.id'},
        )
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.FAIL
        assert 'cannot capture' in result.failure.message

    def test_an_unparseable_until_is_an_error(self):
        dispatcher = FakeDispatcher(ok({}))
        step = get_task(repeat={'until': 'not an expression at all'})
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.ERROR


class TestExpectError:
    def test_an_expected_error_passes(self):
        dispatcher = FakeDispatcher(failed())
        step = get_task(expect_error={'error_type': 'TaskNotFoundError'})
        assert run(runner_for(dispatcher), a_test(step)).result is Outcome.PASS

    def test_the_wrong_error_fails(self):
        dispatcher = FakeDispatcher(failed(ErrorType.INTERNAL))
        step = get_task(expect_error={'error_type': 'TaskNotFoundError'})
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path == 'error.error_type'

    def test_an_unnamed_error_fails_rather_than_being_guessed(self):
        """A SUT that omits the `ErrorInfo` has not identified its error."""
        dispatcher = FakeDispatcher(failed(None))
        step = get_task(expect_error={'error_type': 'TaskNotFoundError'})
        assert run(runner_for(dispatcher), a_test(step)).result is Outcome.FAIL

    def test_success_where_an_error_was_expected_fails(self):
        dispatcher = FakeDispatcher(ok({'id': 'T1'}))
        step = get_task(expect_error={'error_type': 'TaskNotFoundError'})
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.FAIL
        assert 'expected an error' in result.failure.message

    def test_an_error_where_a_body_was_expected_names_the_error(self):
        """Rather than reporting every expected field as missing."""
        dispatcher = FakeDispatcher(failed())
        result = run(
            runner_for(dispatcher), a_test(get_task(expect={'body': {'id': 'T1'}}))
        )
        assert result.result is Outcome.FAIL
        assert 'TaskNotFoundError' in result.failure.message

    def test_status_is_still_asserted_alongside_an_expected_error(self):
        """A JSON-RPC error rides HTTP 200, so this pair is coherent."""
        dispatcher = FakeDispatcher(failed(status=200))
        step = get_task(
            expect={'status': 200}, expect_error={'error_type': 'TaskNotFoundError'}
        )
        assert run(runner_for(dispatcher), a_test(step)).result is Outcome.PASS

    def test_jsonrpc_error_data_is_assertable(self):
        dispatcher = FakeDispatcher(
            WireResponse(
                status=200,
                error=WireError(
                    message='boom',
                    error_type=ErrorType.TASK_NOT_FOUND,
                    code=-32001,
                    raw={'code': -32001, 'message': 'boom', 'data': {'hint': 'x'}},
                ),
            )
        )
        step = get_task(
            expect_error={'error_type': 'TaskNotFoundError', 'data': {'type': 'object'}}
        )
        assert run(runner_for(dispatcher), a_test(step)).result is Outcome.PASS


class TestPolling:
    def working(self):
        return ok({'status': {'state': 'TASK_STATE_WORKING'}})

    def done(self):
        return ok({'status': {'state': 'TASK_STATE_COMPLETED'}})

    def test_stops_as_soon_as_until_holds(self):
        dispatcher = FakeDispatcher([self.working(), self.working(), self.done()])
        step = get_task(repeat={'until': 'status.state == TASK_STATE_COMPLETED'})
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.PASS
        assert result.steps[0].attempts == 3

    def test_exhausting_max_attempts_fails(self):
        """§9.1 says so explicitly."""
        dispatcher = FakeDispatcher(self.working())
        step = get_task(
            repeat={'until': 'status.state == TASK_STATE_COMPLETED', 'max_attempts': 4}
        )
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.FAIL
        assert result.steps[0].attempts == 4
        assert 'never became true' in result.failure.message

    def test_assertions_see_the_last_response(self):
        dispatcher = FakeDispatcher([self.working(), self.done()])
        step = get_task(
            repeat={'until': 'status.state == TASK_STATE_COMPLETED'},
            expect={'body': {'status': {'state': 'TASK_STATE_COMPLETED'}}},
        )
        assert run(runner_for(dispatcher), a_test(step)).result is Outcome.PASS

    def test_defaults_come_from_the_spec(self):
        slept: list[float] = []
        dispatcher = FakeDispatcher(self.working())
        runner = Runner(
            dispatcher, sleep=_record_sleep(slept), clock=lambda: 0.0
        )
        step = get_task(repeat={'until': 'status.state == TASK_STATE_COMPLETED'})
        asyncio.run(runner.run_test(a_test(step)))
        assert len(dispatcher.calls) == DEFAULT_MAX_ATTEMPTS
        assert slept == [DEFAULT_DELAY_MS / 1000] * (DEFAULT_MAX_ATTEMPTS - 1)

    @pytest.mark.parametrize(
        ('backoff', 'expected'),
        [
            ('none', [1.0, 1.0, 1.0]),
            ('linear', [1.0, 2.0, 3.0]),
            ('exponential', [1.0, 2.0, 4.0]),
        ],
    )
    def test_backoff_strategies(self, backoff, expected):
        slept: list[float] = []
        dispatcher = FakeDispatcher(self.working())
        runner = Runner(dispatcher, sleep=_record_sleep(slept), clock=lambda: 0.0)
        step = get_task(
            repeat={
                'until': 'status.state == TASK_STATE_COMPLETED',
                'max_attempts': 4,
                'delay_ms': 1000,
                'backoff': backoff,
            }
        )
        asyncio.run(runner.run_test(a_test(step)))
        assert slept == expected

    def test_no_sleep_after_the_final_attempt(self):
        slept: list[float] = []
        dispatcher = FakeDispatcher(self.done())
        runner = Runner(dispatcher, sleep=_record_sleep(slept), clock=lambda: 0.0)
        step = get_task(repeat={'until': 'status.state == TASK_STATE_COMPLETED'})
        asyncio.run(runner.run_test(a_test(step)))
        assert slept == []


class TestProtocolObligations:
    def test_the_version_header_goes_on_every_request(self):
        """§12.4."""
        dispatcher = FakeDispatcher(ok())
        runner = runner_for(dispatcher, spec_version='1.0')
        run(runner, a_test(get_task(), get_task('again')))
        assert all(call[2][VERSION_HEADER] == '1.0' for call in dispatcher.calls)

    def test_a_message_without_a_messageId_gets_one(self):
        """§12.4 / §4.3: A2A requires every message to carry one."""
        dispatcher = FakeDispatcher(ok())
        step = Step(
            id='send',
            operation=Operation.SEND_MESSAGE,
            params={'message': {'role': 'ROLE_USER', 'parts': []}},
        )
        run(runner_for(dispatcher), a_test(step))
        assert dispatcher.calls[0][1]['message']['messageId'] == 'generated-id'

    def test_an_explicit_messageId_is_kept(self):
        dispatcher = FakeDispatcher(ok())
        step = Step(
            id='send',
            operation=Operation.SEND_MESSAGE,
            params={'message': {'role': 'ROLE_USER', 'messageId': 'mine'}},
        )
        run(runner_for(dispatcher), a_test(step))
        assert dispatcher.calls[0][1]['message']['messageId'] == 'mine'

    def test_params_are_reshaped_for_the_request_message(self):
        """`taskId` is a `Message` field, not a `SendMessageRequest` one."""
        dispatcher = FakeDispatcher(ok())
        step = Step(
            id='send',
            operation=Operation.SEND_MESSAGE,
            params={'message': {'role': 'ROLE_USER'}, 'taskId': 'T1'},
        )
        run(runner_for(dispatcher), a_test(step))
        sent = dispatcher.calls[0][1]
        assert sent['message']['taskId'] == 'T1'
        assert 'taskId' not in sent

    def test_the_step_params_are_not_mutated(self):
        """A second binding runs the same test object."""
        dispatcher = FakeDispatcher(ok())
        step = Step(
            id='send',
            operation=Operation.SEND_MESSAGE,
            params={'message': {'role': 'ROLE_USER'}, 'taskId': 'T1'},
        )
        run(runner_for(dispatcher), a_test(step))
        assert step.params == {'message': {'role': 'ROLE_USER'}, 'taskId': 'T1'}


class TestSkipping:
    def test_a_binding_the_test_does_not_target(self):
        """§12.3."""
        dispatcher = FakeDispatcher(ok(), binding=TransportBinding.GRPC)
        test = a_test(get_task(), transport=[TransportBinding.REST])
        result = run(runner_for(dispatcher), test)
        assert result.result is Outcome.SKIP
        assert 'grpc' in result.skip_reason

    def test_a_runner_requirement_this_runner_lacks(self):
        dispatcher = FakeDispatcher(ok())
        test = a_test(
            get_task(), runner_requirements=[RunnerRequirement.WEBHOOK_ENDPOINT]
        )
        result = run(runner_for(dispatcher), test)
        assert result.result is Outcome.SKIP
        assert 'webhook_endpoint' in result.skip_reason

    def test_a_requirement_this_runner_has(self):
        dispatcher = FakeDispatcher(ok())
        test = a_test(
            get_task(), runner_requirements=[RunnerRequirement.WEBHOOK_ENDPOINT]
        )
        runner = runner_for(
            dispatcher, capabilities=[RunnerRequirement.WEBHOOK_ENDPOINT]
        )
        assert run(runner, test).result is Outcome.PASS

    def test_a_raw_step_is_deferred(self):
        dispatcher = FakeDispatcher(ok())
        test = a_test(
            Step(id='r', raw=RawBlock(method='GET', path='/tasks/x')),
            transport=[TransportBinding.JSONRPC],
        )
        result = run(runner_for(dispatcher), test)
        assert result.result is Outcome.SKIP
        assert 'raw' in result.skip_reason

    def test_a_streaming_step_is_deferred(self):
        dispatcher = FakeDispatcher(ok())
        step = Step(
            id='s',
            operation=Operation.SEND_STREAMING_MESSAGE,
            params={'message': {'role': 'ROLE_USER'}},
            expect_stream={'min_count': 1},
        )
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.SKIP
        assert 'stream' in result.skip_reason

    def test_a_skipped_test_dispatches_nothing(self):
        dispatcher = FakeDispatcher(ok(), binding=TransportBinding.GRPC)
        run(runner_for(dispatcher), a_test(get_task(), transport=[TransportBinding.REST]))
        assert dispatcher.calls == []


class TestPreconditions:
    """§12.5 — unmet means skip, never fail."""

    CARD = {
        'capabilities': {'streaming': True, 'pushNotifications': False},
        'skills': [{'id': 'echo'}],
        'supportedInterfaces': [{'protocolBinding': 'JSONRPC'}],
    }

    def test_a_met_capability_runs(self):
        runner = runner_for(FakeDispatcher(ok()), agent_card=self.CARD)
        test = a_test(get_task(), preconditions={'capabilities': {'streaming': True}})
        assert run(runner, test).result is Outcome.PASS

    def test_an_unmet_capability_skips(self):
        runner = runner_for(FakeDispatcher(ok()), agent_card=self.CARD)
        test = a_test(
            get_task(), preconditions={'capabilities': {'pushNotifications': True}}
        )
        result = run(runner, test)
        assert result.result is Outcome.SKIP
        assert 'pushNotifications' in result.skip_reason

    def test_an_absent_capability_satisfies_a_false_precondition(self):
        """"Must not advertise X" is satisfied by a card that omits X."""
        runner = runner_for(FakeDispatcher(ok()), agent_card=self.CARD)
        test = a_test(
            get_task(), preconditions={'capabilities': {'extendedAgentCard': False}}
        )
        assert run(runner, test).result is Outcome.PASS

    def test_a_missing_skill_skips(self):
        runner = runner_for(FakeDispatcher(ok()), agent_card=self.CARD)
        test = a_test(get_task(), preconditions={'skills': [{'id': 'nope'}]})
        assert run(runner, test).result is Outcome.SKIP

    def test_a_missing_transport_skips(self):
        runner = runner_for(FakeDispatcher(ok()), agent_card=self.CARD)
        test = a_test(get_task(), preconditions={'transport': [TransportBinding.GRPC]})
        assert run(runner, test).result is Outcome.SKIP

    def test_no_card_where_one_is_needed_is_a_run_error(self):
        """Silently passing an unevaluable precondition would be worse."""
        runner = runner_for(FakeDispatcher(ok()))
        test = a_test(get_task(), preconditions={'capabilities': {'streaming': True}})
        with pytest.raises(RunError, match='agent card'):
            run(runner, test)

    def test_no_card_is_fine_when_nothing_needs_one(self):
        runner = runner_for(FakeDispatcher(ok()))
        assert run(runner, a_test(get_task())).result is Outcome.PASS


class TestBehaviorContract:
    """A missing `tck-*` prefix FAILS — see the module docstring."""

    def test_a_declared_behavior_runs(self):
        runner = runner_for(FakeDispatcher(ok()), sut_behaviors={'tck-cancel'})
        test = a_test(get_task(), requires_behaviors=['tck-cancel'])
        assert run(runner, test).result is Outcome.PASS

    def test_an_undeclared_behavior_fails_rather_than_skipping(self):
        runner = runner_for(FakeDispatcher(ok()), sut_behaviors={'tck-cancel'})
        test = a_test(get_task(), requires_behaviors=['tck-long-running'])
        result = run(runner, test)
        assert result.result is Outcome.FAIL
        assert 'tck-long-running' in result.failure.message

    def test_no_contract_means_no_gating(self):
        """Story 4.5 supplies the contract; until then nothing is checked."""
        runner = runner_for(FakeDispatcher(ok()))
        test = a_test(get_task(), requires_behaviors=['tck-anything'])
        assert run(runner, test).result is Outcome.PASS


class TestNamedAssertions:
    def test_a_step_level_assertion_over_a_captured_response(self):
        dispatcher = FakeDispatcher(ok({'tasks': [{'id': 'T1'}, {'id': 'T2'}]}))
        step = Step(
            id='list',
            operation=Operation.LIST_TASKS,
            assertions=[
                {
                    'source': '{{list.response}}',
                    'any': {'path': 'tasks[*]', 'match': {'id': 'T2'}},
                }
            ],
        )
        assert run(runner_for(dispatcher), a_test(step)).result is Outcome.PASS

    def test_a_failing_one_fails_the_step(self):
        dispatcher = FakeDispatcher(ok({'tasks': [{'id': 'T1'}]}))
        step = Step(
            id='list',
            operation=Operation.LIST_TASKS,
            assertions=[
                {
                    'source': '{{list.response}}',
                    'any': {'path': 'tasks[*]', 'match': {'id': 'MISSING'}},
                }
            ],
        )
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.FAIL
        assert result.failure.step_id == 'list'

    def test_a_test_level_assertion_runs_after_every_step(self):
        dispatcher = FakeDispatcher(ok({'id': 'T1'}))
        test = a_test(
            get_task(),
            assertions=[{'source': '{{get.response}}', 'match': {'type': 'object'}}],
        )
        assert run(runner_for(dispatcher), test).result is Outcome.PASS

    def test_a_failing_test_level_assertion_fails_the_test(self):
        dispatcher = FakeDispatcher(ok({'id': 'T1'}))
        test = a_test(
            get_task(),
            assertions=[{'source': '{{get.response}}', 'match': {'type': 'array'}}],
        )
        result = run(runner_for(dispatcher), test)
        assert result.result is Outcome.FAIL
        assert result.steps[0].result is Outcome.PASS


class TestResultModel:
    def test_failure_detail_matches_the_report_schema(self):
        detail = FailureDetail(
            message='m', step_id='s', expected='e', actual='a', assertion_path='p'
        )
        assert detail.as_json() == {
            'message': 'm', 'step_id': 's', 'expected': 'e',
            'actual': 'a', 'assertion_path': 'p',
        }

    def test_optional_fields_are_omitted_when_unset(self):
        assert FailureDetail(message='m').as_json() == {'message': 'm'}

    def test_summarize_tallies_by_level(self):
        dispatcher = FakeDispatcher(ok({'id': 'T1'}))
        runner = runner_for(dispatcher)
        results = [
            run(runner, a_test(get_task(expect={'body': {'id': 'T1'}}))),
            run(runner, a_test(get_task(expect={'body': {'id': 'X'}}), level=Level.SHOULD)),
        ]
        tally = summarize(results)
        assert tally[Level.MUST][Outcome.PASS] == 1
        assert tally[Level.SHOULD][Outcome.FAIL] == 1
        assert tally[Level.MAY][Outcome.PASS] == 0

    def test_conformance_ignores_non_must_failures(self):
        """§12.7: conformant iff every `must` test passes."""
        dispatcher = FakeDispatcher(ok({'id': 'T1'}))
        runner = runner_for(dispatcher)
        should_fail = run(
            runner, a_test(get_task(expect={'body': {'id': 'X'}}), level=Level.SHOULD)
        )
        must_fail = run(runner, a_test(get_task(expect={'body': {'id': 'X'}})))
        assert is_conformant([should_fail])
        assert not is_conformant([must_fail])

    def test_a_skip_does_not_break_conformance(self):
        dispatcher = FakeDispatcher(ok(), binding=TransportBinding.GRPC)
        skipped = run(
            runner_for(dispatcher),
            a_test(get_task(), transport=[TransportBinding.REST]),
        )
        assert is_conformant([skipped])


class TestImportWeight:
    def test_importing_the_package_does_not_load_grpc(self):
        """The runner needs a dispatcher; reading the corpus does not.

        `grpc` is a C extension worth a noticeable slice of this package's
        import time, and the concrete dispatchers load on first reference to
        keep it out of the path of anything that only inspects tests.
        """
        probe = subprocess.run(
            [
                sys.executable,
                '-c',
                'import sys, test_suite.acts as a; '
                'print(bool(a.Runner) and "grpc" in sys.modules)',
            ],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            check=True,
        )
        assert probe.stdout.strip() == 'False'


class TestAgainstTheCorpus:
    """The real corpus, against a SUT that answers but conforms to nothing.

    Every test must reach a verdict — pass, fail or skip — and none may error,
    because an error would mean the runner broke rather than the SUT.
    """

    def _run_all(self, suite, binding):
        dispatcher = FakeDispatcher(ok({}), binding=binding)
        runner = runner_for(
            dispatcher,
            variables={'insufficientAuthToken': 'x', 'otherUserTaskId': 'y'},
            agent_card={'capabilities': {}},
        )
        return asyncio.run(runner.run_suite(suite))

    @pytest.mark.parametrize('binding', list(TransportBinding))
    def test_no_test_errors(self, suite, binding):
        results = self._run_all(suite, binding)
        errored = [(r.id, r.failure.message) for r in results if r.result is Outcome.ERROR]
        assert errored == []

    @pytest.mark.parametrize('binding', list(TransportBinding))
    def test_every_test_reaches_a_verdict(self, suite, binding):
        results = self._run_all(suite, binding)
        assert len(results) == len(suite.tests)
        assert all(r.result in tuple(Outcome) for r in results)

    @pytest.mark.parametrize(
        ('binding', 'deferred'),
        [
            (TransportBinding.JSONRPC, 33),
            (TransportBinding.GRPC, 19),
            (TransportBinding.REST, 24),
        ],
    )
    def test_how_much_is_deferred_to_story_4_4(self, suite, binding, deferred):
        """Raw, client and streaming steps are the remaining gap.

        Fewer than the 40 tests that contain one, because a test restricted to
        another binding is skipped for that reason first — which is why this
        is a per-binding table rather than one number.
        """
        results = self._run_all(suite, binding)
        skipped = [
            r for r in results
            if r.result is Outcome.SKIP and 'not yet supported' in (r.skip_reason or '')
        ]
        assert len(skipped) == deferred

    @pytest.mark.parametrize('binding', list(TransportBinding))
    def test_the_rest_of_the_corpus_is_executed(self, suite, binding):
        """Whatever is not skipped reached the SUT and was judged."""
        results = self._run_all(suite, binding)
        executed = [r for r in results if r.result is not Outcome.SKIP]
        assert len(executed) >= 55
        assert all(r.steps for r in executed)

    def test_a_conformanceless_sut_is_not_reported_conformant(self, suite):
        """The control: an empty-object SUT must not pass."""
        results = self._run_all(suite, TransportBinding.JSONRPC)
        assert not is_conformant(results)
        assert any(r.result is Outcome.FAIL for r in results)
