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
from test_suite.acts.schema import EventAssertion, ExpectStream
from test_suite.acts.streaming import (
    DISCRIMINATORS,
    EVENT_KINDS,
    TERMINAL_STATES,
    StreamedEvent,
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
        assert EVENT_KINDS == {'task', 'message', 'status_update', 'artifact_update'}
        assert set(DISCRIMINATORS) >= EVENT_KINDS


class TestEventView:
    def test_the_payload_reads_straight_through(self):
        """`final_event: {status: ...}` — the corpus's flat style."""
        view = status('TASK_STATE_WORKING').view()
        assert view['status'] == {'state': 'TASK_STATE_WORKING'}

    def test_the_discriminator_addresses_the_same_event(self):
        """`{status_update: {status: ...}}` — the spec's style."""
        view = status('TASK_STATE_WORKING').view()
        assert view['status_update']['status'] == {'state': 'TASK_STATE_WORKING'}

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

    def test_there_are_twelve(self, suite):
        assert len(list(self._blocks(suite))) == 12

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
