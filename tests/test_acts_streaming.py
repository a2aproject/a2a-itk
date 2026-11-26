"""Streaming assertions (spec §7).

Two things carry most of the weight. **Event normalization**, because the
corpus addresses an event both through its `StreamResponse` arm and straight
through to the object inside it, and both have to work against the camelCase
the wire actually sends. And **`monotonic_state`**, because §7.1 gives two
lists of transitions that disagree with each other, so what is enforced is a
decision rather than a transcription.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_suite.acts import load_suite
from test_suite.acts.assertions import evaluate
from test_suite.acts.schema import EventAssertion, ExpectStream
from test_suite.acts.streaming import (
    DISCRIMINATORS,
    EVENT_KINDS,
    TERMINAL_STATES,
    StreamedEvent,
    check_generation,
    check_ordering,
    evaluate_stream,
    normalize,
)


CORPUS = (
    Path(__file__).resolve().parent.parent / 'scenarios' / 'acts' / 'suite.acts.yaml'
)


@pytest.fixture(scope='module')
def suite():
    return load_suite(CORPUS)


def status(state: str, index: int = 0, **extra) -> StreamedEvent:
    return normalize(
        {'statusUpdate': {'taskId': 'T1', 'status': {'state': state}, **extra}}, index
    )


def artifact(index: int = 0, **parts) -> StreamedEvent:
    return normalize(
        {'artifactUpdate': {'taskId': 'T1', 'artifact': {'parts': [parts]}}}, index
    )


def message_update(index: int = 0, **extra) -> StreamedEvent:
    """A client message added to a task, as A2A 1.1 reports it."""
    payload = {
        'taskId': 'T1', 'generation': '4',
        'message': {'messageId': 'm1', 'role': 'ROLE_USER'},
        **extra,
    }
    return normalize({'messageUpdate': payload}, index)


def stream_error(index: int = 0) -> StreamedEvent:
    """One refused request on a live exchange, as A2A 1.1 reports it."""
    return normalize(
        {'error': {
            'taskId': 'T1', 'code': 'INVALID_ARGUMENT',
            'message': 'the message was rejected', 'referenceId': 'm2',
        }},
        index,
    )


def stream(**kwargs) -> ExpectStream:
    return ExpectStream(**kwargs)


class TestNormalization:
    def test_camel_case_wire_spelling(self):
        event = normalize({'statusUpdate': {'status': {'state': 'X'}}}, 0)
        assert event.kind == 'status_update'
        assert event.payload == {'status': {'state': 'X'}}

    def test_snake_case_spelling(self):
        assert normalize({'status_update': {'a': 1}}, 0).kind == 'status_update'

    @pytest.mark.parametrize(
        ('key', 'kind'),
        [
            ('task', 'task'),
            ('message', 'message'),
            ('statusUpdate', 'status_update'),
            ('artifactUpdate', 'artifact_update'),
            ('messageUpdate', 'message_update'),
            ('message_update', 'message_update'),
            ('error', 'error'),
        ],
    )
    def test_every_oneof_arm(self, key, kind):
        assert normalize({key: {'a': 1}}, 0).kind == kind

    def test_an_unwrapped_event_passes_through(self):
        """How a raw streaming step sees its transport envelope."""
        envelope = {'jsonrpc': '2.0', 'id': 1, 'result': {'x': 1}}
        event = normalize(envelope, 0)
        assert event.kind is None
        assert event.payload is envelope

    def test_a_two_key_map_is_not_a_discriminated_event(self):
        """A oneof carries exactly one arm; anything else is a bare event."""
        assert normalize({'task': {}, 'extra': 1}, 0).kind is None

    def test_the_kind_table_covers_the_proto(self):
        """Read off the specification's own message, so a kind added to the
        proto and not here fails this test and does not pass through as an
        event of no kind."""
        from pyproto import a2a_pb2

        arms = a2a_pb2.StreamResponse.DESCRIPTOR.fields
        assert EVENT_KINDS == {arm.name for arm in arms}
        assert EVENT_KINDS == {
            'task', 'message', 'status_update', 'artifact_update',
            'message_update', 'error',
        }
        for arm in arms:
            # Both the proto name and the name ProtoJSON puts on the wire.
            assert DISCRIMINATORS[arm.name] == arm.name
            assert DISCRIMINATORS[arm.json_name] == arm.name

    def test_an_event_of_an_unknown_kind_is_kept_as_it_arrived(self):
        """A later minor version may add kinds. The runner reports what the
        SUT sent, so such an event stays whole and still counts."""
        future = {'futureEvent': {'taskId': 'T1', 'detail': 'x'}}
        event = normalize(future, 1)
        assert event.kind is None
        assert event.view() == future
        events = [status('TASK_STATE_WORKING', 0), event, status('TASK_STATE_COMPLETED', 2)]
        assert evaluate_stream(stream(min_count=3, max_count=3), events).ok
        assert evaluate_stream(
            stream(events=[{'index': 1, 'futureEvent': {'detail': 'x'}}]), events
        ).ok


class TestEventView:
    def test_the_payload_reads_straight_through(self):
        """`final_event: {status: ...}` — the corpus's flat style."""
        view = status('TASK_STATE_WORKING').view()
        assert view['status'] == {'state': 'TASK_STATE_WORKING'}

    def test_the_discriminator_addresses_the_same_event(self):
        """`{status_update: {status: ...}}` — the spec's style."""
        view = status('TASK_STATE_WORKING').view()
        assert view['status_update']['status'] == {'state': 'TASK_STATE_WORKING'}

    def test_the_kind_is_offered_in_both_spellings(self):
        view = status('TASK_STATE_WORKING').view()
        assert view['statusUpdate'] is view['status_update']

    def test_only_the_matching_discriminator_is_present(self):
        """Which is what makes `final_event: {message: {exists: true}}` mean something."""
        view = status('TASK_STATE_WORKING').view()
        assert 'message' not in view
        assert 'task' not in view

    def test_an_unwrapped_event_exposes_itself(self):
        assert normalize({'jsonrpc': '2.0'}, 0).view() == {'jsonrpc': '2.0'}

    def test_state_extraction(self):
        assert status('TASK_STATE_WORKING').state() == 'TASK_STATE_WORKING'
        assert normalize({'task': {'status': {'state': 'S'}}}, 0).state() == 'S'

    def test_events_without_a_state_contribute_none(self):
        assert artifact().state() is None
        assert normalize({'message': {'role': 'ROLE_AGENT'}}, 0).state() is None
        assert message_update().state() is None
        assert stream_error().state() is None


class TestEventKindNames:
    """How an assertion names the kind of an event.

    These decide what "this event is a message update" and "no message update
    was sent" mean, so a mistake here makes a test pass for the wrong reason.
    """

    @pytest.mark.parametrize('spelling', ['message_update', 'messageUpdate'])
    @pytest.mark.parametrize('wire', ['message_update', 'messageUpdate'])
    def test_either_spelling_names_the_kind_however_it_arrived(self, spelling, wire):
        event = normalize({wire: {'taskId': 'T1', 'generation': '4'}}, 0)
        assert evaluate({spelling: {'taskId': 'T1'}}, event.view()).ok
        assert not evaluate({spelling: {'taskId': 'OTHER'}}, event.view()).ok

    @pytest.mark.parametrize('spelling', ['message_update', 'messageUpdate'])
    def test_absent_fails_on_an_event_of_that_kind(self, spelling):
        """`each_event: {message_update: {absent: true}}` is how a test says a
        client of A2A 1.0 is never sent one. It must not pass because the wire
        spelled the kind differently from the test."""
        assertion = {spelling: {'absent': True}}
        assert not evaluate(assertion, message_update().view()).ok
        assert evaluate(assertion, status('TASK_STATE_WORKING').view()).ok

    def test_the_name_of_a_kind_means_the_kind_and_not_a_field(self):
        """A message update and a stream error both have a field `message`.
        Read straight through, `{message: ...}` would match them, and could no
        longer say that an event is a `Message`."""
        is_a_message = {'message': {'exists': True}}
        assert evaluate(is_a_message, normalize({'message': {'role': 'ROLE_AGENT'}}, 0).view()).ok
        assert not evaluate(is_a_message, message_update().view()).ok
        assert not evaluate(is_a_message, stream_error().view()).ok
        assert not evaluate(is_a_message, status('TASK_STATE_WORKING').view()).ok

    def test_such_a_field_is_reached_through_its_own_kind(self):
        added = {'message_update': {'message': {'role': 'ROLE_USER', 'messageId': 'm1'}}}
        assert evaluate(added, message_update().view()).ok
        rejected = {'error': {'message': {'contains': 'rejected'}, 'referenceId': 'm2'}}
        assert evaluate(rejected, stream_error().view()).ok
        assert not evaluate(
            {'error': {'referenceId': 'another'}}, stream_error().view()
        ).ok

    def test_the_other_fields_still_read_straight_through(self):
        view = message_update().view()
        assert evaluate({'taskId': 'T1', 'generation': {'gt': 3}}, view).ok
        assert evaluate({'code': 'INVALID_ARGUMENT'}, stream_error().view()).ok

    def test_an_error_event_is_not_a_status(self):
        """It reports that one request was refused, not a state of the task."""
        assert not evaluate({'status': {'exists': True}}, stream_error().view()).ok


class TestCounts:
    def test_min_count(self):
        assert evaluate_stream(stream(min_count=2), [status('A'), status('B', 1)]).ok
        result = evaluate_stream(stream(min_count=2), [status('A')])
        assert not result.ok
        assert 'at least 2' in result.first.message

    def test_max_count(self):
        assert evaluate_stream(stream(max_count=2), [status('A')]).ok
        assert not evaluate_stream(
            stream(max_count=1), [status('A'), status('B', 1)]
        ).ok

    def test_zero_events_against_min_count(self):
        assert not evaluate_stream(stream(min_count=1), []).ok

    def test_an_empty_block_asserts_nothing(self):
        assert evaluate_stream(stream(), []).checks == 0


class TestOrdering:
    """§7.1 — the two transitions the spec says MUST fail."""

    def test_a_legal_progression(self):
        events = [
            status('TASK_STATE_SUBMITTED', 0),
            status('TASK_STATE_WORKING', 1),
            status('TASK_STATE_COMPLETED', 2),
        ]
        assert check_ordering(events).ok

    def test_a_state_may_repeat(self):
        events = [status('TASK_STATE_WORKING', i) for i in range(3)]
        assert check_ordering(events).ok

    def test_going_back_to_submitted_fails(self):
        events = [status('TASK_STATE_WORKING', 0), status('TASK_STATE_SUBMITTED', 1)]
        result = check_ordering(events)
        assert not result.ok
        assert 'initial state' in result.first.message

    def test_anything_after_a_terminal_state_fails(self):
        events = [status('TASK_STATE_COMPLETED', 0), status('TASK_STATE_WORKING', 1)]
        result = check_ordering(events)
        assert not result.ok
        assert 'terminal' in result.first.message

    @pytest.mark.parametrize('terminal', sorted(TERMINAL_STATES))
    def test_every_terminal_state_is_final(self, terminal):
        events = [status(terminal, 0), status('TASK_STATE_WORKING', 1)]
        assert not check_ordering(events).ok

    def test_input_required_back_to_working_is_legal(self):
        events = [
            status('TASK_STATE_WORKING', 0),
            status('TASK_STATE_INPUT_REQUIRED', 1),
            status('TASK_STATE_WORKING', 2),
        ]
        assert check_ordering(events).ok

    def test_a_transition_the_spec_never_calls_illegal_is_allowed(self):
        """`SUBMITTED → COMPLETED` is absent from §7.1's *valid* list but is
        not in its MUST-fail list either. Rejecting it would fail a SUT the
        spec never says is wrong."""
        events = [
            status('TASK_STATE_SUBMITTED', 0),
            status('TASK_STATE_COMPLETED', 1),
        ]
        assert check_ordering(events).ok

    def test_stateless_events_do_not_break_the_chain(self):
        events = [
            status('TASK_STATE_WORKING', 0),
            artifact(1),
            status('TASK_STATE_COMPLETED', 2),
        ]
        assert check_ordering(events).ok

    def test_ordering_runs_through_evaluate_stream(self):
        from test_suite.acts.schema import Ordering

        events = [status('TASK_STATE_COMPLETED', 0), status('TASK_STATE_WORKING', 1)]
        assert not evaluate_stream(
            stream(ordering=Ordering.MONOTONIC_STATE), events
        ).ok


def gen_status(generation, index, state='TASK_STATE_WORKING') -> StreamedEvent:
    extra = {} if generation is None else {'generation': generation}
    return status(state, index, **extra)


def snapshot(generation, index) -> StreamedEvent:
    task = {'id': 'T1', 'status': {'state': 'TASK_STATE_WORKING'}}
    if generation is not None:
        task['generation'] = generation
    return normalize({'task': task}, index)


class TestGenerationOrdering:
    """`monotonic_generation` and `contiguous_generation`.

    A2A 1.1 numbers the changes to a task: every change raises `generation`
    by one and is reported by one event carrying the new value.
    """

    def test_a_stream_that_reports_every_change(self):
        """As a JSON binding sends it: 64-bit integers written as strings."""
        events = [
            snapshot('2', 0),
            gen_status('3', 1),
            normalize({'artifactUpdate': {'taskId': 'T1', 'generation': '4'}}, 2),
            message_update(3, generation='5'),
            gen_status('6', 4, 'TASK_STATE_COMPLETED'),
        ]
        for contiguous in (False, True):
            result = check_generation(events, contiguous=contiguous)
            assert result.ok
            assert result.checks == 5

    def test_generations_as_numbers(self):
        """A generation written as a JSON number is read as well."""
        events = [snapshot(2, 0), gen_status(3, 1), gen_status(4, 2)]
        assert check_generation(events, contiguous=True).ok

    def test_a_gap_is_allowed_unless_the_stream_must_be_contiguous(self):
        """A subscriber that is not sent every event sees gaps."""
        events = [snapshot('2', 0), gen_status('3', 1), gen_status('5', 2)]
        assert check_generation(events).ok
        result = check_generation(events, contiguous=True)
        assert len(result.failures) == 1
        failure = result.first
        assert failure.path == 'events[2].generation'
        assert (failure.expected, failure.actual) == (4, 5)
        assert 'the latest before it was 3, at event 1' in failure.message

    def test_the_first_update_after_a_snapshot_is_checked_too(self):
        assert check_generation([snapshot('2', 0), gen_status('3', 1)], contiguous=True).ok
        assert not check_generation(
            [snapshot('2', 0), gen_status('4', 1)], contiguous=True
        ).ok

    @pytest.mark.parametrize('contiguous', [False, True])
    def test_an_update_that_repeats_a_generation_fails(self, contiguous):
        """Two events must never report the same generation."""
        events = [gen_status('3', 0), gen_status('3', 1)]
        result = check_generation(events, contiguous=contiguous)
        assert len(result.failures) == 1
        assert 'not greater than the 3 already seen at event 0' in result.first.message

    @pytest.mark.parametrize('contiguous', [False, True])
    def test_an_update_that_goes_back_fails(self, contiguous):
        events = [gen_status('5', 0), gen_status('4', 1)]
        assert not check_generation(events, contiguous=contiguous).ok

    @pytest.mark.parametrize(
        ('written', 'said'),
        [
            (None, 'carries no generation'),
            ('0', 'has generation 0'),
            (0, 'has generation 0'),
            ('-1', 'has generation -1'),
            ('abc', "has generation 'abc', which is not an integer"),
            (1.5, 'has generation 1.5, which is not an integer'),
            (True, 'has generation True, which is not an integer'),
        ],
    )
    def test_an_update_needs_a_generation_of_at_least_one(self, written, said):
        """An absent field and a zero are how an agent that does not
        implement generation looks on the wire."""
        result = check_generation([gen_status(written, 0)])
        assert len(result.failures) == 1
        assert said in result.first.message
        assert result.first.path == 'events[0].generation'

    def test_a_snapshot_may_repeat_the_latest_generation(self):
        """It reports the state of the task and is not a change to it."""
        events = [gen_status('3', 0), snapshot('3', 1), gen_status('4', 2)]
        assert check_generation(events, contiguous=True).ok

    def test_a_snapshot_older_than_what_was_already_sent_fails(self):
        result = check_generation([gen_status('3', 0), snapshot('2', 1)])
        assert len(result.failures) == 1
        assert 'lower than the 3 already seen at event 0' in result.first.message

    def test_an_old_snapshot_is_reported_once(self):
        """The update after it is one above the latest generation, which the
        old snapshot did not change."""
        events = [gen_status('3', 0), snapshot('2', 1), gen_status('4', 2)]
        result = check_generation(events, contiguous=True)
        assert [f.path for f in result.failures] == ['events[1].generation']

    @pytest.mark.parametrize('payload', [None, 'text', ['a', 'list']])
    def test_an_update_that_is_not_an_object_carries_no_generation(self, payload):
        """What a broken agent may send. It fails the stream; it must not
        break the run."""
        result = check_generation([normalize({'statusUpdate': payload}, 0)])
        assert 'carries no generation' in result.first.message

    @pytest.mark.parametrize('written', [None, '0'])
    def test_a_snapshot_needs_a_generation_too(self, written):
        assert not check_generation([snapshot(written, 0)]).ok

    def test_events_without_a_generation_field_are_skipped(self):
        """A `Message` and a stream error have none, and an event of a kind
        the runner does not know is not the runner's to judge."""
        events = [
            gen_status('3', 0),
            stream_error(1),
            normalize({'futureEvent': {'generation': 'whatever'}}, 2),
            gen_status('4', 3),
        ]
        result = check_generation(events, contiguous=True)
        assert result.ok
        assert result.checks == 2
        only_a_message = [normalize({'message': {'role': 'ROLE_AGENT'}}, 0)]
        assert check_generation(only_a_message, contiguous=True).ok

    def test_an_event_of_an_unknown_kind_may_advance_the_generation(self):
        """A server may send a kind added after this runner was written, and
        that event may carry the next generation. The update after it is
        checked for order, not for the step of one."""
        unknown = normalize({'futureUpdate': {'taskId': 'T1', 'generation': '3'}}, 1)
        assert check_generation([snapshot('2', 0), unknown, gen_status('4', 2)], contiguous=True).ok
        result = check_generation([snapshot('2', 0), unknown, gen_status('2', 2)], contiguous=True)
        assert 'not greater than the 2 already seen at event 0' in result.first.message

    def test_a_gap_after_an_unknown_event_is_reported_at_the_next_update(self):
        unknown = normalize({'futureUpdate': {'taskId': 'T1', 'generation': '3'}}, 1)
        events = [snapshot('2', 0), unknown, gen_status('4', 2), gen_status('6', 3)]
        result = check_generation(events, contiguous=True)
        assert [f.path for f in result.failures] == ['events[3].generation']

    def test_a_snapshot_that_jumps_ahead_fails_only_on_a_contiguous_stream(self):
        """Every change is its own event, so on a stream that is sent every
        event a snapshot cannot be ahead of the updates before it. Ahead, it
        would hide the updates that were left out."""
        events = [
            snapshot('2', 0),
            normalize({'artifactUpdate': {'taskId': 'T1', 'generation': '3'}}, 1),
            snapshot('5', 2),
        ]
        assert check_generation(events).ok
        result = check_generation(events, contiguous=True)
        assert len(result.failures) == 1
        failure = result.first
        assert failure.path == 'events[2].generation'
        assert (failure.expected, failure.actual) == (3, 5)
        assert 'a snapshot repeats the latest generation' in failure.message

    def test_a_snapshot_after_a_missing_generation_is_not_held_to_it(self):
        """The missing value is reported once, at the event that lacks it."""
        events = [gen_status('3', 0), gen_status(None, 1), snapshot('5', 2)]
        result = check_generation(events, contiguous=True)
        assert [f.path for f in result.failures] == ['events[1].generation']

    def test_a_stream_may_open_with_an_update(self):
        """A subscriber that resumes is not sent the task first."""
        events = [gen_status('7', 0), gen_status('8', 1)]
        assert check_generation(events, contiguous=True).ok

    def test_one_missing_generation_is_reported_once(self):
        """And not a second time as a gap at the event after it."""
        events = [gen_status('3', 0), gen_status(None, 1), gen_status('5', 2)]
        result = check_generation(events, contiguous=True)
        assert [f.path for f in result.failures] == ['events[1].generation']

    def test_a_gap_after_that_is_reported_again(self):
        events = [
            gen_status('3', 0), gen_status(None, 1), gen_status('5', 2), gen_status('7', 3),
        ]
        result = check_generation(events, contiguous=True)
        assert [f.path for f in result.failures] == [
            'events[1].generation', 'events[3].generation',
        ]

    def test_every_digit_of_a_64_bit_generation_counts(self):
        """These two are the same number once read as floats."""
        below, above = '9007199254740992', '9007199254740993'
        assert float(below) == float(above)
        assert check_generation([gen_status(below, 0), gen_status(above, 1)], contiguous=True).ok
        assert not check_generation([gen_status(above, 0), gen_status(below, 1)]).ok


class TestOrderingRules:
    """`ordering` as one rule or a list, through `evaluate_stream`."""

    GOOD = [
        snapshot('2', 0),
        gen_status('3', 1),
        gen_status('4', 2, 'TASK_STATE_COMPLETED'),
    ]
    #: Generations in order, but the task returns from a terminal state.
    REGRESSES = [
        gen_status('3', 0, 'TASK_STATE_COMPLETED'),
        gen_status('4', 1, 'TASK_STATE_WORKING'),
    ]
    #: States in order, but a generation is skipped.
    SKIPS = [gen_status('3', 0), gen_status('5', 1, 'TASK_STATE_COMPLETED')]
    #: States in order, but two events report the same generation.
    REPEATS = [gen_status('3', 0), gen_status('3', 1, 'TASK_STATE_COMPLETED')]

    @pytest.mark.parametrize(
        'ordering',
        [
            'monotonic_state',
            'monotonic_generation',
            'contiguous_generation',
            ['monotonic_state', 'monotonic_generation'],
            ['monotonic_state', 'contiguous_generation'],
        ],
    )
    def test_a_well_ordered_stream_satisfies_every_rule(self, ordering):
        assert evaluate_stream(stream(ordering=ordering), self.GOOD).ok

    @pytest.mark.parametrize(
        ('ordering', 'events', 'ok'),
        [
            ('monotonic_state', 'REGRESSES', False),
            ('monotonic_generation', 'REGRESSES', True),
            (['monotonic_state', 'monotonic_generation'], 'REGRESSES', False),
            ('monotonic_state', 'SKIPS', True),
            ('monotonic_generation', 'SKIPS', True),
            ('contiguous_generation', 'SKIPS', False),
            (['monotonic_state', 'contiguous_generation'], 'SKIPS', False),
            ('monotonic_state', 'REPEATS', True),
            ('monotonic_generation', 'REPEATS', False),
            (['monotonic_state', 'monotonic_generation'], 'REPEATS', False),
            ('contiguous_generation', 'REPEATS', False),
        ],
    )
    def test_each_rule_checks_its_own_thing(self, ordering, events, ok):
        result = evaluate_stream(stream(ordering=ordering), getattr(self, events))
        assert result.ok is ok

    def test_no_rule_no_check(self):
        assert evaluate_stream(stream(), self.REGRESSES + self.SKIPS).ok

    def test_naming_both_generation_rules_reports_a_problem_once(self):
        both = stream(ordering=['monotonic_generation', 'contiguous_generation'])
        repeated = [gen_status('3', 0), gen_status('3', 1)]
        assert len(evaluate_stream(both, repeated).failures) == 1
        assert not evaluate_stream(both, self.SKIPS).ok

    def test_a_rule_on_one_stream_of_a_set(self):
        """`streams` entries take the same rules for their own stream."""
        from test_suite.acts.schema import StreamPlan

        plan = StreamPlan(ordering='contiguous_generation')
        assert evaluate_stream(plan, self.GOOD).ok
        assert not evaluate_stream(plan, self.SKIPS).ok


class TestFinalEvent:
    def test_matches_the_last_event(self):
        events = [status('TASK_STATE_WORKING', 0), status('TASK_STATE_COMPLETED', 1)]
        expect = stream(final_event={'status': {'state': 'TASK_STATE_COMPLETED'}})
        assert evaluate_stream(expect, events).ok

    def test_not_an_earlier_one(self):
        events = [status('TASK_STATE_COMPLETED', 0), status('TASK_STATE_WORKING', 1)]
        expect = stream(final_event={'status': {'state': 'TASK_STATE_COMPLETED'}})
        assert not evaluate_stream(expect, events).ok

    def test_a_discriminated_final_event(self):
        """`STREAM-MSG-001`'s shape."""
        events = [normalize({'message': {'role': 'ROLE_AGENT'}}, 0)]
        assert evaluate_stream(stream(final_event={'message': {'exists': True}}), events).ok

    def test_the_discriminator_discriminates(self):
        events = [status('TASK_STATE_COMPLETED', 0)]
        assert not evaluate_stream(
            stream(final_event={'message': {'exists': True}}), events
        ).ok

    def test_no_events_at_all(self):
        result = evaluate_stream(stream(final_event={'status': {'exists': True}}), [])
        assert not result.ok
        assert 'produced none' in result.first.message


class TestEachEvent:
    def test_applies_to_every_event(self):
        events = [normalize({'jsonrpc': '2.0'}, i) for i in range(3)]
        assert evaluate_stream(stream(each_event={'jsonrpc': '2.0'}), events).ok

    def test_one_bad_event_fails(self):
        events = [normalize({'jsonrpc': '2.0'}, 0), normalize({'jsonrpc': '1.0'}, 1)]
        result = evaluate_stream(stream(each_event={'jsonrpc': '2.0'}), events)
        assert not result.ok
        assert 'each_event[1]' in result.first.path

    def test_no_events_fails_rather_than_passing_vacuously(self):
        assert not evaluate_stream(stream(each_event={'a': 1}), []).ok


class TestEventAssertions:
    EVENTS = [
        status('TASK_STATE_WORKING', 0),
        artifact(1, text='hello'),
        status('TASK_STATE_COMPLETED', 2),
    ]

    def test_any_position(self):
        expect = stream(events=[
            EventAssertion(match='any_position', **{'artifact': {'exists': True}})
        ])
        assert evaluate_stream(expect, self.EVENTS).ok

    def test_any_position_that_matches_nothing(self):
        expect = stream(events=[
            EventAssertion(match='any_position', **{'nope': {'exists': True}})
        ])
        result = evaluate_stream(expect, self.EVENTS)
        assert not result.ok
        assert 'no event among the 3' in result.first.message

    def test_an_explicit_index(self):
        expect = stream(events=[
            EventAssertion(index=0, **{'status': {'state': 'TASK_STATE_WORKING'}})
        ])
        assert evaluate_stream(expect, self.EVENTS).ok

    def test_an_index_past_the_end(self):
        expect = stream(events=[EventAssertion(index=9, **{'a': {'exists': True}})])
        result = evaluate_stream(expect, self.EVENTS)
        assert not result.ok
        assert 'no event at index 9' in result.first.message

    def test_exact_position_is_the_default(self):
        """§7: the assertion's own place in the list is the event index."""
        expect = stream(events=[
            EventAssertion(**{'status': {'state': 'TASK_STATE_WORKING'}}),
            EventAssertion(**{'artifact': {'exists': True}}),
        ])
        assert evaluate_stream(expect, self.EVENTS).ok

    def test_exact_position_out_of_order_fails(self):
        expect = stream(events=[
            EventAssertion(**{'artifact': {'exists': True}}),
            EventAssertion(**{'status': {'state': 'TASK_STATE_WORKING'}}),
        ])
        assert not evaluate_stream(expect, self.EVENTS).ok

    def test_a_description_only_assertion_checks_nothing(self):
        expect = stream(events=[EventAssertion(description='prose')])
        assert evaluate_stream(expect, self.EVENTS).checks == 0

    def test_a_discriminated_one_of(self):
        """`STREAM-SSE-004`'s shape, verbatim."""
        expect = stream(events=[
            EventAssertion(index=0, **{
                'one_of': [
                    {'task': {'status': {'exists': True}}},
                    {'status_update': {'status': {'exists': True}}},
                ]
            })
        ])
        assert evaluate_stream(expect, self.EVENTS).ok


class TestTimeout:
    def test_a_timeout_is_itself_a_failure(self):
        """§7 calls it the max wait for stream *completion*."""
        result = evaluate_stream(
            stream(timeout_ms=100, min_count=1), [status('A')], timed_out=True
        )
        assert not result.ok
        assert 'did not complete within 100ms' in result.first.message

    def test_the_timeout_is_reported_before_the_count(self):
        """Otherwise the SUT is blamed for a shortfall the runner caused."""
        result = evaluate_stream(
            stream(timeout_ms=100, min_count=5), [status('A')], timed_out=True
        )
        assert 'did not complete' in result.first.message

    def test_no_timeout_no_complaint(self):
        assert evaluate_stream(stream(min_count=1), [status('A')]).ok


class TestCorpus:
    """The corpus's own streaming blocks, against plausible event streams."""

    def _blocks(self, suite):
        for loaded in suite.tests:
            for step in loaded.test.steps:
                if step.expect_stream is not None:
                    yield loaded.test.id, step.id, step.expect_stream

    def test_there_are_fifteen(self, suite):
        """Thirteen before the two raw REST subscribe tests."""
        assert len(list(self._blocks(suite))) == 15

    @pytest.mark.parametrize(
        'events',
        [
            [],
            [status('TASK_STATE_COMPLETED', 0)],
            [
                status('TASK_STATE_SUBMITTED', 0),
                status('TASK_STATE_WORKING', 1),
                artifact(2, text='x'),
                status('TASK_STATE_COMPLETED', 3),
            ],
            [normalize({'jsonrpc': '2.0', 'result': {}}, 0)],
        ],
    )
    def test_every_block_evaluates_without_raising(self, suite, events):
        for test_id, step_id, expect in self._blocks(suite):
            try:
                evaluate_stream(expect, events)
            except Exception as exc:  # pragma: no cover - the assertion reports it
                pytest.fail(f'{test_id}/{step_id} raised {exc!r}')

    def test_a_well_behaved_stream_satisfies_the_lifecycle_tests(self, suite):
        """`STREAM-SSE-001` is the canonical shape: 2+ events, ordered, terminal."""
        expect = suite.by_id('STREAM-SSE-001').test.steps[0].expect_stream
        good = [
            status('TASK_STATE_WORKING', 0),
            status('TASK_STATE_COMPLETED', 1),
        ]
        assert evaluate_stream(expect, good).ok

    def test_a_stream_that_regresses_fails_it(self, suite):
        expect = suite.by_id('STREAM-SSE-001').test.steps[0].expect_stream
        bad = [
            status('TASK_STATE_COMPLETED', 0),
            status('TASK_STATE_WORKING', 1),
            status('TASK_STATE_COMPLETED', 2),
        ]
        assert not evaluate_stream(expect, bad).ok

    def test_the_raw_sse_test_asserts_on_the_envelope(self, suite):
        """`JSONRPC-SSE-001` — `each_event: {jsonrpc: "2.0"}`, unwrapped."""
        expect = suite.by_id('JSONRPC-SSE-001').test.steps[0].expect_stream
        envelopes = [
            normalize({'jsonrpc': '2.0', 'id': 1, 'result': {}}, 0),
            normalize({'jsonrpc': '2.0', 'id': 1, 'result': {}}, 1),
        ]
        assert evaluate_stream(expect, envelopes).ok
        unwrapped = [status('TASK_STATE_WORKING', 0), status('TASK_STATE_COMPLETED', 1)]
        assert not evaluate_stream(expect, unwrapped).ok

    def test_the_raw_rest_subscribe_tests_read_through_the_discriminator(self, suite):
        """`REST-SUB-*` send SubscribeToTask as raw HTTP, so their events are
        not unwrapped and arrive as REST serializes them, `{"statusUpdate":
        ...}`. `final_event: {status: ...}` still has to reach the inner event.
        """
        wire = [
            normalize({'task': {'id': 't', 'status': {'state': 'TASK_STATE_WORKING'}}}, 0),
            normalize(
                {'statusUpdate': {'taskId': 't', 'status': {'state': 'TASK_STATE_COMPLETED'}}},
                1,
            ),
        ]
        for test_id in ('REST-SUB-GET-001', 'REST-SUB-POST-001'):
            expect = suite.by_id(test_id).test.steps[-1].expect_stream
            assert evaluate_stream(expect, wire).ok, test_id
            assert not evaluate_stream(expect, wire[:1]).ok, test_id
