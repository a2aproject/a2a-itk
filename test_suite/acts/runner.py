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
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

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
    WireError,
    WireResponse,
)
from test_suite.acts.dispatcher.params import adapt
from test_suite.acts.loader import LoadedSuite
from test_suite.acts.schema import (
    Backoff,
    Level,
    Operation,
    RunnerRequirement,
    Step,
    StepKind,
    Test,
    TransportBinding,
)
from test_suite.acts.variables import PathError, Scope, UnresolvedVariable


_M = TypeVar('_M', bound=BaseModel)

#: Spec §9's defaults for a `repeat` block that does not give its own.
DEFAULT_MAX_ATTEMPTS = 10
DEFAULT_DELAY_MS = 1000

#: §12.4 requires this on every request, carrying the document's
#: `spec_version`. On gRPC the dispatcher turns it into call metadata.
VERSION_HEADER = 'A2A-Version'


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
            step.kind() for step in test.steps if step.kind() is not StepKind.OPERATION
        }
        if deferred:
            kinds = ', '.join(sorted(k.value for k in deferred))
            return f'contains {kinds} step(s); not yet supported'
        if any(step.expect_stream is not None for step in test.steps):
            return 'asserts on a stream; not yet supported'

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
            params = self._prepare_params(step, scope)
        except (UnresolvedVariable, PathError) as exc:
            # The test names something nothing defines: a problem with the
            # inputs, not with the SUT.
            return self._step(step, Outcome.ERROR, started, message=str(exc))

        try:
            until = (
                None if step.repeat is None else scope.substitute(step.repeat.until)
            )
            response, attempts, converged = await self._dispatch(step, params, until)
        except DispatchError as exc:
            return self._step(step, Outcome.ERROR, started, message=str(exc))
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
        if step.capture:
            try:
                scope.capture(step.id, step.capture, response.payload)
            except UnresolvedVariable as exc:
                # The path is fine but the response did not carry it, so the
                # SUT answered with a shape the test did not expect.
                return self._step(
                    step, Outcome.FAIL, started, attempts=attempts, message=str(exc)
                )

        result = self._evaluate_outcome(step, response, scope)
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
        headers = {VERSION_HEADER: self.spec_version}
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
                if response.error is not None:
                    # Asserting a body against an error response reports every
                    # field as missing and buries the actual cause.
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
