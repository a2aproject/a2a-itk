"""Streaming assertions (spec §7).

A streaming operation returns an ordered sequence of events instead of one
reply, and `expect_stream` asserts over the sequence: how many events, what
some of them look like, what the last one looks like, and whether the task
states they carry form a legal progression.

## Two event shapes, and why both are supported

An event on the wire is a `StreamResponse`, a oneof over `task`, `message`,
`status_update` and `artifact_update`, and from A2A 1.1 also `message_update`
and `error`. Assertions in the corpus address it two different ways, and both
have to work:

    final_event: {status: {state: TASK_STATE_COMPLETED}}   # the inner event
    events: [{one_of: [{task: ...}, {status_update: ...}]}]  # the oneof arm

So each event is normalized to its **kind** plus its **payload**, and
assertions run against a view that exposes the payload's own fields *and* the
one discriminator naming its kind. An assertion mentioning `message` therefore
fails on a status update — which is the point of `STREAM-MSG-001` — while one
mentioning `status` reads straight through to the inner event.

**The name of a kind always means the kind.** Two of the 1.1 payloads have a
field called `message`: a message update carries the message that was added,
and a stream error carries its text. Read straight through, `{message: ...}`
would match both, and could no longer say "this event is a `Message`". So at
the top of an event a kind's name is reserved for the kind, and such a field
is reached through its own kind: `message_update: {message: ...}`.

The wire spells the discriminator in camelCase (ProtoJSON), the spec and the
corpus in snake_case. Normalization accepts either, and the view offers the
kind under both, so the two spellings name one kind in every assertion:
`{message_update: {absent: true}}` fails on an event that arrived as
`messageUpdate`.

An event with no recognizable discriminator is passed through whole. That is
not a fallback for malformed input — it is how a **raw** streaming step works,
where the assertion is about the transport envelope (`each_event:
{jsonrpc: "2.0"}`) and unwrapping it would remove the thing under test.

## Generation

A2A 1.1 numbers the changes to a task. `Task.generation` starts at 1, every
change raises it by one, and each change is reported by one event carrying the
new value. `monotonic_generation` and `contiguous_generation` check that
sequence; see :func:`check_generation`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from test_suite.acts.assertions import (
    AssertionResult,
    Failure,
    evaluate,
    integer_value,
)
from test_suite.acts.schema import (
    EventAssertion,
    EventMatch,
    Ordering,
    StreamAssertions,
)
from test_suite.acts.variables import MISSING


#: Wire spelling -> canonical `StreamResponse` oneof arm.
DISCRIMINATORS: Final[Mapping[str, str]] = {
    'task': 'task',
    'message': 'message',
    'status_update': 'status_update',
    'statusUpdate': 'status_update',
    'artifact_update': 'artifact_update',
    'artifactUpdate': 'artifact_update',
    # A2A 1.1.
    'message_update': 'message_update',
    'messageUpdate': 'message_update',
    'error': 'error',
}

#: The kinds an assertion may name. Values of `DISCRIMINATORS`, deduplicated.
EVENT_KINDS: Final[frozenset[str]] = frozenset(DISCRIMINATORS.values())

#: Every spelling of each kind, for offering an event under all of them.
_SPELLINGS: Final[Mapping[str, tuple[str, ...]]] = {
    kind: tuple(name for name, named in DISCRIMINATORS.items() if named == kind)
    for kind in EVENT_KINDS
}

#: The kinds that report one change to a task, and so carry the generation
#: that change produced. A `task` is a snapshot and is treated separately; a
#: `message` and an `error` carry no generation.
UPDATE_KINDS: Final[frozenset[str]] = frozenset({
    'status_update', 'artifact_update', 'message_update',
})

SUBMITTED: Final = 'TASK_STATE_SUBMITTED'

#: §7.1. A terminal state is final; nothing may follow it.
TERMINAL_STATES: Final[frozenset[str]] = frozenset({
    'TASK_STATE_COMPLETED',
    'TASK_STATE_FAILED',
    'TASK_STATE_CANCELED',
    'TASK_STATE_REJECTED',
})


@dataclass(frozen=True, slots=True)
class StreamedEvent:
    """One event, split into its oneof arm and the object inside it."""

    index: int
    #: The `StreamResponse` arm, or ``None`` when the event was not wrapped in
    #: one — a raw step's envelope, or a SUT that emits bare events.
    kind: str | None
    payload: Any
    #: The event exactly as it arrived, before unwrapping.
    raw: Any = None

    def view(self) -> Any:
        """What assertions are evaluated against.

        The payload's own fields, plus the discriminator naming this event's
        kind, in both spellings — so `{status: ...}` and
        `{status_update: {status: ...}}` both address the same event.

        A payload field that has the name of a kind is left out: `message` on
        a message update, for one. `{message: ...}` therefore addresses only
        an event that *is* a message, whatever the other kinds contain.
        """
        if self.kind is None or not isinstance(self.payload, Mapping):
            return self.payload
        view = {
            name: value
            for name, value in self.payload.items()
            if name not in DISCRIMINATORS
        }
        for spelling in _SPELLINGS[self.kind]:
            view[spelling] = self.payload
        return view

    def state(self) -> str | None:
        """The task state this event carries, if it carries one.

        Only `task` and `status_update` events do; an artifact update or a
        message contributes nothing to the state sequence.
        """
        payload = self.payload
        if not isinstance(payload, Mapping):
            return None
        status = payload.get('status')
        if not isinstance(status, Mapping):
            return None
        state = status.get('state')
        return state if isinstance(state, str) else None


def normalize(data: Any, index: int) -> StreamedEvent:
    """Split one event into its oneof arm and payload."""
    if isinstance(data, Mapping) and len(data) == 1:
        (key, value), = data.items()
        kind = DISCRIMINATORS.get(key)
        if kind is not None:
            return StreamedEvent(index=index, kind=kind, payload=value, raw=data)
    return StreamedEvent(index=index, kind=None, payload=data, raw=data)


def _fail(path: str, operator: str, expected: Any, actual: Any, message: str):
    return AssertionResult((Failure(path, operator, expected, actual, message),), 1)


def check_ordering(events: Sequence[StreamedEvent]) -> AssertionResult:
    """Verify `ordering: monotonic_state` (spec §7.1).

    §7.1 gives both a list of valid transitions and a shorter list of ones that
    **MUST** cause a failure, and the two do not agree — `SUBMITTED → COMPLETED`
    is absent from the first and not named by the second. Only the explicit
    MUST list is enforced here, because rejecting a transition the spec never
    calls illegal would fail a conforming SUT. Resolving the ambiguity is a
    question for the spec, not something to guess at in a runner.

    Enforced, then: nothing may transition *to* `SUBMITTED`, nothing may follow
    a terminal state, and a state may repeat.
    """
    result = AssertionResult()
    previous: str | None = None
    previous_index = -1

    for event in events:
        state = event.state()
        if state is None:
            continue
        if previous is not None and state != previous:
            path = f'events[{event.index}].status.state'
            if state == SUBMITTED:
                result += _fail(
                    path, 'ordering', f'not {SUBMITTED}', state,
                    f'event {event.index} moves back to {SUBMITTED} from '
                    f'{previous} at event {previous_index}; it is only an '
                    f'initial state',
                )
            elif previous in TERMINAL_STATES:
                result += _fail(
                    path, 'ordering', 'no event after a terminal state', state,
                    f'event {event.index} is {state} after terminal '
                    f'{previous} at event {previous_index}',
                )
        if state != previous:
            result += AssertionResult(checks=1)
        previous, previous_index = state, event.index

    return result


def check_generation(
    events: Sequence[StreamedEvent], *, contiguous: bool = False
) -> AssertionResult:
    """Verify `ordering: monotonic_generation`, or `contiguous_generation`.

    The stream fails when:

    - a status, artifact or message update carries no generation, or one
      lower than 1;
    - the generation of such an update is not greater than every generation
      seen earlier on the stream;
    - a task snapshot carries a generation lower than 1, or lower than one
      seen earlier. A snapshot may repeat the latest value: it reports the
      state of the task and is not a change to it.

    With ``contiguous`` each update must also be exactly one above the
    latest generation before it, and a snapshot after the first event may
    not be above it. That holds only on a stream that is sent every event:
    a subscriber that asked for A2A 1.0 is not sent message updates, and sees
    a gap wherever a client message was added.

    A `message` and an `error` carry no generation and are skipped. An event
    of a kind this runner does not know is skipped too, but it may have
    advanced the generation, so the event after it is not checked for a gap.
    """
    result = AssertionResult()
    #: The greatest generation seen so far, and where.
    highest: int | None = None
    highest_index = -1
    #: The event before this one had a generation that could not be read, or
    #: was of a kind this runner does not know. The event after it is not
    #: checked for a gap: a missing value is reported once, and an unknown
    #: kind may have advanced the generation.
    after_unreadable = False

    for event in events:
        if event.kind is None:
            after_unreadable = True
            continue
        if event.kind != 'task' and event.kind not in UPDATE_KINDS:
            continue
        snapshot = event.kind == 'task'
        label = f'event {event.index} ({event.kind})'
        path = f'events[{event.index}].generation'
        written = (
            event.payload.get('generation', MISSING)
            if isinstance(event.payload, Mapping)
            else MISSING
        )
        generation = integer_value(written)

        if generation is None or generation < 1:
            if written is MISSING:
                said = 'carries no generation'
            elif generation is None:
                said = f'has generation {written!r}, which is not an integer'
            else:
                said = f'has generation {generation}'
            result += _fail(
                path, 'ordering', 'a generation of at least 1', written,
                f'{label} {said}; a task and every update to it carry a '
                f'generation of at least 1',
            )
            after_unreadable = True
            continue

        if highest is None:
            # The first generation on the stream has nothing to compare with.
            result += AssertionResult(checks=1)
        elif snapshot and generation < highest:
            result += _fail(
                path, 'ordering', f'at least {highest}', generation,
                f'{label} is at generation {generation}, lower than the '
                f'{highest} already seen at event {highest_index}',
            )
        elif not snapshot and generation <= highest:
            result += _fail(
                path, 'ordering', f'greater than {highest}', generation,
                f'{label} has generation {generation}, which is not greater '
                f'than the {highest} already seen at event {highest_index}',
            )
        elif contiguous and snapshot and not after_unreadable and generation > highest:
            result += _fail(
                path, 'ordering', highest, generation,
                f'{label} is at generation {generation}, but the latest before '
                f'it was {highest}, at event {highest_index}; on a stream that '
                f'misses no change a snapshot repeats the latest generation',
            )
        elif (
            contiguous
            and not snapshot
            and not after_unreadable
            and generation != highest + 1
        ):
            result += _fail(
                path, 'ordering', highest + 1, generation,
                f'{label} has generation {generation}, but the latest before '
                f'it was {highest}, at event {highest_index}; a stream that '
                f'misses no change reads {highest + 1} here',
            )
        else:
            result += AssertionResult(checks=1)

        if highest is None or generation > highest:
            highest, highest_index = generation, event.index
        after_unreadable = False

    return result


def event_matches(assertion: Mapping[str, Any], event: StreamedEvent) -> bool:
    """Does one event satisfy an assertion written for an event?

    The assertion addresses the event as `events` entries do: by the name of
    its kind in either spelling, or straight through to its fields.
    """
    return evaluate(assertion, event.view()).ok


def _event_assertion_body(assertion: EventAssertion) -> dict[str, Any]:
    """The assertion tree inside an `event-assertion`, minus its own controls.

    `description`, `match` and `index` say *which* event to check; everything
    else — declared as extras on the model — says what it must look like.
    """
    body = assertion.model_dump(exclude_none=True)
    for control in ('description', 'match', 'index'):
        body.pop(control, None)
    return body


def _addressed(assertion: EventAssertion, position: int) -> int | None:
    """The index of the one event an assertion applies to.

    ``None`` for `match: any_position`, which applies to whichever event
    matches. Otherwise `index` where it is given, and by default the
    assertion's own place in the list.
    """
    if assertion.index is not None:
        return assertion.index
    if assertion.match is EventMatch.ANY_POSITION:
        return None
    return position


def _evaluate_event_assertion(
    assertion: EventAssertion,
    position: int,
    events: Sequence[StreamedEvent],
    *,
    path: str,
    had: str,
) -> AssertionResult:
    body = _event_assertion_body(assertion)
    if not body:
        # A `description` and nothing else asserts nothing about any event.
        return AssertionResult()

    label = assertion.description or f'events[{position}]'
    at = _addressed(assertion, position)

    if at is None:
        if any(event_matches(body, event) for event in events):
            return AssertionResult(checks=1)
        return _fail(
            f'{path}[*]', 'any_position', body, None,
            f'{label}: no event among the {len(events)} received matched',
        )

    if at >= len(events):
        operator = 'index' if assertion.index is not None else 'exact_position'
        return _fail(
            f'{path}[{at}]', operator, body, None,
            f'{label}: no event at index {at}; {had} {len(events)}',
        )
    return evaluate(body, events[at].view(), path=f'{path}[{at}]')


def evaluate_events(
    assertions: Sequence[EventAssertion],
    events: Sequence[StreamedEvent],
    *,
    path: str = 'events',
    had: str = 'the stream had',
) -> AssertionResult:
    """Evaluate an `events` list against the events that arrived.

    ``path`` is where the list sits in the test, for a failure to name, and
    ``had`` words the report of an event that is not there.
    """
    result = AssertionResult()
    for position, assertion in enumerate(assertions):
        result += _evaluate_event_assertion(
            assertion, position, events, path=path, had=had
        )
    return result


def awaits_events(
    assertions: Sequence[EventAssertion], events: Sequence[StreamedEvent]
) -> bool:
    """Could an event that has not arrived yet change what ``assertions`` come to?

    True while an assertion addresses an event that is not there: an index
    past the last event, or `any_position` with no match so far. An assertion
    whose event has arrived is decided, because events are not changed once
    they have arrived. One that is decided and does not hold has failed the
    list, and nothing is waited for after that.
    """
    waiting = False
    for position, assertion in enumerate(assertions):
        body = _event_assertion_body(assertion)
        if not body:
            continue
        at = _addressed(assertion, position)
        if at is None:
            waiting = waiting or not any(event_matches(body, event) for event in events)
        elif at >= len(events):
            waiting = True
        elif not event_matches(body, events[at]):
            return False
    return waiting


def evaluate_stream(
    expect: StreamAssertions,
    events: Sequence[StreamedEvent],
    *,
    timed_out: bool = False,
) -> AssertionResult:
    """Evaluate one stream's assertions against the events that arrived.

    Takes the assertion vocabulary rather than `ExpectStream` itself, so the
    shared block of a step and the per-stream block of a `streams` entry are
    evaluated by the same code against the same stream.

    ``timed_out`` reports that collection was cut short by `timeout_ms`. That
    is a failure in itself — §7 calls the field the maximum time to wait for
    stream *completion*, so a stream still open when it expires did not
    complete — and it is reported before the count assertions, which would
    otherwise blame the SUT for sending too few events when the truth is that
    the runner stopped listening.
    """
    result = AssertionResult()

    if timed_out:
        result += _fail(
            'stream', 'timeout_ms', f'completion within {expect.timeout_ms}ms',
            f'{len(events)} event(s), stream still open',
            f'stream did not complete within {expect.timeout_ms}ms; '
            f'{len(events)} event(s) received',
        )

    if expect.min_count is not None:
        if len(events) < expect.min_count:
            result += _fail(
                'stream', 'min_count', expect.min_count, len(events),
                f'expected at least {expect.min_count} event(s), got {len(events)}',
            )
        else:
            result += AssertionResult(checks=1)

    if expect.max_count is not None:
        if len(events) > expect.max_count:
            result += _fail(
                'stream', 'max_count', expect.max_count, len(events),
                f'expected at most {expect.max_count} event(s), got {len(events)}',
            )
        else:
            result += AssertionResult(checks=1)

    rules = expect.orderings()
    if Ordering.MONOTONIC_STATE in rules:
        result += check_ordering(events)
    if Ordering.CONTIGUOUS_GENERATION in rules:
        # The stricter rule makes every check of the other one.
        result += check_generation(events, contiguous=True)
    elif Ordering.MONOTONIC_GENERATION in rules:
        result += check_generation(events)

    result += evaluate_events(expect.events or (), events)

    if expect.final_event is not None:
        if not events:
            result += _fail(
                'final_event', 'final_event', expect.final_event, None,
                'expected a final event, but the stream produced none',
            )
        else:
            result += evaluate(
                expect.final_event, events[-1].view(), path='final_event'
            )

    if expect.each_event is not None:
        if not events:
            # Same rule as a collection quantifier: an assertion that inspected
            # nothing has not passed.
            result += _fail(
                'each_event', 'each_event', expect.each_event, None,
                'expected every event to match, but the stream produced none',
            )
        for event in events:
            result += evaluate(
                expect.each_event, event.view(), path=f'each_event[{event.index}]'
            )

    return result


__all__ = [
    'DISCRIMINATORS',
    'EVENT_KINDS',
    'SUBMITTED',
    'TERMINAL_STATES',
    'StreamedEvent',
    'UPDATE_KINDS',
    'awaits_events',
    'check_generation',
    'check_ordering',
    'evaluate_events',
    'evaluate_stream',
    'event_matches',
    'normalize',
]
