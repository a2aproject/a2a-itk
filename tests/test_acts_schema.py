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
    ExpectError,
    ExpectStream,
    HttpMethod,
    Level,
    NamedAssertion,
    Operation,
    Step,
    StepKind,
    Suite,
    Test,
    TransportBinding,
    is_acts_document,
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
        """The corpus made exactly this mistake twice; see PROVENANCE.md §B."""
        with pytest.raises(ValidationError):
            Step.model_validate(_step(expect={'task': {'id': {'type': 'string'}}}))


class TestExpectError:
    def test_known_error_name_is_accepted(self):
        e = ExpectError.model_validate({'error_type': 'TaskNotFoundError'})
        assert e.literal_error_type() is ErrorType.TASK_NOT_FOUND

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
        constrain which". See PROVENANCE.md §C."""
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
        """Guards against a push-config style rename drifting back in; the
        corpus needed 18 such corrections (PROVENANCE.md §A.1)."""
        assert {o.value for o in Operation} == {
            'send_message', 'send_streaming_message', 'get_task', 'list_tasks',
            'cancel_task', 'subscribe_to_task', 'get_agent_card',
            'get_extended_agent_card', 'create_push_config', 'get_push_config',
            'list_push_configs', 'delete_push_config',
        }

    def test_acts_transport_vocabulary_is_not_the_traversal_one(self):
        """ACTS says `rest`; the traversal engine says `http_json`. Sharing
        one enum would silently mistranslate one of the two suites."""
        from test_suite.transports import Transport as TraversalTransport

        assert TransportBinding.REST.value == 'rest'
        assert 'rest' not in {t.value for t in TraversalTransport}
        assert 'http_json' not in {b.value for b in TransportBinding}
