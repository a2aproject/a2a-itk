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

from test_suite.acts import LoadedSuite, LoadedTest, load_suite
from test_suite.acts.dispatcher.base import (
    DispatchError,
    Dispatcher,
    MalformedResponse,
    StreamEvent,
    StreamNotOpened,
    UnsupportedByBinding,
    WireError,
    WireResponse,
)
from test_suite.acts.runner import (
    CLIENT_PARSE_BEHAVIOR,
    DEFAULT_DELAY_MS,
    DEFAULT_MAX_ATTEMPTS,
    EXECUTABLE_KINDS,
    KNOWN_CAPABILITIES,
    UNSATISFIABLE,
    VERSION_HEADER,
    FailureDetail,
    Outcome,
    Runner,
    is_conformant,
    summarize,
    unsatisfiable_skips,
)
from test_suite.acts.schema import (
    ErrorType,
    Level,
    Operation,
    RawBlock,
    RunnerRequirement,
    Step,
    StepKind,
    Test,
    TransportBinding,
)


from tests.acts_fake_agent import run_virtual


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
        events: list[Any] | None = None,
        event_status: int | None = 200,
        stall: float | None = None,
    ) -> None:
        self.binding = binding
        if replies is None:
            replies = WireResponse(status=200, payload={})
        self._replies = replies if isinstance(replies, list) else [replies]
        self._raises = raises
        self._events = events or []
        self._event_status = event_status
        self._stall = stall
        self.calls: list[tuple[Operation, Mapping[str, Any], Mapping[str, str]]] = []
        self.raw_calls: list[tuple[RawBlock, Mapping[str, str]]] = []
        self.streamed: list[str] = []

    async def dispatch(self, operation, params=None, headers=None) -> WireResponse:
        self.calls.append((operation, dict(params or {}), dict(headers or {})))
        if self._raises is not None:
            raise self._raises
        index = min(len(self.calls) - 1, len(self._replies) - 1)
        return self._replies[index]

    async def dispatch_raw(self, raw, headers=None) -> WireResponse:
        self.raw_calls.append((raw, dict(headers or {})))
        if self._raises is not None:
            raise self._raises
        index = min(len(self.raw_calls) - 1, len(self._replies) - 1)
        return self._replies[index]

    async def stream(self, operation, params=None, headers=None):
        self.calls.append((operation, dict(params or {}), dict(headers or {})))
        self.streamed.append('operation')
        async for event in self._emit():
            yield event

    async def stream_raw(self, raw, headers=None):
        self.raw_calls.append((raw, dict(headers or {})))
        self.streamed.append('raw')
        async for event in self._emit():
            yield event

    async def _emit(self) -> AsyncIterator[StreamEvent]:
        if self._raises is not None:
            raise self._raises
        for index, data in enumerate(self._events):
            yield StreamEvent(index=index, data=data, status=self._event_status)
        if self._stall is not None:
            # A stream the SUT never closes, for the timeout_ms path.
            await asyncio.sleep(self._stall)


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


def status_update(state: str, **extra: Any) -> dict:
    """A `StreamResponse` carrying a status update, as the wire spells it."""
    return {'statusUpdate': {'taskId': 'T1', 'status': {'state': state}, **extra}}


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


def _suite_of(*tests: Test) -> LoadedSuite:
    """A `LoadedSuite` around hand-built tests, for whole-run behaviour."""
    return LoadedSuite(
        tests=[
            LoadedTest(test=t, suite_id='s', suite_name='s', source=Path('memory'))
            for t in tests
        ]
    )


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

    def test_a_failed_assertion_is_reported_before_a_missed_capture(self):
        """A value is usually captured on the step that also asserts it, and
        the assertion is the one that says what the SUT got wrong."""
        dispatcher = FakeDispatcher(ok({'task': {'id': 'T1'}}))
        step = Step(
            id='send',
            operation=Operation.SEND_MESSAGE,
            params={'message': {'role': 'ROLE_USER'}},
            expect={'body': {'task': {'generation': {'gte': 1}}}},
            capture={'generation': 'task.generation'},
        )
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path == 'body.task.generation'
        assert 'cannot capture' not in result.failure.message

    def test_a_missed_capture_still_fails_a_step_whose_assertions_pass(self):
        """A later step needs the value."""
        dispatcher = FakeDispatcher(ok({'task': {'id': 'T1'}}))
        step = Step(
            id='send',
            operation=Operation.SEND_MESSAGE,
            params={'message': {'role': 'ROLE_USER'}},
            expect={'status': 200, 'body': {'task': {'id': 'T1'}}},
            capture={'generation': 'task.generation'},
        )
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.FAIL
        assert "cannot capture 'generation'" in result.failure.message

    def test_an_error_response_is_reported_as_the_error_not_as_a_missed_capture(self):
        dispatcher = FakeDispatcher(failed())
        step = get_task(
            expect={'status': 200, 'body': {'id': 'T1'}}, capture={'taskId': 'id'}
        )
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.FAIL
        assert 'cannot capture' not in result.failure.message
        assert 'TaskNotFoundError' in str(result.failure.actual)

    def test_a_missed_capture_names_the_error_the_sut_returned(self):
        """With nothing asserted on the step, the capture is all there is to
        report, and it missed because the call failed."""
        dispatcher = FakeDispatcher(failed())
        result = run(runner_for(dispatcher), a_test(get_task(capture={'taskId': 'id'})))
        assert result.result is Outcome.FAIL
        assert "cannot capture 'taskId'" in result.failure.message
        assert 'TaskNotFoundError: no such task' in result.failure.message

    def test_a_step_that_asserts_with_its_own_missed_capture_fails_on_the_capture(self):
        dispatcher = FakeDispatcher(ok({'task': {'id': 'T1'}}))
        step = Step(
            id='send',
            operation=Operation.SEND_MESSAGE,
            params={'message': {'role': 'ROLE_USER'}},
            expect={'body': {'task': {'endGeneration': {'gte': '{{send.generation}}'}}}},
            capture={'generation': 'task.generation'},
        )
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.FAIL
        assert "cannot capture 'generation'" in result.failure.message

    def test_an_undefined_variable_in_an_assertion_is_an_error_on_that_test(self):
        """The test is wrong, not the SUT, and it costs one result. Left to
        escape, it would lose all of them."""
        dispatcher = FakeDispatcher(ok({'id': 'T1'}))
        suite = _suite_of(
            a_test(get_task(expect={'body': {'id': '{{nope}}'}}), id='T-STEP'),
            a_test(
                get_task(),
                id='T-TEST',
                assertions=[{'source': '{{get.response}}', 'match': {'id': '{{nope}}'}}],
            ),
            a_test(get_task(), id='T-FINE'),
        )
        results = asyncio.run(runner_for(dispatcher).run_suite(suite))
        assert [(r.id, r.result) for r in results] == [
            ('T-STEP', Outcome.ERROR), ('T-TEST', Outcome.ERROR), ('T-FINE', Outcome.PASS),
        ]
        assert "undefined variable 'nope'" in results[0].failure.message

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

    def test_an_unnamed_error_reports_what_the_sut_did_say(self):
        """Otherwise the reader re-runs on JSON-RPC just to learn the reason."""
        dispatcher = FakeDispatcher(failed(None, reason='INVALID_PARAMS'))
        step = get_task(expect_error={'error_type': 'TaskNotFoundError'})
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.FAIL
        assert "ErrorInfo.reason 'INVALID_PARAMS'" in result.failure.message
        assert 'not an A2A error name' in result.failure.message

    def test_an_unnamed_error_falls_back_to_the_jsonrpc_code(self):
        dispatcher = FakeDispatcher(failed(None, jsonrpc_code=-31999))
        step = get_task(expect_error={'error_type': 'TaskNotFoundError'})
        result = run(runner_for(dispatcher), a_test(step))
        assert 'JSON-RPC code -31999' in result.failure.message

    def test_a_named_error_is_not_annotated(self):
        """The name is right there; a hint would only be noise."""
        dispatcher = FakeDispatcher(
            failed(ErrorType.INTERNAL, reason='INTERNAL_ERROR')
        )
        step = get_task(expect_error={'error_type': 'TaskNotFoundError'})
        result = run(runner_for(dispatcher), a_test(step))
        assert 'not an A2A error name' not in result.failure.message

    def test_a_silent_error_is_left_alone(self):
        """Nothing to quote — the SUT identified its error in no way at all."""
        dispatcher = FakeDispatcher(failed(None))
        step = get_task(expect_error={'error_type': 'TaskNotFoundError'})
        result = run(runner_for(dispatcher), a_test(step))
        assert result.failure.message == 'error.error_type is missing'

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

    def test_error_details_are_assertable(self):
        """One key for the details array on every binding.

        JSON-RPC calls it `error.data` and REST `error.details` (A2A §9.5,
        §11.6); both are the same list of `@type`-tagged objects, so
        `expect_error.details` asserts on either.
        """
        info = {
            '@type': 'type.googleapis.com/google.rpc.ErrorInfo',
            'reason': 'TASK_NOT_FOUND',
        }
        dispatcher = FakeDispatcher(
            WireResponse(
                status=200,
                error=WireError(
                    message='boom',
                    error_type=ErrorType.TASK_NOT_FOUND,
                    jsonrpc_code=-32001,
                    details=(info,),
                    raw={'code': -32001, 'message': 'boom', 'data': [info]},
                ),
            )
        )
        step = get_task(
            expect_error={
                'error_type': 'TaskNotFoundError',
                'details': {'type': 'array', 'count_gte': 1},
            }
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


class TestCaptureOnARepeatedStep:
    def test_capture_reads_the_attempt_that_satisfied_until(self):
        """Not the first reply: the value has usually changed by the last."""
        dispatcher = FakeDispatcher([
            ok({'status': {'state': 'TASK_STATE_WORKING'}, 'generation': '2'}),
            ok({'status': {'state': 'TASK_STATE_COMPLETED'}, 'generation': '3'}),
            ok({}),
        ])
        poll = get_task(
            'poll',
            repeat={'until': 'status.state == TASK_STATE_COMPLETED'},
            capture={'generation': 'generation'},
        )
        after = Step(
            id='after', operation=Operation.GET_TASK,
            params={'id': 'T1', 'currentGeneration': '{{poll.generation}}'},
        )
        assert run(runner_for(dispatcher), a_test(poll, after)).result is Outcome.PASS
        assert dispatcher.calls[-1][1]['currentGeneration'] == '3'


def virtual_clock() -> float:
    """The clock of the loop a test runs on; see `run_virtual`."""
    return asyncio.get_running_loop().time()


class SlowDispatcher(FakeDispatcher):
    """Takes ``takes`` seconds to answer, on whatever clock the loop keeps."""

    def __init__(self, *args: Any, takes: float, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.takes = takes

    async def dispatch(self, operation, params=None, headers=None) -> WireResponse:
        await asyncio.sleep(self.takes)
        return await super().dispatch(operation, params, headers)

    async def stream(self, operation, params=None, headers=None):
        await asyncio.sleep(self.takes)
        async for event in super().stream(operation, params, headers):
            yield event


class TestStepDuration:
    """`min_duration_ms` and `max_duration_ms`.

    The only way a test can observe that a server held a request open, or that
    it answered at once.
    """

    def timed(self, dispatcher, step, **kwargs):
        runner = runner_for(dispatcher, clock=virtual_clock, **kwargs)
        return run_virtual(runner.run_test(a_test(step)))

    @pytest.mark.parametrize(
        ('takes', 'bounds', 'outcome'),
        [
            (4.0, {'min_duration_ms': 1000}, Outcome.PASS),
            (0.2, {'min_duration_ms': 1000}, Outcome.FAIL),
            (1.0, {'min_duration_ms': 1000}, Outcome.PASS),
            (0.2, {'max_duration_ms': 3000}, Outcome.PASS),
            (4.0, {'max_duration_ms': 3000}, Outcome.FAIL),
            (2.9, {'max_duration_ms': 3000}, Outcome.PASS),
            (2.0, {'min_duration_ms': 1000, 'max_duration_ms': 3000}, Outcome.PASS),
            (0.5, {'min_duration_ms': 1000, 'max_duration_ms': 3000}, Outcome.FAIL),
            (3.5, {'min_duration_ms': 1000, 'max_duration_ms': 3000}, Outcome.FAIL),
        ],
    )
    def test_the_response_time_is_held_to_the_bounds(self, takes, bounds, outcome):
        result = self.timed(SlowDispatcher(ok({}), takes=takes), get_task(**bounds))
        assert result.result is outcome

    def test_a_response_that_came_too_soon_says_how_soon(self):
        result = self.timed(
            SlowDispatcher(ok({}), takes=0.2), get_task(min_duration_ms=1000)
        )
        failure = result.failure
        assert failure.assertion_path == 'duration'
        assert (failure.expected, failure.actual) == ('at least 1000ms', '200ms')
        assert 'arrived after 200ms' in failure.message

    def test_a_request_held_past_the_bound_is_abandoned(self):
        """The step fails at the bound. Waiting for the answer would take as
        long as the SUT likes, and then end in the transport's own timeout."""
        ended: list[float] = []

        async def held():
            runner = runner_for(SlowDispatcher(ok({}), takes=500.0), clock=virtual_clock)
            result = await runner.run_test(a_test(get_task(max_duration_ms=3000)))
            ended.append(virtual_clock())
            return result

        result = run_virtual(held())
        assert result.result is Outcome.FAIL
        failure = result.failure
        assert failure.assertion_path == 'duration'
        assert (failure.expected, failure.actual) == ('at most 3000ms', 'no response')
        assert 'no response after 3000ms' in failure.message
        assert ended == [pytest.approx(3.0)]

    def test_another_timeout_is_not_taken_for_the_bound(self):
        """Only the step's own bound running out means the SUT held the request."""
        dispatcher = FakeDispatcher(raises=TimeoutError('not the bound'))
        with pytest.raises(TimeoutError, match='not the bound'):
            self.timed(dispatcher, get_task(max_duration_ms=3000))

    def test_a_time_is_not_rounded_to_the_bound_it_missed(self):
        """999.6ms against a minimum of 1000ms must not read "1000ms"."""
        result = self.timed(
            SlowDispatcher(ok({}), takes=0.9996), get_task(min_duration_ms=1000)
        )
        assert result.failure.actual == '999ms'

    def test_the_wait_before_the_step_is_not_part_of_it(self):
        """`delay_ms` is the runner waiting, not the SUT answering."""
        step = get_task(delay_ms=5000, max_duration_ms=1000)
        result = self.timed(
            SlowDispatcher(ok({}), takes=0.2), step, sleep=asyncio.sleep
        )
        assert result.result is Outcome.PASS
        step = get_task(delay_ms=5000, min_duration_ms=1000)
        result = self.timed(
            SlowDispatcher(ok({}), takes=0.2), step, sleep=asyncio.sleep
        )
        assert result.result is Outcome.FAIL

    def test_a_wrong_response_is_reported_before_its_timing(self):
        result = self.timed(
            SlowDispatcher(ok({'id': 'OTHER'}), takes=0.2),
            get_task(min_duration_ms=1000, expect={'body': {'id': 'T1'}}),
        )
        assert result.failure.assertion_path == 'body.id'

    def test_an_error_response_is_timed_too(self):
        step = get_task(
            min_duration_ms=1000, expect_error={'error_type': 'TaskNotFoundError'}
        )
        assert self.timed(SlowDispatcher(failed(), takes=0.2), step).result is Outcome.FAIL
        assert self.timed(SlowDispatcher(failed(), takes=2.0), step).result is Outcome.PASS

    def test_a_refused_stream_is_timed_too(self):
        """A streaming operation that expects to be refused reads no stream,
        so its duration can be bounded like any other response."""
        refusal = StreamNotOpened('the SUT refused the stream', failed())
        step = Step(
            id='s', operation=Operation.SUBSCRIBE_TO_TASK, params={'id': 'T1'},
            expect_error={'error_type': 'TaskNotFoundError'}, max_duration_ms=1000,
        )
        quick = SlowDispatcher(raises=refusal, takes=0.2)
        assert self.timed(quick, step).result is Outcome.PASS
        slow = SlowDispatcher(raises=refusal, takes=1.0004)
        result = self.timed(slow, step)
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path == 'duration'
        assert result.failure.actual == '1001ms'

    def test_a_step_without_bounds_is_not_timed(self):
        result = self.timed(SlowDispatcher(ok({}), takes=500.0), get_task())
        assert result.result is Outcome.PASS


class TestStepSentAsAnotherVersion:
    """`a2a_version`: one step sent as a client of another A2A version."""

    def test_the_step_carries_that_version_and_the_others_the_runs(self):
        dispatcher = FakeDispatcher(ok())
        test = a_test(
            get_task('before'),
            get_task('as-1-0', a2a_version='1.0'),
            get_task('after'),
        )
        run(runner_for(dispatcher, spec_version='1.1'), test)
        assert [call[2][VERSION_HEADER] for call in dispatcher.calls] == ['1.1', '1.0', '1.1']

    def test_spec_version_still_names_the_run_version(self):
        """The step is sent as another version; the run still targets its own."""
        dispatcher = FakeDispatcher(ok())
        step = Step(
            id='get', operation=Operation.GET_TASK, a2a_version='1.0',
            params={'id': '{{specVersion}}'},
        )
        run(runner_for(dispatcher, spec_version='1.1'), a_test(step))
        _, params, headers = dispatcher.calls[0]
        assert params['id'] == '1.1'
        assert headers[VERSION_HEADER] == '1.0'

    def routed(self, *steps):
        """A run whose agent serves 1.0 on an interface of its own."""
        main = FakeDispatcher(ok(), events=[status_update('TASK_STATE_COMPLETED')])
        other = FakeDispatcher(ok(), events=[status_update('TASK_STATE_COMPLETED')])
        asked: list[str] = []

        def dispatcher_for_version(version: str):
            asked.append(version)
            return other if version == '1.0' else None

        runner = runner_for(
            main, spec_version='1.1', dispatcher_for_version=dispatcher_for_version
        )
        result = run(runner, a_test(*steps))
        return result, main, other, asked

    def test_the_step_goes_to_the_interface_for_its_version(self):
        result, main, other, asked = self.routed(
            get_task('before'), get_task('as-1-0', a2a_version='1.0'), get_task('after')
        )
        assert result.result is Outcome.PASS
        assert len(main.calls) == 2
        assert [call[2][VERSION_HEADER] for call in other.calls] == ['1.0']
        # Asked only for the step that names a version.
        assert asked == ['1.0']

    def test_a_streaming_step_goes_there_too(self):
        step = Step(
            id='s', operation=Operation.SUBSCRIBE_TO_TASK, params={'id': 'T1'},
            a2a_version='1.0', expect_stream={'min_count': 1},
        )
        _, main, other, _ = self.routed(step)
        assert (main.streamed, other.streamed) == ([], ['operation'])
        assert other.calls[0][2][VERSION_HEADER] == '1.0'

    def test_a_repeated_step_goes_there_on_every_attempt(self):
        step = get_task(
            a2a_version='1.0',
            repeat={'until': 'status.state == NEVER', 'max_attempts': 3},
        )
        _, main, other, _ = self.routed(step)
        assert main.calls == []
        assert [call[2][VERSION_HEADER] for call in other.calls] == ['1.0'] * 3

    def test_a_version_the_agent_does_not_serve_goes_to_the_runs_interface(self):
        """Which is expected to refuse it."""
        _, main, other, asked = self.routed(get_task(a2a_version='1.99'))
        assert asked == ['1.99']
        assert [call[2][VERSION_HEADER] for call in main.calls] == ['1.99']
        assert other.calls == []


class TestProtocolObligations:
    def test_the_version_header_goes_on_every_request(self):
        """§12.4."""
        dispatcher = FakeDispatcher(ok())
        runner = runner_for(dispatcher, spec_version='1.0')
        run(runner, a_test(get_task(), get_task('again')))
        assert all(call[2][VERSION_HEADER] == '1.0' for call in dispatcher.calls)

    def test_the_version_header_carries_the_run_version(self):
        dispatcher = FakeDispatcher(ok())
        run(runner_for(dispatcher, spec_version='1.1'), a_test(get_task()))
        assert dispatcher.calls[0][2][VERSION_HEADER] == '1.1'

    def test_spec_version_resolves_to_the_run_version(self):
        """A raw step is sent as written, so this variable is how it carries
        the version the run targets."""
        dispatcher = FakeDispatcher(ok())
        step = Step(
            id='raw',
            raw={
                'method': 'POST', 'path': '/',
                'headers': {'A2A-Version': '{{specVersion}}'},
                'body': {},
            },
        )
        test = a_test(step, transport=[TransportBinding.JSONRPC])
        run(runner_for(dispatcher, spec_version='1.1'), test)
        assert dispatcher.raw_calls[0][0].headers == {'A2A-Version': '1.1'}

    def test_nothing_can_make_the_variable_disagree_with_the_header(self):
        """Not a caller's variable and not a document's."""
        dispatcher = FakeDispatcher(ok())
        step = Step(
            id='get', operation=Operation.GET_TASK,
            params={'id': '{{specVersion}}'},
        )
        runner = runner_for(
            dispatcher, spec_version='1.1', variables={'specVersion': '9.9'}
        )
        run(runner, a_test(step), variables={'specVersion': '8.8'})
        _, params, headers = dispatcher.calls[0]
        assert params['id'] == '1.1'
        assert headers[VERSION_HEADER] == '1.1'

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

    @pytest.mark.parametrize(
        ('bounds', 'spec_version', 'said'),
        [
            ({'min_spec_version': '1.1'}, '1.0', 'A2A 1.1 and newer'),
            ({'max_spec_version': '1.0'}, '1.1', 'A2A 1.0 and older'),
        ],
    )
    def test_a_version_the_test_does_not_apply_to(self, bounds, spec_version, said):
        """A backstop: the pipeline drops these before the run."""
        dispatcher = FakeDispatcher(ok())
        runner = runner_for(dispatcher, spec_version=spec_version)
        result = run(runner, a_test(get_task(), **bounds))
        assert result.result is Outcome.SKIP
        assert said in result.skip_reason
        assert f'targets A2A {spec_version}' in result.skip_reason
        assert dispatcher.calls == []

    def test_a_version_inside_the_range_runs(self):
        dispatcher = FakeDispatcher(ok())
        runner = runner_for(dispatcher, spec_version='1.1')
        test = a_test(get_task(), min_spec_version='1.1')
        assert run(runner, test).result is Outcome.PASS

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

    def test_a_capability_the_spec_does_not_define_is_marked_unsatisfiable(self):
        """An ordinary "capability=False" skip reads as "not applicable here".

        `SEC-AUTH-001..004` and `SEC-AUTH-006` gated on
        `capabilities.authentication`, which A2A has not got, and so sat
        invisible on every binding against every SDK. They now gate on
        `preconditions.authentication` instead, but the marker stays: it is
        what caught that, and it says nobody can clear the skip by configuring
        the SUT differently.
        """
        runner = runner_for(FakeDispatcher(ok()), agent_card=self.CARD)
        test = a_test(
            get_task(), preconditions={'capabilities': {'telepathy': True}}
        )
        result = run(runner, test)
        assert result.result is Outcome.SKIP
        assert result.skip_reason.startswith(UNSATISFIABLE)
        assert "'telepathy' is not a capability" in result.skip_reason

    VERSIONED_CARD = {
        'supportedInterfaces': [
            {'protocolBinding': 'JSONRPC', 'protocolVersion': '1.1', 'url': 'http://sut/a'},
            {'protocolBinding': 'JSONRPC', 'protocolVersion': '1.0.0', 'url': 'http://sut/a'},
            {'protocolBinding': 'JSONRPC', 'url': 'http://sut/unversioned'},
            {'protocolBinding': 'GRPC', 'protocolVersion': '0.3', 'url': 'sut:1'},
        ],
    }

    def versioned(self, *versions, binding=TransportBinding.JSONRPC):
        runner = runner_for(
            FakeDispatcher(ok(), binding=binding), agent_card=self.VERSIONED_CARD
        )
        test = a_test(get_task(), preconditions={'protocol_versions': list(versions)})
        return run(runner, test)

    def test_a_version_the_agent_serves_on_this_binding_runs(self):
        assert self.versioned('1.1').result is Outcome.PASS
        # The card writes `1.0.0`, which plainly means 1.0.
        assert self.versioned('1.0').result is Outcome.PASS
        assert self.versioned('1.0', '1.1').result is Outcome.PASS

    def test_a_version_the_agent_does_not_serve_skips(self):
        result = self.versioned('1.2')
        assert result.result is Outcome.SKIP
        assert result.skip_reason == (
            'agent card lists no jsonrpc interface for A2A 1.2; it lists 1.0, 1.1'
        )

    def test_every_listed_version_has_to_be_served(self):
        result = self.versioned('1.0', '1.2')
        assert result.result is Outcome.SKIP
        assert 'for A2A 1.2;' in result.skip_reason

    def test_an_interface_on_another_binding_does_not_count(self):
        """It does not serve a request sent over this one."""
        assert self.versioned('0.3').result is Outcome.SKIP
        assert self.versioned('0.3', binding=TransportBinding.GRPC).result is Outcome.PASS
        result = self.versioned('1.1', binding=TransportBinding.GRPC)
        assert result.skip_reason == (
            'agent card lists no grpc interface for A2A 1.1; it lists 0.3'
        )

    def test_a_card_with_no_interface_for_the_binding_skips(self):
        runner = runner_for(FakeDispatcher(ok()), agent_card={})
        test = a_test(get_task(), preconditions={'protocol_versions': ['1.0']})
        result = run(runner, test)
        assert result.result is Outcome.SKIP
        assert result.skip_reason.endswith('it lists none')

    def test_the_known_set_comes_from_the_specs_own_proto(self):
        """Written out by hand it would drift; derived, it cannot."""
        assert KNOWN_CAPABILITIES == {
            'streaming', 'pushNotifications', 'extensions', 'extendedAgentCard',
            'userArtifacts',
        }

    SECURED_CARD = {
        'securitySchemes': {'bearerAuth': {'httpAuthSecurityScheme': {'scheme': 'Bearer'}}},
        'securityRequirements': [{'schemes': {'bearerAuth': {'list': []}}}],
    }

    def test_authentication_is_read_off_the_top_level_of_the_card(self):
        """Not off `capabilities`, which has no member for it."""
        runner = runner_for(FakeDispatcher(ok()), agent_card=self.SECURED_CARD)
        result = run(runner, a_test(get_task(), preconditions={'authentication': True}))
        assert result.result is not Outcome.SKIP

    def test_an_agent_declaring_no_security_skips_a_test_needing_it(self):
        """A2A conditions the obligation on the agent's own declaration, so a
        rejection test is genuinely not applicable to an open agent."""
        runner = runner_for(FakeDispatcher(ok()), agent_card=self.CARD)
        result = run(runner, a_test(get_task(), preconditions={'authentication': True}))
        assert result.result is Outcome.SKIP
        assert result.skip_reason == 'agent card authentication=False, needs True'

    def test_schemes_without_requirements_do_not_count_as_requiring_auth(self):
        """`securitySchemes` offers a scheme a client *may* use; only a
        non-empty `securityRequirements` says one is required."""
        runner = runner_for(
            FakeDispatcher(ok()),
            agent_card={
                'securitySchemes': {'bearerAuth': {}}, 'securityRequirements': []
            },
        )
        result = run(runner, a_test(get_task(), preconditions={'authentication': True}))
        assert result.result is Outcome.SKIP
        assert 'authentication=False' in result.skip_reason

    def test_a_test_needing_an_open_agent_skips_against_a_secured_one(self):
        runner = runner_for(FakeDispatcher(ok()), agent_card=self.SECURED_CARD)
        result = run(runner, a_test(get_task(), preconditions={'authentication': False}))
        assert result.result is Outcome.SKIP
        assert result.skip_reason == 'agent card authentication=True, needs False'

    def test_the_skip_is_not_marked_unsatisfiable(self):
        """A configurable deployment can meet it, unlike a phantom capability."""
        runner = runner_for(FakeDispatcher(ok()), agent_card=self.CARD)
        result = run(runner, a_test(get_task(), preconditions={'authentication': True}))
        assert not (result.skip_reason or '').startswith(UNSATISFIABLE)

    def test_an_unsatisfiable_skip_is_not_an_error(self):
        """It is the corpus that is wrong, and the SUT must not wear it.

        `is_conformant` counts errors against the verdict, so erroring here
        would publish a corpus defect as this SDK's non-conformance.
        """
        runner = runner_for(FakeDispatcher(ok()), agent_card=self.CARD)
        test = a_test(
            get_task(), preconditions={'capabilities': {'authentication': True}}
        )
        assert run(runner, test).result is not Outcome.ERROR

    def test_unsatisfiable_skips_are_collectable(self):
        """What the CLI reads to print them; an ordinary skip stays out."""
        runner = runner_for(FakeDispatcher(ok()), agent_card=self.CARD)
        blocked = run(
            runner,
            a_test(get_task(), preconditions={'capabilities': {'authentication': True}}),
        )
        ordinary = run(
            runner,
            a_test(
                get_task(), preconditions={'capabilities': {'pushNotifications': True}}
            ),
        )
        assert [r.id for r in unsatisfiable_skips([blocked, ordinary])] == [blocked.id]

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

    def test_no_card_where_one_is_needed_errors_the_test(self):
        """Silently passing an unevaluable precondition would be worse.

        `error`, not `fail`: the inputs to the run are incomplete and the SUT
        was never asked anything, so it must not wear this in its report.
        """
        runner = runner_for(FakeDispatcher(ok()))
        test = a_test(get_task(), preconditions={'capabilities': {'streaming': True}})
        result = run(runner, test)
        assert result.result is Outcome.ERROR
        assert 'agent card' in result.failure.message

    def test_an_unevaluable_precondition_does_not_abort_the_rest(self):
        """One missing input costs one result, not the whole suite."""
        runner = runner_for(FakeDispatcher(ok()))
        needs_card = a_test(
            get_task(), preconditions={'capabilities': {'streaming': True}}
        )
        results = asyncio.run(
            runner.run_suite(_suite_of(needs_card, a_test(get_task(), id='T-002')))
        )
        assert [r.result for r in results] == [Outcome.ERROR, Outcome.PASS]

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
        """A SUT that ships no contract file has claimed nothing to check."""
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

class TestRawSteps:
    """§4.4 — a hand-built request, sent exactly as written."""

    def a_raw_test(self, *steps, binding=TransportBinding.JSONRPC):
        """A test of only raw steps MUST declare its binding (§4.4), and the
        schema enforces it — a raw request hard-codes one wire format."""
        return a_test(*steps, transport=[binding])

    def raw_step(self, step_id='r', **kwargs):
        block = kwargs.pop('raw', None) or RawBlock(
            method='POST', path='/', headers={'Content-Type': 'application/json'},
            body={'jsonrpc': '2.0', 'id': 1, 'method': 'GetTask'},
        )
        return Step(id=step_id, raw=block, **kwargs)

    def test_a_missed_capture_does_not_blame_the_error_in_the_body(self):
        """A raw response is read whole, error or not, so the path missed for
        a reason of its own."""
        dispatcher = FakeDispatcher(failed())
        step = self.raw_step(capture={'code': 'error.code'})
        result = run(runner_for(dispatcher), self.a_raw_test(step))
        assert result.result is Outcome.FAIL
        assert "cannot capture 'code'" in result.failure.message
        assert 'TaskNotFoundError' not in result.failure.message

    def test_it_goes_to_dispatch_raw_not_dispatch(self):
        dispatcher = FakeDispatcher(ok({'jsonrpc': '2.0', 'result': {}}))
        run(runner_for(dispatcher), self.a_raw_test(self.raw_step()))
        assert len(dispatcher.raw_calls) == 1
        assert dispatcher.calls == []

    def test_the_runner_adds_no_headers_of_its_own(self):
        """`VER-NEG-002` omits `A2A-Version` deliberately; §12.4 excepts it,
        and a runner that helpfully added one would repair the test away."""
        dispatcher = FakeDispatcher(ok({}))
        run(runner_for(dispatcher), self.a_raw_test(self.raw_step()))
        assert dispatcher.raw_calls[0][1] == {}

    def test_variables_in_raw_headers_are_substituted(self):
        """`SEC-AUTH-002`'s `Authorization: Bearer {{insufficientAuthToken}}`."""
        dispatcher = FakeDispatcher(ok({}))
        runner = runner_for(dispatcher, variables={'tok': 'shh'})
        block = RawBlock(
            method='POST', path='/', headers={'Authorization': 'Bearer {{tok}}'}
        )
        run(runner, self.a_raw_test(self.raw_step(raw=block)))
        assert dispatcher.raw_calls[0][0].headers['Authorization'] == 'Bearer shh'

    def test_variables_in_the_raw_path_are_substituted(self):
        dispatcher = FakeDispatcher(ok({}))
        runner = runner_for(dispatcher, variables={'id': 'T9'})
        block = RawBlock(method='GET', path='/tasks/{{id}}')
        run(runner, self.a_raw_test(self.raw_step(raw=block)))
        assert dispatcher.raw_calls[0][0].path == '/tasks/T9'

    def test_expect_body_asserts_on_the_whole_envelope(self):
        """`JSONRPC-ERR-001` asserts `body.error.code`, not an unwrapped result."""
        envelope = {'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32601, 'message': 'no'}}
        dispatcher = FakeDispatcher(ok(envelope))
        step = self.raw_step(expect={'status': 200, 'body': {'error': {'code': -32601}}})
        assert run(runner_for(dispatcher), self.a_raw_test(step)).result is Outcome.PASS

    def test_a_wrong_envelope_fails(self):
        envelope = {'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32000}}
        dispatcher = FakeDispatcher(ok(envelope))
        step = self.raw_step(expect={'body': {'error': {'code': -32601}}})
        result = run(runner_for(dispatcher), self.a_raw_test(step))
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path == 'body.error.code'

    def test_capture_works_from_a_raw_response(self):
        dispatcher = FakeDispatcher(ok({'result': {'task': {'id': 'T5'}}}))
        step = self.raw_step(capture={'taskId': 'result.task.id'})
        assert run(runner_for(dispatcher), self.a_raw_test(step)).result is Outcome.PASS

    def test_a_raw_step_on_grpc_errors_rather_than_failing(self):
        """It means the test should have declared a transport, not that the
        agent is wrong."""
        dispatcher = FakeDispatcher(
            ok(), binding=TransportBinding.GRPC,
            raises=UnsupportedByBinding('gRPC has no raw-request form'),
        )
        test = self.a_raw_test(self.raw_step(), binding=TransportBinding.GRPC)
        assert run(runner_for(dispatcher), test).result is Outcome.ERROR


class TestStreamingSteps:
    """§7 wiring — the evaluator itself is covered in test_acts_streaming."""

    def stream_step(self, step_id='s', **kwargs):
        return Step(
            id=step_id,
            operation=Operation.SEND_STREAMING_MESSAGE,
            params={'message': {'role': 'ROLE_USER'}},
            **kwargs,
        )

    def test_a_satisfied_stream_passes(self):
        dispatcher = FakeDispatcher(events=[
            status_update('TASK_STATE_WORKING'),
            status_update('TASK_STATE_COMPLETED'),
        ])
        step = self.stream_step(expect_stream={
            'min_count': 2,
            'final_event': {'status': {'state': 'TASK_STATE_COMPLETED'}},
        })
        assert run(runner_for(dispatcher), a_test(step)).result is Outcome.PASS

    def test_an_unsatisfied_stream_fails(self):
        dispatcher = FakeDispatcher(events=[status_update('TASK_STATE_WORKING')])
        step = self.stream_step(expect_stream={'min_count': 3})
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.FAIL
        assert 'at least 3' in result.failure.message

    def test_it_streams_rather_than_dispatching(self):
        dispatcher = FakeDispatcher(events=[status_update('TASK_STATE_COMPLETED')])
        run(runner_for(dispatcher), a_test(self.stream_step(expect_stream={'min_count': 1})))
        assert dispatcher.streamed == ['operation']

    def test_the_version_header_still_goes_on_a_streaming_operation(self):
        dispatcher = FakeDispatcher(events=[status_update('TASK_STATE_COMPLETED')])
        run(runner_for(dispatcher), a_test(self.stream_step(expect_stream={'min_count': 1})))
        assert dispatcher.calls[0][2][VERSION_HEADER] == '1.0'

    def test_a_raw_streaming_step_uses_stream_raw(self):
        """`JSONRPC-SSE-001` — raw plus `expect_stream`."""
        dispatcher = FakeDispatcher(events=[{'jsonrpc': '2.0'}, {'jsonrpc': '2.0'}])
        step = Step(
            id='stream-raw',
            raw=RawBlock(method='POST', path='/', body={'jsonrpc': '2.0'}),
            expect_stream={'min_count': 2, 'each_event': {'jsonrpc': '2.0'}},
        )
        test = a_test(step, transport=[TransportBinding.JSONRPC])
        result = run(runner_for(dispatcher), test)
        assert result.result is Outcome.PASS
        assert dispatcher.streamed == ['raw']

    def test_expect_status_uses_the_observed_stream_status(self):
        dispatcher = FakeDispatcher(
            events=[status_update('TASK_STATE_COMPLETED')], event_status=200
        )
        step = self.stream_step(
            expect={'status': 200}, expect_stream={'min_count': 1}
        )
        assert run(runner_for(dispatcher), a_test(step)).result is Outcome.PASS

    def test_a_wrong_stream_status_fails(self):
        dispatcher = FakeDispatcher(
            events=[status_update('TASK_STATE_COMPLETED')], event_status=500
        )
        step = self.stream_step(expect={'status': 200}, expect_stream={'min_count': 1})
        assert run(runner_for(dispatcher), a_test(step)).result is Outcome.FAIL

    def test_an_unobservable_status_is_reported_not_invented(self):
        """gRPC has none, and a stream with no events observed nothing."""
        dispatcher = FakeDispatcher(events=[], event_status=None)
        step = self.stream_step(expect={'status': 200}, expect_stream={})
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.FAIL
        assert 'no observable one' in result.failure.message

    def test_max_count_stops_collection_one_event_past_the_limit(self):
        """Enough to prove the violation, without reading a runaway stream."""
        dispatcher = FakeDispatcher(events=[status_update('TASK_STATE_WORKING')] * 50)
        step = self.stream_step(expect_stream={'max_count': 2})
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.FAIL
        assert 'got 3' in result.failure.message

    def test_timeout_ms_cuts_a_stream_that_never_closes(self):
        dispatcher = FakeDispatcher(
            events=[status_update('TASK_STATE_WORKING')], stall=30.0
        )
        step = self.stream_step(expect_stream={'min_count': 1, 'timeout_ms': 20})
        result = asyncio.run(
            Runner(dispatcher, clock=lambda: 0.0).run_test(a_test(step))
        )
        assert result.result is Outcome.FAIL
        assert 'did not complete within 20ms' in result.failure.message

    def test_a_step_declaring_no_bound_still_stops(self):
        """Every streaming step in the corpus is one of these.

        None declares `timeout_ms` and none declares `max_count`, so without a
        runner-side backstop a SUT that stops sending hangs the run for good
        and no report is produced at all.
        """
        dispatcher = FakeDispatcher(
            events=[status_update('TASK_STATE_WORKING')], stall=30.0
        )
        step = self.stream_step(expect_stream={'min_count': 1})
        result = asyncio.run(
            Runner(dispatcher, clock=lambda: 0.0, stream_timeout_ms=20).run_test(
                a_test(step)
            )
        )
        assert result.result is Outcome.ERROR
        assert 'still open after 20ms' in result.failure.message

    def test_a_backstopped_stream_errors_rather_than_failing(self):
        """The deadline was ours, so the SUT must not wear it as a §7 failure.

        It stays counted against `must` conformance, so a SUT that never
        closes a stream still cannot be reported conformant.
        """
        dispatcher = FakeDispatcher(
            events=[status_update('TASK_STATE_WORKING')], stall=30.0
        )
        step = self.stream_step(expect_stream={'min_count': 1})
        result = asyncio.run(
            Runner(dispatcher, clock=lambda: 0.0, stream_timeout_ms=20).run_test(
                a_test(step)
            )
        )
        assert result.result is Outcome.ERROR
        assert not is_conformant([result])

    def test_a_declared_timeout_wins_over_the_backstop(self):
        """However long it is — the test's own bound is the assertion."""
        dispatcher = FakeDispatcher(
            events=[status_update('TASK_STATE_WORKING')], stall=30.0
        )
        step = self.stream_step(expect_stream={'min_count': 1, 'timeout_ms': 20})
        result = asyncio.run(
            Runner(
                dispatcher, clock=lambda: 0.0, stream_timeout_ms=30_000
            ).run_test(a_test(step))
        )
        assert result.result is Outcome.FAIL
        assert 'did not complete within 20ms' in result.failure.message

    def test_a_stream_that_closes_is_untouched_by_the_backstop(self):
        dispatcher = FakeDispatcher(events=[status_update('TASK_STATE_COMPLETED')])
        step = self.stream_step(expect_stream={'min_count': 1})
        runner = runner_for(dispatcher, stream_timeout_ms=20)
        assert run(runner, a_test(step)).result is Outcome.PASS

    def test_the_events_are_the_steps_response(self):
        dispatcher = FakeDispatcher(events=[status_update('TASK_STATE_COMPLETED')])
        step = self.stream_step(
            expect_stream={'min_count': 1},
            assertions=[{
                'source': '{{s.response}}',
                'any': {'path': '[*]', 'match': {'status': {'exists': True}}},
            }],
        )
        assert run(runner_for(dispatcher), a_test(step)).result is Outcome.PASS

    # -- capture, and assertions that use captured values ---------------------

    SNAPSHOT = {'task': {'id': 'T1', 'status': {'state': 'TASK_STATE_WORKING'}, 'generation': '2'}}

    def subscribe(self, step_id='s', **kwargs):
        return Step(
            id=step_id, operation=Operation.SUBSCRIBE_TO_TASK, params={'id': 'T1'}, **kwargs
        )

    def test_capture_reads_the_first_event_with_its_kind_named_first(self):
        dispatcher = FakeDispatcher(
            events=[self.SNAPSHOT, status_update('TASK_STATE_COMPLETED', generation='3')]
        )
        watch = self.subscribe(
            capture={'taskId': 'task.id', 'generation': 'task.generation'}
        )
        after = Step(
            id='after', operation=Operation.GET_TASK,
            params={'id': '{{s.taskId}}', 'currentGeneration': '{{s.generation}}'},
        )
        assert run(runner_for(dispatcher), a_test(watch, after)).result is Outcome.PASS
        assert dispatcher.calls[-1][1] == {'id': 'T1', 'currentGeneration': '2'}

    @pytest.mark.parametrize('path', ['status_update.taskId', 'statusUpdate.taskId'])
    def test_capture_accepts_either_spelling_of_the_kind(self, path):
        """The wire writes `statusUpdate`; a test writes `status_update`."""
        dispatcher = FakeDispatcher(events=[status_update('TASK_STATE_COMPLETED')])
        watch = self.subscribe(capture={'taskId': path})
        after = Step(id='after', operation=Operation.GET_TASK, params={'id': '{{s.taskId}}'})
        assert run(runner_for(dispatcher), a_test(watch, after)).result is Outcome.PASS
        assert dispatcher.calls[-1][1] == {'id': 'T1'}

    def test_capture_does_not_read_past_the_first_event(self):
        """The first event is the one a later step can rely on."""
        dispatcher = FakeDispatcher(
            events=[status_update('TASK_STATE_WORKING'), self.SNAPSHOT]
        )
        result = run(
            runner_for(dispatcher), a_test(self.subscribe(capture={'taskId': 'task.id'}))
        )
        assert result.result is Outcome.FAIL
        assert "cannot capture 'taskId'" in result.failure.message
        assert 'it is a status_update event' in result.failure.message

    def test_capture_on_a_stream_without_events_fails(self):
        dispatcher = FakeDispatcher(events=[])
        result = run(
            runner_for(dispatcher), a_test(self.subscribe(capture={'taskId': 'task.id'}))
        )
        assert result.result is Outcome.FAIL
        assert 'the stream produced no event' in result.failure.message

    def test_capture_on_a_raw_stream_reads_the_event_as_it_arrived(self):
        """A raw step asserts on the envelope, and captures from it too."""
        dispatcher = FakeDispatcher(
            events=[{'jsonrpc': '2.0', 'id': 1, 'result': {'task': {'id': 'T7'}}}]
        )
        watch = Step(
            id='s',
            raw=RawBlock(method='POST', path='/', body={'jsonrpc': '2.0'}),
            expect_stream={'min_count': 1},
            capture={'taskId': 'result.task.id'},
        )
        after = Step(id='after', operation=Operation.GET_TASK, params={'id': '{{s.taskId}}'})
        test = a_test(watch, after, transport=[TransportBinding.JSONRPC])
        assert run(runner_for(dispatcher), test).result is Outcome.PASS
        assert dispatcher.calls[-1][1] == {'id': 'T7'}

    def test_a_failed_stream_assertion_is_reported_before_a_missed_capture(self):
        dispatcher = FakeDispatcher(
            events=[{'task': {'id': 'T1', 'status': {'state': 'TASK_STATE_WORKING'}}}]
        )
        step = self.subscribe(
            expect_stream={'events': [{'index': 0, 'task': {'generation': {'gte': 1}}}]},
            capture={'generation': 'task.generation'},
        )
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path == 'events[0].task.generation'

    def test_variables_reach_every_part_of_expect_stream(self):
        """An event is compared with the value a variable stands for, not
        with the text `{{id}}`, which no event could ever equal."""
        dispatcher = FakeDispatcher(
            events=[self.SNAPSHOT, status_update('TASK_STATE_COMPLETED')]
        )
        step = self.subscribe(expect_stream={
            'events': [{'index': 0, 'task': {'id': '{{id}}'}}],
            'each_event': {'any_of': [{'task': {'id': '{{id}}'}}, {'taskId': '{{id}}'}]},
            'final_event': {'status': {'state': '{{final}}'}},
            'streams': [{'final_event': {'taskId': '{{id}}'}}],
        })
        variables = {'id': 'T1', 'final': 'TASK_STATE_COMPLETED'}
        assert run(runner_for(dispatcher, variables=variables), a_test(step)).result is Outcome.PASS
        for wrong in ({'id': 'OTHER'}, {'final': 'TASK_STATE_FAILED'}):
            runner = runner_for(dispatcher, variables={**variables, **wrong})
            assert run(runner, a_test(step)).result is Outcome.FAIL

    @pytest.mark.parametrize(
        ('second', 'outcome'), [('3', Outcome.PASS), ('2', Outcome.FAIL)]
    )
    def test_a_stream_is_asserted_with_what_it_captured_from_its_first_event(
        self, second, outcome
    ):
        """How a test says "everything after the snapshot is newer than it"."""
        dispatcher = FakeDispatcher(
            events=[self.SNAPSHOT, status_update('TASK_STATE_COMPLETED', generation=second)]
        )
        step = self.subscribe(
            capture={'generation': 'task.generation'},
            expect_stream={'each_event': {'any_of': [
                {'task': {'generation': {'gte': '{{s.generation}}'}}},
                {'status_update': {'generation': {'gt': '{{s.generation}}'}}},
            ]}},
        )
        assert run(runner_for(dispatcher), a_test(step)).result is outcome

    def test_a_stream_asserted_with_a_capture_it_could_not_read_fails_on_the_capture(self):
        """The SUT sent a first event without the value. That is a failure of
        the SUT, and must not read as a test naming an undefined variable."""
        dispatcher = FakeDispatcher(
            events=[{'task': {'id': 'T1', 'status': {'state': 'TASK_STATE_WORKING'}}}]
        )
        step = self.subscribe(
            capture={'generation': 'task.generation'},
            expect_stream={'each_event': {'generation': {'gte': '{{s.generation}}'}}},
        )
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.FAIL
        assert "cannot capture 'generation'" in result.failure.message

    def test_ordering_rules_reach_the_stream_through_a_step(self):
        """A list of rules, on a step whose block is resolved against the
        scope before it is evaluated."""
        events = [
            self.SNAPSHOT,
            status_update('TASK_STATE_WORKING', generation='3'),
            status_update('TASK_STATE_COMPLETED', generation='5'),
        ]
        step = self.subscribe(expect_stream={
            'ordering': ['monotonic_state', 'contiguous_generation'],
            'events': [{'index': 0, 'task': {'id': '{{id}}'}}],
        })
        result = run(
            runner_for(FakeDispatcher(events=events), variables={'id': 'T1'}), a_test(step)
        )
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path == 'events[2].generation'
        events[2] = status_update('TASK_STATE_COMPLETED', generation='4')
        result = run(
            runner_for(FakeDispatcher(events=events), variables={'id': 'T1'}), a_test(step)
        )
        assert result.result is Outcome.PASS

    def test_an_undefined_variable_in_expect_stream_is_an_error(self):
        dispatcher = FakeDispatcher(events=[self.SNAPSHOT])
        step = self.subscribe(
            expect_stream={'events': [{'index': 0, 'task': {'id': '{{nope}}'}}]}
        )
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.ERROR
        assert "undefined variable 'nope'" in result.failure.message

    # -- stays_open_ms ---------------------------------------------------------

    WAITING = [status_update('TASK_STATE_WORKING'), status_update('TASK_STATE_INPUT_REQUIRED')]

    def read(self, dispatcher, step, **kwargs):
        """Run on virtual time: a stream that stalls costs no real waiting."""
        return run_virtual(runner_for(dispatcher, **kwargs).run_test(a_test(step)))

    def test_a_stream_still_open_after_its_last_event_passes(self):
        dispatcher = FakeDispatcher(events=self.WAITING, stall=3600.0)
        step = self.subscribe(expect_stream={
            'stays_open_ms': 3000,
            'final_event': {'status': {'state': 'TASK_STATE_INPUT_REQUIRED'}},
        })
        result = self.read(dispatcher, step)
        assert result.result is Outcome.PASS
        assert result.steps[0].checks == 2

    def test_a_stream_the_sut_closes_fails(self):
        """What a stream read to its end cannot show: that it did not end."""
        dispatcher = FakeDispatcher(events=self.WAITING)
        step = self.subscribe(expect_stream={
            'stays_open_ms': 3000,
            'final_event': {'status': {'state': 'TASK_STATE_INPUT_REQUIRED'}},
        })
        result = self.read(dispatcher, step)
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path == 'stream'
        assert result.failure.message == (
            'the SUT closed the stream after 2 event(s); it had to stay open '
            'for 3000ms after its last event'
        )

    def test_a_stream_closed_before_any_event_fails(self):
        dispatcher = FakeDispatcher(events=[])
        step = self.subscribe(expect_stream={'stays_open_ms': 3000})
        result = self.read(dispatcher, step)
        assert result.result is Outcome.FAIL
        assert 'closed the stream after 0 event(s)' in result.failure.message

    def test_a_stream_that_closes_within_the_wait_fails(self):
        """Open for two seconds is not open for three."""
        dispatcher = FakeDispatcher(events=self.WAITING, stall=2.0)
        step = self.subscribe(expect_stream={'stays_open_ms': 3000})
        assert self.read(dispatcher, step).result is Outcome.FAIL

    def test_the_other_assertions_still_apply_to_the_events_that_arrived(self):
        dispatcher = FakeDispatcher(events=self.WAITING, stall=3600.0)
        step = self.subscribe(expect_stream={
            'stays_open_ms': 3000,
            'final_event': {'status': {'state': 'TASK_STATE_COMPLETED'}},
        })
        result = self.read(dispatcher, step)
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path == 'final_event.status.state'

    def test_the_wait_starts_after_an_event_and_not_before_the_first(self):
        """A stream that opens and sends nothing has no last event to be open
        after. It is bounded by the step's deadline, like any other read."""
        dispatcher = FakeDispatcher(events=[], stall=3600.0)
        step = self.subscribe(expect_stream={'stays_open_ms': 3000, 'timeout_ms': 10_000})
        result = self.read(dispatcher, step)
        assert result.result is Outcome.FAIL
        assert 'did not complete within 10000ms' in result.failure.message

    def test_the_runner_stops_reading_once_the_stream_is_shown_open(self):
        """It does not wait out the step's deadline as well."""
        dispatcher = FakeDispatcher(events=self.WAITING, stall=3600.0)
        step = self.subscribe(expect_stream={'stays_open_ms': 3000, 'timeout_ms': 60_000})
        elapsed: list[float] = []

        async def timed():
            loop = asyncio.get_running_loop()
            result = await runner_for(dispatcher).run_test(a_test(step))
            elapsed.append(loop.time())
            return result

        assert run_virtual(timed()).result is Outcome.PASS
        assert elapsed == [pytest.approx(3.0)]

    def test_a_streams_own_wait_does_not_shorten_the_steps(self):
        """The SUT closes the stream a second after its last event: long
        enough for the stream's own bound, too soon for the step's."""
        dispatcher = FakeDispatcher(events=self.WAITING, stall=1.0)
        step = self.subscribe(expect_stream={
            'stays_open_ms': 3000, 'streams': [{'stays_open_ms': 500}],
        })
        result = run_virtual(runner_for(dispatcher).run_test(a_test(step)))
        assert result.result is Outcome.FAIL
        assert 'the SUT closed the stream' in result.failure.message

    def test_one_stream_of_a_set_may_have_to_stay_open(self):
        dispatcher = FakeDispatcher(events=self.WAITING)
        step = self.subscribe(expect_stream={
            'streams': [{'description': 'closes'}, {'description': 'stays', 'stays_open_ms': 3000}],
        })
        test = a_test(step, runner_requirements=['concurrent_streams'])
        runner = runner_for(dispatcher, capabilities=[RunnerRequirement.CONCURRENT_STREAMS])
        result = run_virtual(runner.run_test(test))
        assert result.result is Outcome.FAIL
        assert result.failure.message.startswith('stays: the SUT closed the stream')

    def test_repeat_on_a_streaming_step_is_an_error(self):
        dispatcher = FakeDispatcher(events=[status_update('TASK_STATE_COMPLETED')])
        step = self.stream_step(
            expect_stream={'min_count': 1}, repeat={'until': 'a == b'}
        )
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.ERROR
        assert 'repeat' in result.failure.message

    def test_a_stream_that_dies_partway_is_an_error(self):
        dispatcher = FakeDispatcher(raises=DispatchError('stream failed after 2'))
        step = self.stream_step(expect_stream={'min_count': 1})
        assert run(runner_for(dispatcher), a_test(step)).result is Outcome.ERROR

    def test_a_wrongly_shaped_reply_is_a_failure_not_an_error(self):
        """A streaming call answered with `application/json` is a finding about
        the SUT; filing it as a broken run would hide it behind our name."""
        dispatcher = FakeDispatcher(
            raises=MalformedResponse("Expected 'text/event-stream', got 'application/json'")
        )
        step = self.stream_step(expect_stream={'min_count': 1})
        result = run(runner_for(dispatcher), a_test(step))
        assert result.result is Outcome.FAIL
        assert 'text/event-stream' in result.failure.message


class StreamRecorder(Dispatcher):
    """Reports how its streams were *used*, not only what they carried.

    Concurrency and disconnection are facts about the connections; a
    dispatcher handing back a list of events shows neither.

    The streams rendezvous at a barrier before sending anything, so a runner
    that opens them one at a time leaves the first waiting alone and sets
    `serialized` — a failure in a second rather than a hang.
    """

    binding = TransportBinding.JSONRPC

    def __init__(self, events: list[Any], *, concurrent: int = 1) -> None:
        self._events = events
        self._concurrent = concurrent
        self._barrier: asyncio.Barrier | None = None
        self.serialized = False
        self.requests: list[tuple[Operation, Mapping[str, Any]]] = []
        #: Events actually handed over, per stream, in the order they opened.
        self.delivered: list[int] = []
        #: Indices of streams the runner hung up on.
        self.hung_up: list[int] = []
        #: `delivered`, as it stood the moment each hang-up arrived. *When* is
        #: the whole question: an abandoned generator is closed too, but not
        #: until the loop shuts down and every other stream has finished.
        self.hung_up_at: list[list[int]] = []

    async def stream(self, operation, params=None, headers=None):
        index = len(self.requests)
        self.requests.append((operation, dict(params or {})))
        self.delivered.append(0)

        if self._concurrent > 1:
            if self._barrier is None:
                self._barrier = asyncio.Barrier(self._concurrent)
            try:
                await asyncio.wait_for(self._barrier.wait(), 1.0)
            except (asyncio.TimeoutError, TimeoutError):
                self.serialized = True

        try:
            for position, data in enumerate(self._events):
                # Before the yield: a hang-up never resumes the generator, so
                # counting after it would record the cut stream as sent nothing.
                self.delivered[index] += 1
                yield StreamEvent(index=position, data=data, status=200)
                # Let the other streams run, so a hang-up on one is observed
                # while the rest are still being read.
                await asyncio.sleep(0)
        except GeneratorExit:
            self.hung_up.append(index)
            self.hung_up_at.append(list(self.delivered))
            raise

    async def dispatch(self, operation, params=None, headers=None):
        return WireResponse(status=200, payload={})

    async def dispatch_raw(self, raw, headers=None):
        return WireResponse(status=200, payload={})

    async def stream_raw(self, raw, headers=None):
        raise UnsupportedByBinding('not used')
        yield  # pragma: no cover - makes this a generator


class TestConcurrentStreams:
    """`expect_stream.streams` — ACTS §7.3, `concurrent_streams`."""

    def subscribe(self, **kwargs):
        return Step(
            id='subscribe',
            operation=Operation.SUBSCRIBE_TO_TASK,
            params={'id': 'T1'},
            **kwargs,
        )

    def a_concurrent_test(self, *steps):
        return a_test(*steps, runner_requirements=['concurrent_streams'])

    def capable(self, dispatcher):
        return runner_for(dispatcher, capabilities=[
            RunnerRequirement.CONCURRENT_STREAMS, RunnerRequirement.STREAM_DISCONNECT,
        ])

    def test_both_streams_are_open_at_the_same_time(self):
        """The claim `STREAM-MULTI-001` makes, and the one no single stream
        can make however many events it collects."""
        dispatcher = StreamRecorder(
            [status_update('TASK_STATE_COMPLETED')], concurrent=2
        )
        step = self.subscribe(expect_stream={
            'min_count': 1, 'streams': [{}, {}],
        })
        result = run(self.capable(dispatcher), self.a_concurrent_test(step))
        assert not dispatcher.serialized
        assert len(dispatcher.requests) == 2
        assert result.result is Outcome.PASS

    def test_every_stream_sends_the_identical_request(self):
        """Rebuilding the params per stream would give each its own generated
        `messageId` — on `send_streaming_message`, a different task apiece."""
        dispatcher = StreamRecorder(
            [status_update('TASK_STATE_COMPLETED')], concurrent=2
        )
        step = Step(
            id='stream',
            operation=Operation.SEND_STREAMING_MESSAGE,
            params={'message': {'role': 'ROLE_USER'}},
            expect_stream={'min_count': 1, 'streams': [{}, {}]},
        )
        run(self.capable(dispatcher), self.a_concurrent_test(step))
        first, second = dispatcher.requests
        assert first == second

    def test_the_shared_block_is_asserted_against_every_stream(self):
        dispatcher = StreamRecorder([status_update('TASK_STATE_WORKING')], concurrent=2)
        step = self.subscribe(expect_stream={'min_count': 2, 'streams': [{}, {}]})
        result = run(self.capable(dispatcher), self.a_concurrent_test(step))
        assert result.result is Outcome.FAIL
        assert 'at least 2' in result.failure.message

    def test_a_per_stream_assertion_only_binds_its_own_stream(self):
        dispatcher = StreamRecorder([status_update('TASK_STATE_WORKING')], concurrent=2)
        step = self.subscribe(expect_stream={'streams': [
            {'description': 'lenient', 'min_count': 1},
            {'description': 'strict', 'min_count': 9},
        ]})
        result = run(self.capable(dispatcher), self.a_concurrent_test(step))
        assert result.result is Outcome.FAIL
        assert 'at least 9' in result.failure.message

    def test_a_failure_names_the_stream_that_produced_it(self):
        """The members of a set are not interchangeable, so "got 1" has to
        say which."""
        dispatcher = StreamRecorder([status_update('TASK_STATE_WORKING')], concurrent=2)
        step = self.subscribe(expect_stream={'streams': [
            {'description': 'the lenient one', 'min_count': 1},
            {'description': 'the strict one', 'min_count': 9},
        ]})
        result = run(self.capable(dispatcher), self.a_concurrent_test(step))
        assert result.failure.message.startswith('the strict one: ')
        assert result.failure.assertion_path.startswith('streams[1].')

    def test_one_stream_reads_exactly_as_it_did_before_streams_existed(self):
        """A single stream must not start reporting itself as `streams[0]`."""
        dispatcher = StreamRecorder([status_update('TASK_STATE_WORKING')])
        step = self.subscribe(expect_stream={'min_count': 4})
        result = run(self.capable(dispatcher), a_test(step))
        assert result.failure.message == 'expected at least 4 event(s), got 1'


class TestStreamDisconnect:
    """`disconnect_after` — ACTS §7.3, `stream_disconnect`."""

    def subscribe(self, step_id='subscribe', **kwargs):
        return Step(
            id=step_id,
            operation=Operation.SUBSCRIBE_TO_TASK,
            params={'id': 'T1'},
            **kwargs,
        )

    def capable(self, dispatcher):
        return runner_for(dispatcher, capabilities=[
            RunnerRequirement.CONCURRENT_STREAMS, RunnerRequirement.STREAM_DISCONNECT,
        ])

    def test_reading_stops_at_the_declared_event(self):
        dispatcher = StreamRecorder([status_update('TASK_STATE_WORKING')] * 20)
        step = self.subscribe(expect_stream={
            'min_count': 1, 'streams': [{'disconnect_after': 1}],
        })
        test = a_test(step, runner_requirements=['stream_disconnect'])
        assert run(self.capable(dispatcher), test).result is Outcome.PASS
        assert dispatcher.delivered == [1]

    def test_a_stream_read_to_the_end_is_not_reported_as_hung_up(self):
        dispatcher = StreamRecorder([status_update('TASK_STATE_COMPLETED')])
        step = self.subscribe(expect_stream={'min_count': 1})
        run(self.capable(dispatcher), a_test(step))
        assert dispatcher.hung_up == []

    def test_hanging_up_on_one_stream_leaves_the_others_reading(self):
        """`STREAM-MULTI-002`, in miniature.

        `hung_up_at` is the load-bearing assertion, and why this test is
        concurrent: closure *eventually* proves nothing, since
        `shutdown_asyncgens` closes an abandoned generator too. Only a real
        hang-up lands while the other stream is mid-flight — `[1, 1]`, not
        `[1, 4]`.
        """
        events = [status_update('TASK_STATE_WORKING')] * 3 + [
            status_update('TASK_STATE_COMPLETED')
        ]
        dispatcher = StreamRecorder(events, concurrent=2)
        step = self.subscribe(expect_stream={
            'min_count': 1,
            'streams': [
                {'description': 'hangs up', 'disconnect_after': 1},
                {
                    'description': 'stays',
                    'final_event': {'status': {'state': 'TASK_STATE_COMPLETED'}},
                },
            ],
        })
        test = a_test(
            step, runner_requirements=['concurrent_streams', 'stream_disconnect']
        )
        result = run(self.capable(dispatcher), test)
        assert result.result is Outcome.PASS
        assert dispatcher.delivered == [1, 4]
        assert dispatcher.hung_up_at == [[1, 1]]

    def test_a_disconnect_needs_a_stream_that_can_be_closed(self):
        """Rather than silently reading `disconnect_after` events and calling
        that a disconnect."""
        dispatcher = FakeDispatcher(events=[status_update('TASK_STATE_WORKING')] * 5)

        def uncloseable(*args, **kwargs):
            async def once():
                yield StreamEvent(index=0, data=status_update('TASK_STATE_WORKING'))

            class Iterator:
                def __init__(self) -> None:
                    self._inner = once()

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    return await self._inner.__anext__()

            return Iterator()

        dispatcher.stream = uncloseable
        step = self.subscribe(expect_stream={
            'min_count': 1, 'streams': [{'disconnect_after': 1}],
        })
        result = run(
            self.capable(dispatcher),
            a_test(step, runner_requirements=['stream_disconnect']),
        )
        assert result.result is Outcome.ERROR
        assert 'cannot disconnect mid-stream' in result.failure.message


class TestClientSteps:
    """ACTS §10, delivered through the `tck-client-parse` behaviour."""

    def a_client_test(self, expect_parsed, operation='send_message'):
        return a_test(
            Step(
                id='parse',
                client_response={
                    'operation': operation,
                    'wire_payload': {'jsonrpc': '2.0', 'id': '1', 'result': {}},
                },
                expect_parsed=expect_parsed,
            )
        )

    def replying_with(self, parsed):
        """A SUT that implements the behaviour and returns ``parsed``."""
        return FakeDispatcher(ok({
            'task': {
                'id': 'T1',
                'status': {'state': 'TASK_STATE_COMPLETED'},
                'artifacts': [{'artifactId': 'a1', 'parts': [{'data': parsed}]}],
            }
        }))

    def test_the_parsed_payload_is_asserted(self):
        dispatcher = self.replying_with({'task': {'id': 'task-abc-123'}})
        test = self.a_client_test({'task': {'id': 'task-abc-123'}})
        assert run(runner_for(dispatcher), test).result is Outcome.PASS

    def test_a_wrong_parse_fails_and_locates_itself(self):
        dispatcher = self.replying_with({'task': {'id': 'WRONG'}})
        result = run(runner_for(dispatcher), self.a_client_test({'task': {'id': 'T'}}))
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path == 'parsed.task.id'

    def test_it_goes_out_as_a_send_message_naming_the_behaviour(self):
        """§10 gives no mechanism, so the payload rides a normal operation."""
        dispatcher = self.replying_with({'task': {}})
        run(runner_for(dispatcher), self.a_client_test({'task': {'exists': True}}))
        operation, params, _ = dispatcher.calls[0]
        assert operation is Operation.SEND_MESSAGE
        parts = params['message']['parts']
        assert parts[0]['text'] == CLIENT_PARSE_BEHAVIOR
        assert parts[1]['data']['operation'] == 'send_message'
        assert parts[1]['data']['wire_payload']['jsonrpc'] == '2.0'

    def test_a_sut_that_ignores_the_behaviour_fails(self):
        """Not a silent pass: it demonstrated no parsing at all."""
        dispatcher = FakeDispatcher(ok({'task': {'id': 'T1', 'artifacts': []}}))
        result = run(runner_for(dispatcher), self.a_client_test({'task': {}}))
        assert result.result is Outcome.FAIL
        assert CLIENT_PARSE_BEHAVIOR in result.failure.message

    def test_an_error_from_the_sut_fails_the_step(self):
        dispatcher = FakeDispatcher(failed(ErrorType.INVALID_PARAMS))
        result = run(runner_for(dispatcher), self.a_client_test({'task': {}}))
        assert result.result is Outcome.FAIL
        assert 'could not parse' in result.failure.message

    def test_a_parsed_error_envelope_is_assertable(self):
        """`CLIENT-PARSE-004` asserts the client surfaced the JSON-RPC error."""
        dispatcher = self.replying_with(
            {'error': {'code': -32001, 'message': 'Task not found'}}
        )
        test = self.a_client_test(
            {'error': {'code': -32001, 'message': {'contains': 'not found'}}},
            operation='get_task',
        )
        assert run(runner_for(dispatcher), test).result is Outcome.PASS


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
        dispatcher = FakeDispatcher(
            ok({}),
            binding=binding,
            events=[status_update('TASK_STATE_COMPLETED')],
        )
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

    @pytest.mark.parametrize('binding', list(TransportBinding))
    def test_nothing_is_deferred_any_more(self, suite, binding):
        """Operation, raw and client steps all execute.

        `assertion` is the one kind still outside `EXECUTABLE_KINDS`, so this
        holds because the corpus contains none of them, not because every kind
        has an execution path — hence the second assertion.
        """
        results = self._run_all(suite, binding)
        deferred = [
            r for r in results
            if r.result is Outcome.SKIP and 'not yet supported' in (r.skip_reason or '')
        ]
        assert deferred == []
        assert not any(
            step.kind() not in EXECUTABLE_KINDS
            for loaded in suite.tests for step in loaded.test.steps
        )

    @pytest.mark.parametrize('binding', list(TransportBinding))
    def test_no_raw_test_reaches_the_grpc_dispatcher(self, suite, binding):
        """Every raw test in the corpus is jsonrpc- or rest-restricted.

        Which is why none errors on gRPC with `UnsupportedByBinding`: they are
        filtered out by transport before they get that far. Asserting only
        "nothing errored" would pass for the wrong reason, so this checks the
        filtering directly.
        """
        raw_tests = [
            loaded.test for loaded in suite.tests
            if any(step.kind() is StepKind.RAW for step in loaded.test.steps)
        ]
        assert raw_tests
        for test in raw_tests:
            assert test.transport, f'{test.id} has a raw step but no transport'
            assert TransportBinding.GRPC not in test.transport

        results = self._run_all(suite, binding)
        assert not any(
            r.result is Outcome.ERROR for r in results
        ), [r.id for r in results if r.result is Outcome.ERROR]

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


class TestWebhookSteps:
    """Assertions over what the SUT pushed, not over a reply to the runner.

    Five tests used to register a push config and assert nothing about
    delivery, so an SDK that stored the config and never called back passed
    two MUSTs. `expect_webhook` is what closes that, and these are the checks
    that it can actually fail.
    """

    def a_webhook_test(self, **block):
        block.setdefault('task_id', 'T1')
        return a_test(
            Step(id='delivered', expect_webhook=block),
            runner_requirements=[RunnerRequirement.WEBHOOK_ENDPOINT],
        )

    def receiver(self, notifications):
        async def read(task_id):
            assert task_id == 'T1'
            return notifications
        return read

    def runner(self, **kwargs):
        """Offers `webhook_endpoint`, which spec §7.2 obliges every test here
        to declare — without it they would skip on the gate rather than run.
        """
        return runner_for(
            FakeDispatcher(),
            capabilities=[RunnerRequirement.WEBHOOK_ENDPOINT],
            **kwargs,
        )

    def test_a_delivery_that_arrived_passes(self):
        runner = self.runner(
            read_webhook=self.receiver([{'event': {'task': {'id': 'T1'}}}]),
        )
        result = run(runner, self.a_webhook_test(each_event={'task': {'id': 'T1'}}))
        assert result.result is Outcome.PASS

    def test_a_delivery_is_addressed_like_a_stream_event(self):
        """The wire writes `statusUpdate`; a test may write `status_update`,
        or address the payload's fields without naming the kind."""
        delivered = [{'event': {'statusUpdate': {
            'taskId': 'T1', 'generation': '3',
            'status': {'state': 'TASK_STATE_COMPLETED'},
        }}}]
        runner = self.runner(read_webhook=self.receiver(delivered))
        for assertion in (
            {'status_update': {'generation': {'gte': 1}}},
            {'statusUpdate': {'taskId': 'T1'}},
            {'status': {'state': 'TASK_STATE_COMPLETED'}},
        ):
            for part in ('each_event', 'final_event'):
                result = run(runner, self.a_webhook_test(**{part: assertion}))
                assert result.result is Outcome.PASS, (part, assertion)
        result = run(
            runner,
            self.a_webhook_test(final_event={'status': {'state': 'TASK_STATE_FAILED'}}),
        )
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path == 'webhook.final_event[0].status.state'

    @pytest.mark.parametrize('spelling', ['message_update', 'messageUpdate'])
    def test_a_kind_that_must_not_be_delivered(self, spelling):
        """How a test checks that a configuration created for A2A 1.0 is sent
        none of the kinds A2A 1.1 added. It must fail when one arrives,
        however the test spelled the kind."""
        never = {spelling: {'absent': True}}
        update = [{'event': {'messageUpdate': {'taskId': 'T1', 'message': {'messageId': 'm'}}}}]
        result = run(
            self.runner(read_webhook=self.receiver(update)),
            self.a_webhook_test(each_event=never),
        )
        assert result.result is Outcome.FAIL
        status = [{'event': {'statusUpdate': {'taskId': 'T1', 'status': {}}}}]
        result = run(
            self.runner(read_webhook=self.receiver(status)),
            self.a_webhook_test(each_event=never),
        )
        assert result.result is Outcome.PASS

    def test_no_delivery_fails_and_says_how_many_arrived(self):
        """The whole point: silence has to be a failure, not a pass."""
        runner = self.runner(read_webhook=self.receiver([]))
        result = run(runner, self.a_webhook_test(timeout_ms=0))
        assert result.result is Outcome.FAIL
        assert 'delivered 0 notification' in result.failure.message
        assert result.failure.assertion_path == 'webhook.min_count'

    def test_a_wrong_payload_fails_and_locates_itself(self):
        runner = self.runner(
            read_webhook=self.receiver([{'event': {'task': {'id': 'OTHER'}}}]),
        )
        result = run(runner, self.a_webhook_test(each_event={'task': {'id': 'T1'}}))
        assert result.result is Outcome.FAIL
        assert result.failure.assertion_path == 'webhook.each_event[0].task.id'

    def test_a_missing_credential_header_fails(self):
        """A2A §4.3.3 makes the `authentication` credentials a MUST on the
        push request, and an anonymous delivery is what SEC-PUSH-001 exists
        to catch."""
        runner = self.runner(
            read_webhook=self.receiver([{'event': {'task': {}}, 'headers': {}}]),
        )
        result = run(runner, self.a_webhook_test(headers={'Authorization': 'Bearer t'}))
        assert result.result is Outcome.FAIL
        assert 'headers' in result.failure.assertion_path

    def test_the_credential_header_passes_and_folds_case(self):
        """HTTP header names are case-insensitive, and the receiver
        lowercases what it captured."""
        runner = self.runner(
            read_webhook=self.receiver([
                {'event': {'task': {}}, 'headers': {'authorization': 'Bearer t'}},
            ]),
        )
        result = run(runner, self.a_webhook_test(headers={'Authorization': 'Bearer t'}))
        assert result.result is Outcome.PASS

    def test_the_token_header_is_available_but_unasserted_by_the_corpus(self):
        """A2A defines `PushNotificationConfig.token` but no header to carry
        it: a2a-python always sends `X-A2A-Notification-Token`, a2a-js sends
        it only when `authentication` is absent, and neither is wrong. The
        assertion stays available for a runner that knows its SUT's
        convention; no corpus test may require it."""
        runner = self.runner(
            read_webhook=self.receiver([{'event': {'task': {}}, 'token': 'expected'}]),
        )
        assert run(runner, self.a_webhook_test(token='expected')).result is Outcome.PASS

    def test_without_a_receiver_the_run_errors_rather_than_blaming_the_sut(self):
        """Now reachable only by a runner offering `webhook_endpoint` with no
        receiver behind it: the schema rejects an undeclared test, and the gate
        skips a runner that lacks the capability. A runner's own inconsistency
        must not read as a SUT defect."""
        result = run(self.runner(), self.a_webhook_test())
        assert result.result is Outcome.ERROR
        assert 'webhook_endpoint' in (result.failure.message or '')
