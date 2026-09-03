"""Execute ACTS tests against one SUT (spec §9, §12, §13.3).

The runner is the part that decides *what happened*. Everything it needs in
order to decide already exists: `loader.py` produced the plan, `dispatcher/`
performs a call on one binding, and `assertions.py` and `variables.py` say
what a response means. This module sequences them and turns the outcome into
results the report writer can serialize.

Two distinctions carry most of the design.

**Fail is not error.** A `fail` is a statement about the SUT: it answered, and
its answer did not conform. An `error` is a statement about the run: the call
could not be completed, or the test referred to a variable nobody defined.
Reporting the second as the first would put a bug in our runner into someone
else's conformance record, so the two are kept apart everywhere.

**Skip is not fail either.** A SUT that does not advertise push notifications
is not non-conformant for lacking them (§12.5), and neither is one reached
over a binding the test does not target (§12.3). Missing `tck-*` behaviors are
the deliberate exception: those **fail**, because a skip would let an SDK's
lagging support disappear quietly from its own report.

Scope: the non-streaming, non-raw path. A test with a `raw`, `client_response`
or streaming step is skipped with a reason naming the gap, which is 40 of the
corpus's 111 tests; the rest of the machinery — polling, capture, scope
isolation, the result model — is complete and those step kinds plug into it.
"""

from __future__ import annotations

import asyncio
import enum
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, TypeVar

from pydantic import BaseModel

from test_suite.acts.assertions import (
    AssertionResult,
    Failure,
    UntilError,
    evaluate_body,
    evaluate_error,
    evaluate_named,
    evaluate_status,
    evaluate_until,
)
from test_suite.acts.dispatcher.base import (
    DispatchError,
    Dispatcher,
    MalformedResponse,
    StreamEvent,
    WireError,
    WireResponse,
)
from test_suite.acts.dispatcher.params import adapt
from test_suite.acts.loader import LoadedSuite
from test_suite.acts.schema import (
    Backoff,
    Level,
    Operation,
    RawBlock,
    RunnerRequirement,
    Step,
    StepKind,
    Test,
    TransportBinding,
)
from test_suite.acts.streaming import StreamedEvent, evaluate_stream, normalize
from test_suite.acts.wire_map import binding_for_operation
from test_suite.acts.variables import PathError, Scope, UnresolvedVariable


_M = TypeVar('_M', bound=BaseModel)

#: Spec §9's defaults for a `repeat` block that does not give its own.
DEFAULT_MAX_ATTEMPTS = 10
DEFAULT_DELAY_MS = 1000

#: §12.4 requires this on every request, carrying the document's
#: `spec_version`. On gRPC the dispatcher turns it into call metadata.
VERSION_HEADER = 'A2A-Version'

#: Step kinds the runner can execute. `client_response` (spec §10) is not one:
#: it feeds a canned payload to the SUT's own *client* and asserts on what that
#: client parsed, which needs a client-side entry point on the agent rather
#: than anything the runner can drive over a transport. No story owns it yet.
EXECUTABLE_KINDS: Final[frozenset[StepKind]] = frozenset(
    {StepKind.OPERATION, StepKind.RAW}
)


class Outcome(str, enum.Enum):
    """Per-test and per-step result (spec §13.3)."""

    PASS = 'pass'
    FAIL = 'fail'
    SKIP = 'skip'
    ERROR = 'error'


@dataclass(frozen=True, slots=True)
class FailureDetail:
    """Why something failed, shaped as §13.3's `failure-detail`."""

    message: str
    step_id: str | None = None
    expected: str | None = None
    actual: str | None = None
    assertion_path: str | None = None

    @classmethod
    def from_assertion(cls, failure: Failure, step_id: str | None = None) -> FailureDetail:
        detail = failure.as_detail()
        return cls(
            message=detail['message'],
            step_id=step_id,
            expected=detail.get('expected'),
            actual=detail.get('actual'),
            assertion_path=detail.get('assertion_path'),
        )

    def as_json(self) -> dict[str, str]:
        """Drop the fields §13.3 marks optional when they are unset."""
        out = {'message': self.message}
        for name in ('step_id', 'expected', 'actual', 'assertion_path'):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out


@dataclass(frozen=True, slots=True)
class StepResult:
    """One executed step.

    `attempts` and `checks` are ours rather than §13.3's: the first says
    whether a poll converged or ran out, the second how many comparisons
    actually ran. The report writer emits only the spec's fields.
    """

    id: str
    result: Outcome
    duration_ms: int
    failure: FailureDetail | None = None
    attempts: int = 1
    checks: int = 0


@dataclass(frozen=True, slots=True)
class TestResult:
    """One test's outcome (spec §13.3 `test-result`)."""

    # Named for the domain; without this pytest collects it wherever it is
    # imported, same as `schema.Test`.
    __test__ = False

    id: str
    name: str
    level: Level
    result: Outcome
    duration_ms: int
    skip_reason: str | None = None
    failure: FailureDetail | None = None
    steps: tuple[StepResult, ...] = ()


def _delay_seconds(base_ms: int, attempt: int, backoff: Backoff) -> float:
    """How long to wait before attempt ``attempt + 1``."""
    if backoff is Backoff.LINEAR:
        return base_ms * attempt / 1000
    if backoff is Backoff.EXPONENTIAL:
        return base_ms * (2 ** (attempt - 1)) / 1000
    return base_ms / 1000


def _error_info(error: WireError) -> Mapping[str, Any] | None:
    """The `google.rpc.ErrorInfo` among an error's details, if it carried one."""
    for detail in error.details:
        if isinstance(detail, Mapping) and 'ErrorInfo' in str(detail.get('@type', '')):
            return detail
    return None


def _observed_error(error: WireError) -> dict[str, Any]:
    """An error as an assertion target for `expect_error` (spec §6.2).

    `error_type` is present only when the wire actually named the error — a
    JSON-RPC code or an `ErrorInfo.reason`. Leaving it out where the SUT gave
    nothing to map is deliberate: `MISSING` fails an `error_type` assertion,
    where inventing a plausible name would quietly pass one.
    """
    observed: dict[str, Any] = {'message': error.message}
    if error.error_type is not None:
        observed['error_type'] = error.error_type.value
    if error.code is not None:
        observed['code'] = error.code
    if isinstance(error.raw, Mapping) and 'data' in error.raw:
        observed['data'] = error.raw['data']  # JSON-RPC `error.data`
    elif error.details:
        observed['data'] = list(error.details)  # REST / gRPC `details[]`
    info = _error_info(error)
    if info is not None:
        observed['details'] = info
    return observed


class Runner:
    """Runs tests against one SUT over one binding.

    One instance per (SUT, binding) pair: `dispatcher.binding` decides which
    tests apply, so running the same suite over three bindings means three
    runners. Each *test* gets a fresh :class:`~test_suite.acts.variables.Scope`
    — that is the isolation boundary (§3.2), and it is why a leaked capture
    cannot make one test depend on another having run first.
    """

    def __init__(
        self,
        dispatcher: Dispatcher,
        *,
        variables: Mapping[str, Any] | None = None,
        spec_version: str = '1.0',
        agent_card: Mapping[str, Any] | None = None,
        sut_behaviors: Iterable[str] | None = None,
        capabilities: Iterable[RunnerRequirement] = (),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        new_uuid: Callable[[], str] | None = None,
    ) -> None:
        self.dispatcher = dispatcher
        #: Overlaid on the document's own `variables`, so a runner-supplied
        #: value wins. The corpus references two variables no document
        #: defines and they can only arrive this way.
        self.variables = dict(variables or {})
        self.spec_version = spec_version
        self.agent_card = agent_card
        #: The `tck-*` prefixes the SUT declares (story 4.5's
        #: `sut-behaviors.yaml`). ``None`` means no contract was supplied, so
        #: behavior gating is off rather than failing every test that needs one.
        self.sut_behaviors = None if sut_behaviors is None else frozenset(sut_behaviors)
        self.capabilities = frozenset(capabilities)
        self._sleep = sleep
        self._clock = clock
        self._new_uuid = new_uuid if new_uuid is not None else lambda: str(uuid.uuid4())

    @property
    def binding(self) -> TransportBinding:
        return self.dispatcher.binding

    # -- running -----------------------------------------------------------

    async def run_suite(self, suite: LoadedSuite) -> list[TestResult]:
        """Run every test in ``suite``, in load order.

        Sequential on purpose. Tests share one SUT whose task list and push
        configurations are global to it, so `CORE-LIST-*` asserting on what
        `list_tasks` returns is only meaningful if nothing else is creating
        tasks at the same time.
        """
        document_variables = dict(suite.variables)
        document_variables.update(self.variables)
        return [
            await self.run_test(loaded.test, variables=document_variables)
            for loaded in suite.tests
        ]

    async def run_test(
        self, test: Test, *, variables: Mapping[str, Any] | None = None
    ) -> TestResult:
        """Run one test in its own variable scope."""
        started = self._clock()

        skip = self._skip_reason(test)
        if skip is not None:
            return self._result(test, Outcome.SKIP, started, skip_reason=skip)

        gap = self._behavior_gap(test)
        if gap is not None:
            return self._result(
                test,
                Outcome.FAIL,
                started,
                failure=FailureDetail(message=gap),
            )

        scope = Scope(
            {**(variables or {}), **self.variables}, new_uuid=self._new_uuid
        )

        steps: list[StepResult] = []
        for step in test.steps:
            result = await self._run_step(step, scope)
            steps.append(result)
            if result.result is not Outcome.PASS:
                # Later steps read this one's captures, so continuing produces
                # a cascade of failures that all describe the same cause.
                return self._result(
                    test, result.result, started,
                    failure=result.failure, steps=steps,
                )

        outcome = self._evaluate_named(test.assertions, scope, step_id=None)
        if outcome is not None:
            return self._result(test, Outcome.FAIL, started, failure=outcome, steps=steps)

        return self._result(test, Outcome.PASS, started, steps=steps)

    # -- gating ------------------------------------------------------------

    def _skip_reason(self, test: Test) -> str | None:
        """Why this test should not run at all — never why it failed."""
        if not test.applies_to(self.binding):
            targets = ', '.join(t.value for t in test.transport or ())
            return f'targets {targets}; this runner speaks {self.binding.value}'

        deferred = {
            step.kind() for step in test.steps if step.kind() not in EXECUTABLE_KINDS
        }
        if deferred:
            kinds = ', '.join(sorted(k.value for k in deferred))
            return f'contains {kinds} step(s); not yet supported'

        missing = set(test.runner_requirements or ()) - self.capabilities
        if missing:
            needed = ', '.join(sorted(r.value for r in missing))
            return f'runner does not provide {needed}'

        return self._unmet_precondition(test)

    def _unmet_precondition(self, test: Test) -> str | None:
        """Evaluate `preconditions` against the agent card (spec §12.5)."""
        preconditions = test.preconditions
        if preconditions is None:
            return None
        card = self.agent_card
        if card is None:
            raise RunError(
                f'{test.id} declares preconditions but no agent card is '
                f'available to evaluate them against'
            )

        advertised = card.get('capabilities') or {}
        for name, expected in (preconditions.capabilities or {}).items():
            actual = advertised.get(name, False)
            if isinstance(expected, bool):
                # A card that omits a capability has not advertised it, which
                # is what `false` is asking for.
                if bool(actual) is not expected:
                    return f'agent card capability {name}={bool(actual)}, needs {expected}'
            elif actual != expected:
                return f'agent card capability {name}={actual!r}, needs {expected!r}'

        wanted_skills = {s['id'] for s in (preconditions.skills or []) if 'id' in s}
        if wanted_skills:
            have = {s.get('id') for s in (card.get('skills') or [])}
            absent = wanted_skills - have
            if absent:
                return f'agent card lacks skill(s) {", ".join(sorted(absent))}'

        if preconditions.transport:
            have = {
                str(i.get('protocolBinding', '')).lower()
                for i in (card.get('supportedInterfaces') or [])
            }
            wanted = {t.value for t in preconditions.transport}
            if not wanted & have:
                return f'agent card advertises no binding among {", ".join(sorted(wanted))}'

        if preconditions.extensions:
            have = {
                e.get('uri') for e in (advertised.get('extensions') or [])
            }
            absent = set(preconditions.extensions) - have
            if absent:
                return f'agent card lacks extension(s) {", ".join(sorted(absent))}'

        return None

    def _behavior_gap(self, test: Test) -> str | None:
        """A `tck-*` prefix the SUT does not implement — a failure, not a skip.

        Deliberate: a skip would let an SDK's missing behavior vanish from its
        own conformance report, which is the opposite of what the report is
        for. ``sut_behaviors=None`` means no contract file was supplied, so
        there is nothing to check against yet.
        """
        if self.sut_behaviors is None:
            return None
        missing = test.behaviors() - self.sut_behaviors
        if not missing:
            return None
        return (
            f'SUT does not declare behavior(s) {", ".join(sorted(missing))}; '
            f'the test cannot be satisfied without them'
        )

    # -- steps -------------------------------------------------------------

    async def _run_step(self, step: Step, scope: Scope) -> StepResult:
        started = self._clock()

        if step.delay_ms:
            await self._sleep(step.delay_ms / 1000)

        try:
            if _is_streaming(step):
                return await self._run_streaming_step(step, scope, started)
            return await self._run_unary_step(step, scope, started)
        except MalformedResponse as exc:
            # The SUT answered in a shape the binding does not permit. That is
            # a finding about the SUT, so it must not be filed as a broken run.
            return self._step(step, Outcome.FAIL, started, message=str(exc))
        except DispatchError as exc:
            # The exchange did not complete, so the SUT has not been shown
            # non-conformant. `UnsupportedByBinding` arrives here too, which is
            # right: a raw step on gRPC is a test that should have declared a
            # transport, not a defect in the agent.
            return self._step(step, Outcome.ERROR, started, message=str(exc))

    async def _run_unary_step(
        self, step: Step, scope: Scope, started: float
    ) -> StepResult:
        """One request, one reply — an operation step or a raw one."""
        attempts = 1

        if step.kind() is StepKind.RAW:
            try:
                raw = _resolved(step.raw, scope)
            except (UnresolvedVariable, PathError) as exc:
                return self._step(step, Outcome.ERROR, started, message=str(exc))
            response = await self.dispatcher.dispatch_raw(raw, self._raw_headers())
        else:
            try:
                params = self._prepare_params(step, scope)
                until = (
                    None if step.repeat is None
                    else scope.substitute(step.repeat.until)
                )
            except (UnresolvedVariable, PathError) as exc:
                # The test names something nothing defines: a problem with the
                # inputs, not with the SUT.
                return self._step(step, Outcome.ERROR, started, message=str(exc))

            try:
                response, attempts, converged = await self._dispatch(step, params, until)
            except UntilError as exc:
                return self._step(step, Outcome.ERROR, started, message=str(exc))

            if not converged:
                return self._step(
                    step, Outcome.FAIL, started, attempts=attempts,
                    message=(
                        f'polled {attempts} time(s) and `{step.repeat.until}` '
                        f'never became true'
                    ),
                )

        scope.record_response(step.id, response.payload)
        captured = self._capture(step, scope, response.payload)
        if captured is not None:
            return self._step(
                step, Outcome.FAIL, started, attempts=attempts, message=captured
            )

        result = self._evaluate_outcome(step, response, scope)
        return self._finish(step, scope, result, started, attempts)

    async def _run_streaming_step(
        self, step: Step, scope: Scope, started: float
    ) -> StepResult:
        """A streaming operation, or a raw request answered with a stream."""
        if step.repeat is not None:
            return self._step(
                step, Outcome.ERROR, started,
                message='`repeat` cannot re-dispatch a streaming step',
            )

        try:
            source = self._stream_source(step, scope)
        except (UnresolvedVariable, PathError) as exc:
            return self._step(step, Outcome.ERROR, started, message=str(exc))

        try:
            events, status, timed_out = await self._collect(source, step.expect_stream)
        except DispatchError as exc:
            if step.expect_error is None:
                raise
            # A stream the SUT refuses to open *is* the expected outcome:
            # `STREAM-SUB-003` subscribes to a terminal task and requires an
            # error. The transport reports that as a failed call rather than
            # as a WireError, so there is no code to name — an `expect_error`
            # that asserts a specific `error_type` will still fail, honestly,
            # because nothing here can identify one.
            result = evaluate_error(
                _resolved(step.expect_error, scope), {'message': str(exc)}
            )
            return self._finish(step, scope, result, started, attempts=1)

        scope.record_response(step.id, [event.payload for event in events])
        captured = self._capture(step, scope, [event.payload for event in events])
        if captured is not None:
            return self._step(step, Outcome.FAIL, started, message=captured)

        result = AssertionResult()
        if step.expect_stream is not None:
            result += evaluate_stream(step.expect_stream, events, timed_out=timed_out)
        elif step.expect_error is not None:
            # The stream opened, so the error the step required did not happen.
            result += _failed(
                f'expected an error, but the stream opened and produced '
                f'{len(events)} event(s)',
                'an error', f'{len(events)} event(s)',
            )
        result += self._evaluate_stream_status(step, scope, status, len(events))
        return self._finish(step, scope, result, started, attempts=1)

    def _stream_source(self, step: Step, scope: Scope) -> AsyncIterator[StreamEvent]:
        if step.kind() is StepKind.RAW:
            raw: RawBlock = _resolved(step.raw, scope)
            return self.dispatcher.stream_raw(raw, self._raw_headers())
        return self.dispatcher.stream(
            step.operation, self._prepare_params(step, scope), self._headers()
        )

    async def _collect(
        self, source: AsyncIterator[StreamEvent], expect: Any | None
    ) -> tuple[list[StreamedEvent], int | None, bool]:
        """Read a stream into a list, bounded by `timeout_ms` and `max_count`.

        One event past `max_count` is enough to prove the limit was broken, and
        stopping there keeps a SUT that never closes the stream from hanging a
        run that has no `timeout_ms` to fall back on.
        """
        events: list[StreamedEvent] = []
        status: int | None = None
        # `expect` is absent when a streaming operation asserts only that the
        # call fails — `STREAM-SUB-003` subscribes to a terminal task.
        limit = None if expect is None or expect.max_count is None else expect.max_count + 1

        async def pump() -> None:
            nonlocal status
            async for event in source:
                if status is None:
                    status = event.status
                events.append(normalize(event.data, event.index))
                if limit is not None and len(events) >= limit:
                    break

        if expect is None or not expect.timeout_ms:
            await pump()
            return events, status, False

        try:
            await asyncio.wait_for(pump(), expect.timeout_ms / 1000)
        except (asyncio.TimeoutError, TimeoutError):
            return events, status, True
        return events, status, False

    def _evaluate_stream_status(
        self, step: Step, scope: Scope, status: int | None, count: int
    ) -> AssertionResult:
        """`expect.status` beside `expect_stream` — the stream's own status."""
        if step.expect is None or step.expect.status is None:
            return AssertionResult()
        if status is None:
            return _failed(
                f'expected a status, but the stream produced no observable one '
                f'({count} event(s); gRPC has no HTTP status)',
                scope.substitute(step.expect.status), None,
            )
        return evaluate_status(scope.substitute(step.expect.status), status)

    def _capture(self, step: Step, scope: Scope, response: Any) -> str | None:
        """Apply a step's `capture`; returns a failure message, or ``None``.

        A miss is a failure rather than an error: the path is well-formed and
        the SUT simply answered with a shape the test did not expect.
        """
        if not step.capture:
            return None
        try:
            scope.capture(step.id, step.capture, response)
        except UnresolvedVariable as exc:
            return str(exc)
        return None

    def _finish(
        self,
        step: Step,
        scope: Scope,
        result: AssertionResult,
        started: float,
        attempts: int,
    ) -> StepResult:
        """Turn an evaluated step into its result, running `assertions` last."""
        if not result.ok:
            return self._step(
                step, Outcome.FAIL, started, attempts=attempts,
                failure=FailureDetail.from_assertion(result.first, step.id),
                checks=result.checks,
            )

        named = self._evaluate_named(step.assertions, scope, step_id=step.id)
        if named is not None:
            return self._step(
                step, Outcome.FAIL, started, attempts=attempts,
                failure=named, checks=result.checks,
            )

        return self._step(
            step, Outcome.PASS, started, attempts=attempts, checks=result.checks
        )

    def _headers(self) -> dict[str, str]:
        """§12.4: the version header goes on every abstract operation."""
        return {VERSION_HEADER: self.spec_version}

    def _raw_headers(self) -> dict[str, str]:
        """Nothing. A raw request goes exactly as the test wrote it.

        §12.4 requires `A2A-Version` on requests the runner builds, and
        explicitly excepts tests that alter or omit it deliberately — which is
        the whole of `VER-NEG-002`, whose raw block carries no version header
        and would be silently repaired if the runner added one.
        """
        return {}

    def _prepare_params(self, step: Step, scope: Scope) -> dict[str, Any]:
        """Resolve `{{...}}`, reshape for the request message, add a messageId.

        Order matters. Substitution first, so a `{{send.taskId}}` becomes a
        value before `adapt` decides where that value belongs; the messageId
        last, so it is not folded anywhere or overwritten.
        """
        substituted = scope.substitute(dict(step.params or {}))
        params = adapt(step.operation, substituted)
        return self._ensure_message_id(params)

    def _ensure_message_id(self, params: dict[str, Any]) -> dict[str, Any]:
        """§12.4: every message needs a `messageId`; generate one if absent."""
        message = params.get('message')
        if isinstance(message, Mapping) and not message.get('messageId'):
            params['message'] = {**message, 'messageId': self._new_uuid()}
        return params

    async def _dispatch(
        self, step: Step, params: Mapping[str, Any], until: str | None = None
    ) -> tuple[WireResponse, int, bool]:
        """Dispatch, re-dispatching while a `repeat.until` is unsatisfied.

        Returns the last response, how many attempts it took, and whether the
        condition ever held. §9.1 makes an exhausted poll a *failure*, so that
        last flag is a result rather than an exception.
        """
        headers = self._headers()
        operation: Operation = step.operation
        repeat = step.repeat

        if repeat is None:
            return await self.dispatcher.dispatch(operation, params, headers), 1, True

        attempts = repeat.max_attempts or DEFAULT_MAX_ATTEMPTS
        delay_ms = DEFAULT_DELAY_MS if repeat.delay_ms is None else repeat.delay_ms
        backoff = repeat.backoff or Backoff.NONE

        response = None
        for attempt in range(1, attempts + 1):
            response = await self.dispatcher.dispatch(operation, params, headers)
            if evaluate_until(until, response.payload):
                return response, attempt, True
            if attempt < attempts:
                await self._sleep(_delay_seconds(delay_ms, attempt, backoff))
        return response, attempts, False

    # -- assertions --------------------------------------------------------

    def _evaluate_outcome(
        self, step: Step, response: WireResponse, scope: Scope
    ) -> AssertionResult:
        """Check a step's `expect` / `expect_error` against what came back.

        Assertions are substituted, not only params: `expect.body` routinely
        asserts that a field equals a value an earlier step captured
        (`id: "{{send.taskId}}"`), and comparing against the literal template
        would fail every such test.
        """
        result = AssertionResult()

        if step.expect_error is not None:
            if response.error is None:
                return _failed(
                    'expected an error, but the call succeeded',
                    'an error', f'HTTP {response.status}',
                )
            result += evaluate_error(
                _resolved(step.expect_error, scope), _observed_error(response.error)
            )

        if step.expect is not None:
            if step.expect.status is not None:
                result += evaluate_status(
                    scope.substitute(step.expect.status), response.status
                )
            if step.expect.body is not None:
                if response.error is not None and step.kind() is not StepKind.RAW:
                    # On an operation step `payload` is the unwrapped result,
                    # so asserting a body against an error would report every
                    # expected field as missing and bury the real cause.
                    #
                    # A raw step is the opposite case: `dispatch_raw` returns
                    # the *whole* body, so an error envelope is precisely what
                    # `expect.body` describes — `JSONRPC-ERR-001` asserts
                    # `body.error.code: -32601`. Refusing here would fail every
                    # such test for a reason of our own making.
                    return _failed(
                        f'expected a response body, got error '
                        f'{_name_of(response.error)}: {response.error.message}',
                        'a successful response', _name_of(response.error),
                    )
                result += evaluate_body(
                    scope.substitute(step.expect.body), response.payload
                )

        return result

    def _evaluate_named(
        self, assertions: Any, scope: Scope, *, step_id: str | None
    ) -> FailureDetail | None:
        """Run a list of `named-assertion`s, resolving each `source` first."""
        for assertion in assertions or ():
            try:
                source = scope.substitute(assertion.source)
            except UnresolvedVariable as exc:
                return FailureDetail(message=str(exc), step_id=step_id)
            resolved = _resolved(assertion, scope, keep=('source',))
            outcome = evaluate_named(resolved, source)
            if not outcome.ok:
                return FailureDetail.from_assertion(outcome.first, step_id)
        return None

    # -- result plumbing ---------------------------------------------------

    def _elapsed_ms(self, started: float) -> int:
        return int((self._clock() - started) * 1000)

    def _result(
        self,
        test: Test,
        outcome: Outcome,
        started: float,
        *,
        skip_reason: str | None = None,
        failure: FailureDetail | None = None,
        steps: Iterable[StepResult] = (),
    ) -> TestResult:
        return TestResult(
            id=test.id,
            name=test.name,
            level=test.level,
            result=outcome,
            duration_ms=self._elapsed_ms(started),
            skip_reason=skip_reason,
            failure=failure,
            steps=tuple(steps),
        )

    def _step(
        self,
        step: Step,
        outcome: Outcome,
        started: float,
        *,
        message: str | None = None,
        failure: FailureDetail | None = None,
        attempts: int = 1,
        checks: int = 0,
    ) -> StepResult:
        if failure is None and message is not None:
            failure = FailureDetail(message=message, step_id=step.id)
        return StepResult(
            id=step.id,
            result=outcome,
            duration_ms=self._elapsed_ms(started),
            failure=failure,
            attempts=attempts,
            checks=checks,
        )


class RunError(RuntimeError):
    """The run cannot proceed — missing inputs, not a SUT defect."""


def _is_streaming(step: Step) -> bool:
    """Should this step be driven as a stream?

    Decided by the *operation*, not by the presence of `expect_stream`. Two
    corpus steps subscribe or send-streaming while asserting only that the
    call fails (`STREAM-SUB-003`, `CORE-CAP-002`), and routing those to the
    unary path makes the dispatcher refuse them — an error about our own
    dispatch, reported against the SUT.
    """
    if step.expect_stream is not None:
        return True
    if step.kind() is not StepKind.OPERATION:
        return False
    return binding_for_operation(step.operation).streaming


def _resolved(model: _M, scope: Scope, *, keep: tuple[str, ...] = ()) -> _M:
    """A copy of an assertion-bearing model with its `{{...}}` resolved.

    Round-tripping through the model rather than patching fields keeps nested
    assertion trees covered — a `collection-match.match` is as likely to name
    a captured variable as an `expect.body` is.

    ``keep`` names fields to leave as written. `named-assertion.source` is one:
    it is a *reference* to a prior response, resolved separately into the value
    the assertion runs against, and substituting it in place would put a whole
    response object where the model declares a string.
    """
    data = model.model_dump(exclude_none=True)
    held = {name: data.pop(name) for name in keep if name in data}
    return type(model).model_validate({**scope.substitute(data), **held})


def _name_of(error: WireError) -> str:
    return error.error_type.value if error.error_type else 'an unnamed error'


def _failed(message: str, expected: Any, actual: Any) -> AssertionResult:
    return AssertionResult((Failure('', 'outcome', expected, actual, message),), 1)


def summarize(results: Iterable[TestResult]) -> dict[Level, dict[Outcome, int]]:
    """Tally by conformance level, for §12.7's summary.

    An implementation is conformant iff every `must` test passes, skips
    excluded — so the shape a caller needs is level first, outcome second.
    """
    tally: dict[Level, dict[Outcome, int]] = {
        level: dict.fromkeys(Outcome, 0) for level in Level
    }
    for result in results:
        tally[result.level][result.result] += 1
    return tally


def is_conformant(results: Iterable[TestResult]) -> bool:
    """§12.7: conformant iff no `must` test failed or errored."""
    return not any(
        result.level is Level.MUST
        and result.result in (Outcome.FAIL, Outcome.ERROR)
        for result in results
    )


__all__ = [
    'DEFAULT_DELAY_MS',
    'DEFAULT_MAX_ATTEMPTS',
    'VERSION_HEADER',
    'FailureDetail',
    'Outcome',
    'RunError',
    'Runner',
    'StepResult',
    'TestResult',
    'is_conformant',
    'summarize',
]
