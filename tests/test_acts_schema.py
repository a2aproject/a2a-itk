"""ACTS model validation.

The schema's job is to reject a document no runner could execute, while
leaving assertion trees alone (they are only interpretable against a real
response — see the module docstring in ``test_suite.acts.schema``). These
tests pin both halves of that: what must be rejected, and what must pass
through untouched.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from test_suite.acts.schema import (
    ActsDocument,
    ErrorType,
    EventMatch,
    ExpectError,
    ExpectStream,
    HttpMethod,
    Level,
    NamedAssertion,
    Operation,
    Ordering,
    RunnerRequirement,
    Step,
    StepKind,
    Suite,
    Test,
    TransportBinding,
    is_acts_document,
    parse_spec_version,
)


def _step(**overrides):
    base = {
        'id': 'send',
        'operation': 'send_message',
        'params': {'message': {'role': 'ROLE_USER', 'parts': [{'text': 'hi'}]}},
        'expect': {'status': 200},
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def _test(**overrides):
    base = {
        'id': 'CORE-SEND-001',
        'name': 'SendMessage returns a task',
        'level': 'must',
        'steps': [_step()],
    }
    base.update(overrides)
    return base


def _document(**overrides):
    base = {
        'acts_version': '1.0',
        'spec_version': '1.0',
        'suites': [{'id': 'core', 'name': 'Core', 'tests': [_test()]}],
    }
    base.update(overrides)
    return base


class TestDocument:
    def test_minimal_document(self):
        doc = ActsDocument.model_validate(_document())
        assert doc.acts_version == '1.0'
        assert len(doc.suites) == 1
        assert doc.suites[0].tests[0].level is Level.MUST

    def test_manifest_with_only_include_is_valid(self):
        doc = ActsDocument.model_validate({
            'acts_version': '1.0',
            'spec_version': '1.0',
            'include': ['discovery.acts.yaml'],
            'suites': None,
        })
        assert doc.include == ['discovery.acts.yaml']
        assert doc.suites is None

    def test_neither_include_nor_suites_is_rejected(self):
        """A document that contributes no tests is an authoring mistake."""
        with pytest.raises(ValidationError, match='needs `include` or `suites`'):
            ActsDocument.model_validate({'acts_version': '1.0', 'spec_version': '1.0'})

    def test_duplicate_suite_id_is_rejected(self):
        suite = {'id': 'core', 'name': 'Core', 'tests': [_test()]}
        other = {'id': 'core', 'name': 'Core again',
                 'tests': [_test(id='CORE-SEND-002')]}
        with pytest.raises(ValidationError, match='duplicate suite id'):
            ActsDocument.model_validate(_document(suites=[suite, other]))

    def test_unknown_top_level_key_is_rejected(self):
        """An unknown key is a typo or a stale field; ignoring it drops
        whatever the author meant to say."""
        with pytest.raises(ValidationError):
            ActsDocument.model_validate(_document(sutes=[]))

    def test_variables_are_carried(self):
        doc = ActsDocument.model_validate(_document(variables={'baseUrl': '{{SUT}}'}))
        assert doc.variables == {'baseUrl': '{{SUT}}'}


class TestDiscriminator:
    def test_acts_document_is_recognised(self):
        assert is_acts_document({'acts_version': '1.0'}) is True

    def test_traversal_scenario_is_not_an_acts_document(self):
        """Both formats can sit in scenarios/; neither loader may claim the
        other's files."""
        assert is_acts_document({'schema': 'traversal/v1', 'name': 'x'}) is False

    def test_non_mapping_is_not_an_acts_document(self):
        assert is_acts_document([{'acts_version': '1.0'}]) is False


class TestSuiteAndTest:
    def test_empty_test_list_is_rejected(self):
        with pytest.raises(ValidationError):
            Suite.model_validate({'id': 'core', 'name': 'Core', 'tests': []})

    def test_empty_step_list_is_rejected(self):
        with pytest.raises(ValidationError):
            Test.model_validate(_test(steps=[]))

    def test_duplicate_test_id_within_a_suite_is_rejected(self):
        with pytest.raises(ValidationError, match='duplicate test id'):
            Suite.model_validate({
                'id': 'core', 'name': 'Core', 'tests': [_test(), _test()],
            })

    def test_duplicate_step_id_is_rejected(self):
        """`{{send.taskId}}` would resolve against whichever step iteration
        reached last — invisible in the file."""
        with pytest.raises(ValidationError, match='duplicate step id'):
            Test.model_validate(_test(steps=[_step(), _step()]))

    def test_unknown_level_is_rejected(self):
        with pytest.raises(ValidationError):
            Test.model_validate(_test(level='required'))

    def test_behaviors_defaults_to_empty(self):
        assert Test.model_validate(_test()).behaviors() == frozenset()

    def test_behaviors_are_collected(self):
        t = Test.model_validate(_test(requires_behaviors=['tck-multi-turn']))
        assert t.behaviors() == frozenset({'tck-multi-turn'})

    def test_unrestricted_test_applies_to_every_transport(self):
        t = Test.model_validate(_test())
        assert all(t.applies_to(b) for b in TransportBinding)

    def test_restricted_test_applies_only_to_its_bindings(self):
        t = Test.model_validate(_test(transport=['jsonrpc']))
        assert t.applies_to(TransportBinding.JSONRPC)
        assert not t.applies_to(TransportBinding.GRPC)
        assert not t.applies_to(TransportBinding.REST)


class TestVersionRange:
    """Which A2A versions a test applies to.

    One corpus serves every version, so a wrong answer here either runs a test
    against a version that never had the requirement or drops it from the
    version that does.
    """

    def test_a_test_without_bounds_applies_to_every_version(self):
        test = Test.model_validate(_test())
        assert test.applies_to_version('1.0')
        assert test.applies_to_version('1.1')
        assert test.applies_to_version('2.0')

    def test_the_minimum_is_inclusive(self):
        test = Test.model_validate(_test(min_spec_version='1.1'))
        assert not test.applies_to_version('1.0')
        assert test.applies_to_version('1.1')
        assert test.applies_to_version('1.2')

    def test_the_maximum_is_inclusive(self):
        test = Test.model_validate(_test(max_spec_version='1.0'))
        assert test.applies_to_version('1.0')
        assert not test.applies_to_version('1.1')

    def test_both_bounds_together(self):
        test = Test.model_validate(
            _test(min_spec_version='1.1', max_spec_version='1.2')
        )
        assert [
            test.applies_to_version(v) for v in ('1.0', '1.1', '1.2', '1.3')
        ] == [False, True, True, False]

    def test_versions_compare_as_numbers_not_as_text(self):
        """As text, "1.10" sorts before "1.9"."""
        test = Test.model_validate(_test(min_spec_version='1.10'))
        assert not test.applies_to_version('1.9')
        assert test.applies_to_version('1.10')
        assert parse_spec_version('1.10') > parse_spec_version('1.9')

    @pytest.mark.parametrize(
        'bound', ['1', '1.0.0', 'v1.1', '1.x', '', '01.0', '1.01', '\u0661.\u0660'],
    )
    def test_a_bound_that_is_not_a_version_is_rejected(self, bound):
        with pytest.raises(ValidationError, match='min_spec_version'):
            Test.model_validate(_test(min_spec_version=bound))
        with pytest.raises(ValidationError, match='max_spec_version'):
            Test.model_validate(_test(max_spec_version=bound))

    def test_an_unquoted_yaml_number_is_rejected(self):
        """`max_spec_version: 1.10` unquoted is the float 1.1."""
        with pytest.raises(ValidationError, match='max_spec_version'):
            Test.model_validate(_test(max_spec_version=1.10))

    def test_an_empty_range_is_rejected(self):
        """It would drop the test from every run without saying so."""
        with pytest.raises(ValidationError, match='would apply to no version'):
            Test.model_validate(
                _test(min_spec_version='1.1', max_spec_version='1.0')
            )

    @pytest.mark.parametrize(
        ('bounds', 'expected'),
        [
            ({}, 'every A2A version'),
            ({'min_spec_version': '1.1'}, 'A2A 1.1 and newer'),
            ({'max_spec_version': '1.0'}, 'A2A 1.0 and older'),
            ({'min_spec_version': '1.1', 'max_spec_version': '1.2'}, 'A2A 1.1 to 1.2'),
            ({'min_spec_version': '1.1', 'max_spec_version': '1.1'}, 'A2A 1.1'),
        ],
    )
    def test_the_range_reads_as_words(self, bounds, expected):
        assert Test.model_validate(_test(**bounds)).version_range() == expected


class TestStepKind:
    @pytest.mark.parametrize(('raw', 'expected'), [
        (_step(), StepKind.OPERATION),
        ({'id': 's', 'raw': {'method': 'GET', 'path': '/tasks/x'}}, StepKind.RAW),
        ({'id': 's',
          'client_response': {'operation': 'get_task', 'wire_payload': {}},
          'expect_parsed': {'id': 'x'}}, StepKind.CLIENT),
        ({'id': 's',
          'assertion': {'source': '{{a.response}}',
                        'any': {'path': 'x[*]', 'match': {'text': {'type': 'string'}}}}},
         StepKind.ASSERTION),
    ])
    def test_kind_is_derived_from_the_payload(self, raw, expected):
        assert Step.model_validate(raw).kind() is expected

    def test_a_step_with_no_kind_is_rejected(self):
        with pytest.raises(ValidationError, match='a step needs one of'):
            Step.model_validate({'id': 's', 'expect': {'status': 200}})

    def test_a_step_with_two_kinds_is_rejected(self):
        with pytest.raises(ValidationError, match='exactly one kind'):
            Step.model_validate({
                'id': 's',
                'operation': 'get_task',
                'raw': {'method': 'GET', 'path': '/'},
            })


class TestStepFieldsMatchKind:
    """Fields the step's kind cannot act on are never evaluated.

    Left accepted they read as working assertions while asserting nothing —
    a green test that tested nothing, which is worse than a red one.
    """

    def test_params_on_a_raw_step_is_rejected(self):
        with pytest.raises(ValidationError, match='`params` belongs to an operation step'):
            Step.model_validate({
                'id': 's',
                'raw': {'method': 'GET', 'path': '/'},
                'params': {'id': 'x'},
            })

    def test_expect_parsed_on_an_operation_step_is_rejected(self):
        with pytest.raises(ValidationError, match='`expect_parsed` belongs to a client_response'):
            Step.model_validate(_step(expect=None, expect_parsed={'id': 'x'}))

    def test_client_step_without_expect_parsed_is_rejected(self):
        with pytest.raises(ValidationError, match='needs `expect_parsed`'):
            Step.model_validate({
                'id': 's',
                'client_response': {'operation': 'get_task', 'wire_payload': {}},
            })

    def test_client_step_cannot_expect_a_response(self):
        """It sends nothing, so there is no response to assert on."""
        with pytest.raises(ValidationError, match='a client_response step sends nothing'):
            Step.model_validate({
                'id': 's',
                'client_response': {'operation': 'get_task', 'wire_payload': {}},
                'expect_parsed': {'id': 'x'},
                'expect': {'status': 200},
            })

    def test_assertion_step_cannot_capture(self):
        with pytest.raises(ValidationError, match='an assertion step only re-checks'):
            Step.model_validate({
                'id': 's',
                'assertion': {'source': '{{a.response}}',
                              'any': {'path': 'x[*]', 'match': {'t': {'type': 'string'}}}},
                'capture': {'taskId': 'task.id'},
            })

    def test_expect_body_and_expect_error_together_is_rejected(self):
        with pytest.raises(ValidationError, match='cannot both return a body and fail'):
            Step.model_validate(_step(
                expect={'body': {'task': {'id': 'x'}}},
                expect_error={'error_type': 'TaskNotFoundError'},
            ))

    def test_expect_status_alongside_expect_error_is_allowed(self):
        """A JSON-RPC error rides an HTTP 200, so asserting the status of a
        failure is coherent."""
        s = Step.model_validate(_step(
            expect={'status': 200},
            expect_error={'error_type': 'TaskNotFoundError'},
        ))
        assert s.expect.status == 200
        assert s.expect_error.literal_error_type() is ErrorType.TASK_NOT_FOUND

    def test_expect_error_on_a_raw_step_is_allowed(self):
        """Spec §4.4 permits it explicitly; the runner maps the wire error
        back to an abstract name. Being stricter than the spec would reject
        somebody else's valid suite."""
        s = Step.model_validate({
            'id': 's',
            'raw': {'method': 'GET', 'path': '/'},
            'expect_error': {'error_type': 'TaskNotFoundError'},
        })
        assert s.kind() is StepKind.RAW
        assert s.expect_error.literal_error_type() is ErrorType.TASK_NOT_FOUND

    def test_repeat_on_a_client_step_is_rejected(self):
        with pytest.raises(ValidationError, match='a step that talks to the SUT'):
            Step.model_validate({
                'id': 's',
                'client_response': {'operation': 'get_task', 'wire_payload': {}},
                'expect_parsed': {'id': 'x'},
                'repeat': {'until': 'true'},
            })


class TestProtocolVersionOnAStep:
    def test_an_operation_step_may_name_the_version_it_is_sent_as(self):
        step = Step.model_validate(_step(a2a_version='1.99'))
        assert step.a2a_version == '1.99'
        assert Step.model_validate(_step()).a2a_version is None

    def test_a_raw_step_writes_its_own_header(self):
        with pytest.raises(ValidationError, match='writes its own `A2A-Version` header'):
            Step.model_validate({
                'id': 'r', 'raw': {'method': 'POST', 'path': '/'}, 'a2a_version': '1.0',
            })

    def test_it_is_not_for_a_step_that_sends_nothing_itself(self):
        with pytest.raises(ValidationError, match='belongs to an operation step'):
            Step.model_validate({
                'id': 'w', 'expect_webhook': {'task_id': 'T1'}, 'a2a_version': '1.0',
            })

    @pytest.mark.parametrize('written', [1.0, 1, ''])
    def test_the_version_is_written_as_text(self, written):
        """Unquoted, YAML reads `1.10` as the number 1.1."""
        with pytest.raises(ValidationError, match='a2a_version'):
            Step.model_validate(_step(a2a_version=written))


class TestStepDuration:
    def test_bounds_are_kept(self):
        step = Step.model_validate(_step(min_duration_ms=1000, max_duration_ms=5000))
        assert (step.min_duration_ms, step.max_duration_ms) == (1000, 5000)

    @pytest.mark.parametrize('name', ['min_duration_ms', 'max_duration_ms'])
    def test_a_bound_cannot_be_negative(self, name):
        with pytest.raises(ValidationError):
            Step.model_validate(_step(**{name: -1}))

    def test_bounds_no_response_can_satisfy_are_rejected(self):
        with pytest.raises(ValidationError, match='no response can satisfy both'):
            Step.model_validate(_step(min_duration_ms=2000, max_duration_ms=1000))

    @pytest.mark.parametrize('name', ['min_duration_ms', 'max_duration_ms'])
    def test_a_bound_times_one_request_not_several(self, name):
        with pytest.raises(ValidationError, match='`repeat` sends several'):
            Step.model_validate(
                _step(**{name: 1000}, repeat={'until': 'status.state == X'})
            )

    @pytest.mark.parametrize('name', ['min_duration_ms', 'max_duration_ms'])
    def test_a_bound_is_not_for_a_step_that_reads_a_stream(self, name):
        with pytest.raises(ValidationError, match='this step reads a stream'):
            Step.model_validate(
                _step(operation='subscribe_to_task', params={'id': 'T1'}, expect=None,
                      expect_stream={'min_count': 1}, **{name: 1000})
            )

    @pytest.mark.parametrize('name', ['min_duration_ms', 'max_duration_ms'])
    def test_a_bound_is_for_an_operation_step(self, name):
        with pytest.raises(ValidationError, match='times the response to an operation'):
            Step.model_validate({
                'id': 'r', 'raw': {'method': 'POST', 'path': '/'}, name: 1000,
            })


class TestBackgroundStep:
    """A step whose request stays open while the steps after it run."""

    @staticmethod
    def _background(**overrides):
        return _step(background=True, **overrides)

    def _a_test(self, *steps, **overrides):
        overrides.setdefault('runner_requirements', ['concurrent_requests'])
        return Test.model_validate(_test(steps=list(steps), **overrides))

    def test_it_is_kept(self):
        test = self._a_test(self._background(), _step(id='next'))
        assert [step.background for step in test.steps] == [True, None]

    def test_it_can_open_a_stream(self):
        step = Step.model_validate(self._background(
            operation='subscribe_to_task', params={'id': 'T1'}, expect=None,
            expect_stream={'min_count': 1}, capture={'taskId': 'task.id'},
        ))
        assert step.background is True

    def test_a_request_sent_again_cannot_stay_open(self):
        with pytest.raises(ValidationError, match='set one or the other'):
            Step.model_validate(
                self._background(repeat={'until': 'status.state == X'})
            )

    def test_repeat_is_still_allowed_on_a_step_that_says_it_is_not_background(self):
        step = Step.model_validate(
            _step(background=False, repeat={'until': 'status.state == X'})
        )
        assert step.repeat is not None

    @pytest.mark.parametrize('step', [
        {'id': 'r', 'raw': {'method': 'POST', 'path': '/'}},
        {'id': 'w', 'expect_webhook': {'task_id': 'T1'}},
        {
            'id': 'c',
            'client_response': {'operation': 'get_task', 'wire_payload': {}},
            'expect_parsed': {'id': 'x'},
        },
    ])
    def test_it_is_for_an_operation_step(self, step):
        with pytest.raises(ValidationError, match='keeps the request of an operation open'):
            Step.model_validate({**step, 'background': True})

    def test_the_test_has_to_declare_the_runner_requirement(self):
        with pytest.raises(ValidationError, match='concurrent_requests'):
            Test.model_validate(_test(steps=[self._background(), _step(id='next')]))

    def test_a_step_that_is_not_background_declares_nothing(self):
        test = Test.model_validate(_test(steps=[_step(background=False)]))
        assert test.runner_requirements is None


class TestClientResponse:
    """The canned response a client step feeds to the SDK's own client."""

    STREAM = [
        {'jsonrpc': '2.0', 'id': '1', 'result': {'task': {'id': 'T1'}}},
        {'jsonrpc': '2.0', 'id': '1', 'result': {'statusUpdate': {'taskId': 'T1'}}},
    ]

    @staticmethod
    def _client_step(**response):
        return {
            'id': 'parse',
            'client_response': response,
            'expect_parsed': {'events': {'count': 2}},
        }

    def test_a_stream_of_events_is_kept_in_order(self):
        step = Step.model_validate(self._client_step(
            operation='send_streaming_message', wire_stream=self.STREAM,
        ))
        assert step.kind() is StepKind.CLIENT
        assert step.client_response.wire_stream == self.STREAM
        assert step.client_response.wire_payload is None

    def test_one_payload_is_still_accepted(self):
        step = Step.model_validate(self._client_step(
            operation='get_task', wire_payload={'id': 'T1'},
        ))
        assert step.client_response.wire_payload == {'id': 'T1'}
        assert step.client_response.wire_stream is None

    def test_it_needs_one_of_the_two(self):
        with pytest.raises(ValidationError, match='got neither'):
            Step.model_validate(self._client_step(operation='get_task'))

    def test_it_cannot_have_both(self):
        with pytest.raises(ValidationError, match='exactly one of `wire_payload` and `wire_stream`'):
            Step.model_validate(self._client_step(
                operation='send_streaming_message',
                wire_payload={}, wire_stream=self.STREAM,
            ))

    @pytest.mark.parametrize('nothing', [[], None])
    def test_a_stream_lists_at_least_one_event(self, nothing):
        with pytest.raises(ValidationError):
            Step.model_validate(self._client_step(
                operation='send_streaming_message', wire_stream=nothing,
            ))

    def test_the_downlink_of_a_live_exchange_is_a_stream_a_client_can_be_fed(self):
        step = Step.model_validate(self._client_step(
            operation='send_live_message', wire_stream=self.STREAM,
        ))
        assert step.client_response.operation is Operation.SEND_LIVE_MESSAGE


class TestLiveMessageOperation:
    def test_an_operation_step_cannot_name_it(self):
        """It sends one request and reads one response; a live exchange is
        neither."""
        with pytest.raises(ValidationError, match='`send_live_message` is a two-way exchange'):
            Step.model_validate(_step(
                operation='send_live_message', expect=None,
                expect_stream={'min_count': 1},
            ))


class TestWebhookEvents:
    """`expect_webhook.events`: individual deliveries the step waits for."""

    @staticmethod
    def _webhook(**block):
        return Step.model_validate(
            {'id': 'delivered', 'expect_webhook': {'task_id': 'T1', **block}}
        )

    def test_they_are_event_assertions_like_those_of_a_stream(self):
        step = self._webhook(events=[
            {'description': 'the task completed', 'match': 'any_position',
             'status': {'state': 'TASK_STATE_COMPLETED'}},
            {'index': 0, 'task': {'id': 'T1'}},
        ])
        completed, first = step.expect_webhook.events
        assert completed.match is EventMatch.ANY_POSITION
        assert completed.model_extra == {'status': {'state': 'TASK_STATE_COMPLETED'}}
        assert first.index == 0

    def test_an_empty_list_is_rejected(self):
        """It would wait for nothing, and reads as if it waited."""
        with pytest.raises(ValidationError):
            self._webhook(events=[])

    def test_a_step_without_them_is_unchanged(self):
        assert self._webhook(min_count=2).expect_webhook.events is None


class TestLiveStep:
    """A two-way exchange (`SendLiveMessage`, from A2A 1.1): one request
    opens it, further requests follow, and the SUT streams events."""

    OPEN = {'send_message': {'message': {'role': 'ROLE_USER', 'parts': [{'text': 'hi'}]}}}

    def _live(self, live=None, **fields):
        return {'id': 'exchange', 'live': live or {'open': self.OPEN}, **fields}

    def _a_test(self, *steps, **overrides):
        overrides.setdefault('runner_requirements', ['live_stream'])
        return Test.model_validate(_test(steps=list(steps), **overrides))

    def test_the_whole_form(self):
        step = Step.model_validate(self._live(
            {
                'open': self.OPEN,
                'uplink': [
                    {
                        'id': 'rejected',
                        'description': 'A message the agent rejects',
                        'send_message': {'message': {'taskId': '{{exchange.taskId}}'}},
                        'expect_ack': {'id': '{{exchange.taskId}}'},
                    },
                    {
                        'after_event': {'error': {'referenceId': 'm-1'}},
                        'subscribe': {'id': '{{exchange.taskId}}', 'lastSeenGeneration': 3},
                    },
                    {'unrecognized': True},
                ],
            },
            a2a_version='1.1',
            capture={'taskId': 'task.id'},
            expect_stream={'timeout_ms': 20000, 'final_event': {'status': {'state': 'X'}}},
        ))
        assert step.kind() is StepKind.LIVE
        rejected, resumed, unknown = step.live.uplink
        assert step.live.open.send_message == self.OPEN['send_message']
        assert rejected.id == 'rejected'
        assert rejected.expect_ack == {'id': '{{exchange.taskId}}'}
        assert resumed.after_event == {'error': {'referenceId': 'm-1'}}
        assert resumed.subscribe.id == '{{exchange.taskId}}'
        assert resumed.subscribe.lastSeenGeneration == 3
        assert unknown.unrecognized is True

    def test_an_exchange_the_sut_must_refuse(self):
        step = Step.model_validate(self._live(
            {'open': {'subscribe': {'id': 'no-such-task'}}},
            expect_error={'error_type': 'TaskNotFoundError'},
        ))
        assert step.kind() is StepKind.LIVE

    @pytest.mark.parametrize('request_', [
        {},
        {'send_message': {'message': {}}, 'subscribe': {'id': 'T1'}},
        {'send_message': {'message': {}}, 'unrecognized': True},
    ])
    def test_a_request_is_exactly_one_kind(self, request_):
        with pytest.raises(ValidationError, match='a live request is exactly one of'):
            Step.model_validate(self._live({'open': request_}))
        with pytest.raises(ValidationError, match='a live request is exactly one of'):
            Step.model_validate(self._live({'open': self.OPEN, 'uplink': [request_]}))

    @pytest.mark.parametrize('written', [False, 1, 'true', None])
    def test_unrecognized_is_only_ever_true(self, written):
        with pytest.raises(ValidationError):
            Step.model_validate(self._live({'open': {'unrecognized': written}}))

    def test_a_subscription_may_carry_other_request_fields(self):
        step = Step.model_validate(
            self._live({'open': {'subscribe': {'id': 'T1', 'tenant': 'acme'}}})
        )
        assert step.live.open.subscribe.model_extra == {'tenant': 'acme'}

    @pytest.mark.parametrize('generation', [[3], {'at': 3}, 3.5])
    def test_a_generation_is_an_integer_or_a_string(self, generation):
        subscribe = {'id': 'T1', 'lastSeenGeneration': generation}
        with pytest.raises(ValidationError):
            Step.model_validate(self._live({'open': {'subscribe': subscribe}}))

    @pytest.mark.parametrize(
        'field', [{'id': 'first'}, {'after_event': {}}, {'expect_ack': {}}]
    )
    def test_the_request_that_opens_it_takes_no_field_of_a_later_one(self, field):
        with pytest.raises(ValidationError):
            Step.model_validate(self._live({'open': {**self.OPEN, **field}}))
        Step.model_validate(
            self._live({'open': self.OPEN, 'uplink': [{**self.OPEN, **field}]})
        )

    def test_a_later_request_that_is_named_has_a_name(self):
        with pytest.raises(ValidationError):
            Step.model_validate(
                self._live({'open': self.OPEN, 'uplink': [{**self.OPEN, 'id': ''}]})
            )

    def test_a_subscription_names_its_task(self):
        with pytest.raises(ValidationError, match='id'):
            Step.model_validate(self._live({'open': {'subscribe': {'lastSeenGeneration': 3}}}))

    def test_it_needs_the_request_that_opens_it(self):
        with pytest.raises(ValidationError, match='open'):
            Step.model_validate(self._live({'uplink': [self.OPEN]}))

    def test_an_empty_uplink_is_rejected(self):
        with pytest.raises(ValidationError):
            Step.model_validate(self._live({'open': self.OPEN, 'uplink': []}))

    def test_a_misspelled_member_is_rejected(self):
        with pytest.raises(ValidationError):
            Step.model_validate(self._live({'open': self.OPEN, 'uplinks': [self.OPEN]}))
        with pytest.raises(ValidationError):
            Step.model_validate(self._live({
                'open': self.OPEN, 'uplink': [{**self.OPEN, 'after_events': {}}],
            }))

    @pytest.mark.parametrize(('field', 'value'), [
        ('expect', {'status': 200}),
        ('params', {'id': 'T1'}),
        ('repeat', {'until': 'status.state == X'}),
        ('delay_ms', 100),
        ('min_duration_ms', 100),
        ('background', True),
        ('expect_parsed', {'id': 'T1'}),
        ('assertions', [{'source': '{{a.response}}', 'match': {'type': 'object'}}]),
    ])
    def test_fields_of_other_step_kinds_are_rejected(self, field, value):
        with pytest.raises(ValidationError):
            Step.model_validate(self._live(**{field: value}))

    def test_a_refused_exchange_is_sent_nothing_further(self):
        with pytest.raises(ValidationError, match='either `expect_error` or `live.uplink`'):
            Step.model_validate(self._live(
                {'open': self.OPEN, 'uplink': [self.OPEN]},
                expect_error={'error_type': 'UnsupportedOperationError'},
            ))

    def test_a_refusal_and_a_stream_cannot_both_be_expected(self):
        with pytest.raises(ValidationError, match='either `expect_error` or `expect_stream`'):
            Step.model_validate(self._live(
                expect_error={'error_type': 'UnsupportedOperationError'},
                expect_stream={'min_count': 1},
            ))

    def test_it_is_one_kind_of_step_and_not_two(self):
        with pytest.raises(ValidationError, match='exactly one kind'):
            Step.model_validate(self._live(operation='send_message'))

    def test_the_test_has_to_declare_the_runner_requirement(self):
        with pytest.raises(ValidationError, match='live_stream'):
            Test.model_validate(_test(steps=[self._live()]))
        assert self._a_test(self._live()).runner_requirements == [
            RunnerRequirement.LIVE_STREAM
        ]

    def test_a_version_override_is_still_refused_on_the_other_kinds(self):
        with pytest.raises(ValidationError, match='operation step or a live step'):
            Step.model_validate({
                'id': 'w', 'expect_webhook': {'task_id': 'T1'}, 'a2a_version': '1.0',
            })


class TestProtocolVersionsPrecondition:
    def test_versions_are_kept(self):
        test = Test.model_validate(
            _test(preconditions={'protocol_versions': ['1.0', '1.1']})
        )
        assert test.preconditions.protocol_versions == ['1.0', '1.1']

    @pytest.mark.parametrize('written', [[], ['1'], ['1.0.0'], ['latest'], [1.0]])
    def test_each_entry_is_a_version(self, written):
        """It is compared with the versions an agent card lists."""
        with pytest.raises(ValidationError, match='protocol_versions'):
            Test.model_validate(_test(preconditions={'protocol_versions': written}))


class TestStaysOpen:
    def test_it_is_kept_for_the_step_and_for_one_stream_of_it(self):
        block = ExpectStream.model_validate(
            {'stays_open_ms': 3000, 'streams': [{}, {'stays_open_ms': 500}]}
        )
        assert block.stays_open_ms == 3000
        assert [plan.stays_open_ms for plan in block.streams] == [None, 500]

    def test_a_stream_the_runner_hangs_up_on_cannot_show_it(self):
        with pytest.raises(ValidationError, match='has the runner hang up'):
            ExpectStream.model_validate(
                {'streams': [{'disconnect_after': 1, 'stays_open_ms': 500}]}
            )

    def test_the_steps_own_bound_applies_to_a_cut_stream_too(self):
        with pytest.raises(ValidationError, match='applies to every stream'):
            ExpectStream.model_validate(
                {'stays_open_ms': 500, 'streams': [{'disconnect_after': 1}, {}]}
            )

    @pytest.mark.parametrize('wait', [-1, 0])
    def test_it_has_to_be_a_wait(self, wait):
        """With none, the read ends before the SUT closing the stream could
        be seen, and the step passes whatever the SUT does."""
        with pytest.raises(ValidationError):
            ExpectStream.model_validate({'stays_open_ms': wait})

    @pytest.mark.parametrize('block', [
        {'stays_open_ms': 3000, 'timeout_ms': 3000},
        {'timeout_ms': 2000, 'streams': [{}, {'stays_open_ms': 3000}]},
    ])
    def test_the_deadline_has_to_outlast_it(self, block):
        """Or an agent that keeps the stream open fails on the deadline."""
        with pytest.raises(ValidationError, match='ends the read before'):
            ExpectStream.model_validate(block)
        ExpectStream.model_validate({**block, 'timeout_ms': 3001})


class TestRawOnlyTestsNeedATransport:
    """Spec §4.4. A raw request hard-codes one binding's wire shape."""

    RAW_STEP = {'id': 's', 'raw': {'method': 'POST', 'path': '/'}}

    def test_raw_only_test_without_transport_is_rejected(self):
        with pytest.raises(ValidationError, match='must declare `transport`'):
            Test.model_validate(_test(steps=[self.RAW_STEP]))

    def test_raw_only_test_with_transport_is_accepted(self):
        t = Test.model_validate(_test(steps=[self.RAW_STEP], transport=['jsonrpc']))
        assert t.transport == [TransportBinding.JSONRPC]

    def test_a_mixed_test_does_not_need_a_transport(self):
        """Its abstract steps are portable; only the raw one is not, and the
        spec scopes the rule to raw-only tests."""
        t = Test.model_validate(_test(steps=[_step(), self.RAW_STEP]))
        assert t.transport is None


class TestDeclaresTheRunnerFeaturesItUses:
    """Spec §7.2 and §7.3.

    §3.2 leaves `runner_requirements` optional, so nothing but this validator
    stops a test naming a capability in its prose and quietly running without
    it — which is what `STREAM-MULTI-001/002` and `STREAM-RESUB-001` did.
    """

    WEBHOOK_STEP = {'id': 'push', 'expect_webhook': {'task_id': 't-1'}}

    @staticmethod
    def _streaming(streams):
        return {
            'id': 'sub',
            'operation': 'send_streaming_message',
            'params': {'message': {'role': 'ROLE_USER', 'parts': [{'text': 'hi'}]}},
            'expect_stream': {'streams': streams},
        }

    def test_webhook_step_without_the_requirement_is_rejected(self):
        with pytest.raises(ValidationError, match='webhook_endpoint'):
            Test.model_validate(_test(steps=[self.WEBHOOK_STEP]))

    def test_webhook_step_with_the_requirement_is_accepted(self):
        t = Test.model_validate(_test(
            steps=[self.WEBHOOK_STEP],
            runner_requirements=['webhook_endpoint'],
        ))
        assert t.steps[0].kind() is StepKind.WEBHOOK

    def test_a_second_stream_without_the_requirement_is_rejected(self):
        with pytest.raises(ValidationError, match='concurrent_streams'):
            Test.model_validate(_test(
                steps=[self._streaming([{'min_count': 1}, {'min_count': 1}])]
            ))

    def test_a_disconnect_without_the_requirement_is_rejected(self):
        with pytest.raises(ValidationError, match='stream_disconnect'):
            Test.model_validate(_test(
                steps=[self._streaming([{'disconnect_after': 2}])]
            ))

    def test_one_undisturbed_stream_declares_nothing(self):
        """The boundary the rule turns on: a single plan needs no capability,
        and this is exactly what `STREAM-MULTI-001` ran while its description
        claimed two streams."""
        t = Test.model_validate(_test(steps=[self._streaming([{'min_count': 1}])]))
        assert t.runner_requirements is None


class TestAssertionTreesArePreserved:
    """The schema must not touch assertion subtrees.

    §5.2 makes `{status: {state: FOO}}` (two path levels then an exact match)
    syntactically identical to `{type: array, count_gte: 1}` (two operators).
    Only the evaluator can tell them apart, so both must survive intact.
    """

    def test_nested_exact_match_survives(self):
        s = Step.model_validate(_step(expect={
            'status': 200,
            'body': {'task': {'status': {'state': 'TASK_STATE_COMPLETED'}}},
        }))
        assert s.expect.body == {
            'task': {'status': {'state': 'TASK_STATE_COMPLETED'}},
        }

    def test_operator_map_survives(self):
        s = Step.model_validate(_step(expect={
            'body': {'artifacts': {'type': 'array', 'count_gte': 1}},
        }))
        assert s.expect.body == {'artifacts': {'type': 'array', 'count_gte': 1}}

    def test_status_may_be_an_assertion_object(self):
        s = Step.model_validate(_step(expect={'status': {'one_of': [200, 401, 403]}}))
        assert s.expect.status == {'one_of': [200, 401, 403]}

    def test_params_are_opaque(self):
        s = Step.model_validate(_step(params={'anything': {'at': ['all', 1, None]}}))
        assert s.params == {'anything': {'at': ['all', 1, None]}}

    def test_stray_key_directly_under_expect_is_rejected(self):
        """`expect` admits only `status`, `headers` and `body`.

        A response field placed directly here would send the runner looking
        for a top-level `task` on the response, and fail every time.
        """
        with pytest.raises(ValidationError):
            Step.model_validate(_step(expect={'task': {'id': {'type': 'string'}}}))

    def test_headers_are_accepted(self):
        s = Step.model_validate(
            _step(expect={'headers': {'Content-Type': {'starts_with': 'application/'}}})
        )
        assert s.expect.headers == {'Content-Type': {'starts_with': 'application/'}}


class TestExpectError:
    def test_known_error_name_is_accepted(self):
        e = ExpectError.model_validate({'error_type': 'TaskNotFoundError'})
        assert e.literal_error_type() is ErrorType.TASK_NOT_FOUND

    def test_the_a2a_1_1_error_name_is_accepted(self):
        e = ExpectError.model_validate({'error_type': 'TaskGenerationMismatchError'})
        assert e.literal_error_type() is ErrorType.TASK_GENERATION_MISMATCH

    def test_misspelled_error_name_is_rejected(self):
        """A name the spec doesn't define could never match, so the test
        would fail for the wrong reason."""
        with pytest.raises(ValidationError, match='unknown error_type'):
            ExpectError.model_validate({'error_type': 'TaskMissingError'})

    def test_error_type_may_be_an_assertion(self):
        e = ExpectError.model_validate({
            'error_type': {'one_of': ['TaskNotFoundError', 'TaskNotCancelableError']},
        })
        assert e.literal_error_type() is None

    def test_error_type_may_be_omitted(self):
        """Five corpus tests assert only a message — "some error, don't
        constrain which". The spec makes `error_type` optional for exactly
        that case: some requirements mandate a failure without naming it."""
        e = ExpectError.model_validate({'message': {'type': 'string'}})
        assert e.error_type is None
        assert e.literal_error_type() is None


class TestExpectStream:
    def test_counts_are_kept(self):
        s = ExpectStream.model_validate({'min_count': 2, 'max_count': 5})
        assert (s.min_count, s.max_count) == (2, 5)

    def test_impossible_count_range_is_rejected(self):
        with pytest.raises(ValidationError, match='no stream can satisfy both'):
            ExpectStream.model_validate({'min_count': 5, 'max_count': 2})

    def test_unknown_ordering_is_rejected(self):
        with pytest.raises(ValidationError):
            ExpectStream.model_validate({'ordering': 'sorted'})

    def test_every_ordering_rule_of_the_spec_is_accepted(self):
        for rule in ('monotonic_state', 'monotonic_generation', 'contiguous_generation'):
            assert ExpectStream.model_validate({'ordering': rule}).orderings() == {Ordering(rule)}

    def test_ordering_takes_one_rule_or_a_list(self):
        block = ExpectStream.model_validate(
            {'ordering': ['monotonic_state', 'monotonic_generation']}
        )
        assert block.orderings() == {
            Ordering.MONOTONIC_STATE, Ordering.MONOTONIC_GENERATION,
        }
        assert ExpectStream.model_validate({}).orderings() == frozenset()

    def test_an_empty_list_of_rules_is_rejected(self):
        """It reads as a constraint and checks nothing."""
        with pytest.raises(ValidationError):
            ExpectStream.model_validate({'ordering': []})

    def test_an_unknown_rule_in_a_list_is_rejected(self):
        with pytest.raises(ValidationError):
            ExpectStream.model_validate({'ordering': ['monotonic_state', 'sorted']})

    def test_a_stream_of_a_set_takes_the_rules_too(self):
        block = ExpectStream.model_validate(
            {'streams': [{'ordering': ['monotonic_generation']}, {}]}
        )
        assert block.streams[0].orderings() == {Ordering.MONOTONIC_GENERATION}
        assert block.streams[1].orderings() == frozenset()

    def test_event_payload_keys_are_open(self):
        """Event payload keys are assertion subtrees like any other body, so
        they are accepted rather than enumerated."""
        s = ExpectStream.model_validate({
            'events': [{'match': 'any_position', 'artifact': {'exists': True}}],
        })
        assert s.events[0].artifact == {'exists': True}


class TestRawBlock:
    def test_unknown_method_is_rejected(self):
        with pytest.raises(ValidationError):
            Step.model_validate({'id': 's', 'raw': {'method': 'PATCH', 'path': '/'}})

    def test_body_raw_carries_an_unparseable_payload(self):
        """The ParseError tests need a body that is deliberately not JSON."""
        s = Step.model_validate({
            'id': 's',
            'raw': {'method': 'POST', 'path': '/', 'body_raw': '{"broken": '},
        })
        assert s.raw.method is HttpMethod.POST
        assert s.raw.body_raw == '{"broken": '

    def test_body_and_body_raw_together_is_rejected(self):
        with pytest.raises(ValidationError, match='not both'):
            Step.model_validate({
                'id': 's',
                'raw': {'method': 'POST', 'path': '/', 'body': {}, 'body_raw': 'x'},
            })


class TestNamedAssertion:
    def test_a_check_is_required(self):
        with pytest.raises(ValidationError, match='asserts nothing'):
            NamedAssertion.model_validate({'source': '{{send.response}}'})

    def test_two_checks_are_rejected(self):
        with pytest.raises(ValidationError, match='only one of'):
            NamedAssertion.model_validate({
                'source': '{{send.response}}',
                'any': {'path': 'a[*]', 'match': {'x': 1}},
                'all': {'path': 'a[*]', 'match': {'x': 1}},
            })

    def test_collection_match_is_kept(self):
        a = NamedAssertion.model_validate({
            'source': '{{send.response}}',
            'any': {'path': 'task.artifacts[*].parts[*]',
                    'match': {'text': {'type': 'string'}}},
        })
        assert a.any.path == 'task.artifacts[*].parts[*]'
        assert a.any.match == {'text': {'type': 'string'}}


class TestEnumsMatchTheSpec:
    def test_operations_are_the_spec_set(self):
        """Guards against a push-config style rename drifting into the enum.

        The corpus once used `set_push_notification_config` and friends at 18
        sites; A2A §5.3 names none of them, and they are gone upstream.
        """
        assert {o.value for o in Operation} == {
            'send_message', 'send_streaming_message', 'get_task', 'list_tasks',
            'cancel_task', 'subscribe_to_task', 'get_agent_card',
            'get_extended_agent_card', 'create_push_config', 'get_push_config',
            'list_push_configs', 'delete_push_config',
            # From A2A 1.1.
            'send_live_message',
        }

    def test_acts_transport_vocabulary_is_not_the_traversal_one(self):
        """ACTS says `rest`; the traversal engine says `http_json`. Sharing
        one enum would silently mistranslate one of the two suites."""
        from test_suite.transports import Transport as TraversalTransport

        assert TransportBinding.REST.value == 'rest'
        assert 'rest' not in {t.value for t in TraversalTransport}
        assert 'http_json' not in {b.value for b in TransportBinding}
