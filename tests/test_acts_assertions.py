"""The assertion DSL.

Three things are worth the weight of a test here. The operators themselves,
which are mechanical but numerous. The **disambiguation** between an operator
map and a path descent, which is the only part of the format that cannot be
decided syntactically and so is the only part that can be silently wrong. And
the cases where an assertion could report success having compared nothing —
a conformance suite that does that is worse than one that fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_suite.acts import load_suite
from test_suite.acts.assertions import (
    LEAF_OPERATORS,
    OPERATORS,
    TYPE_NAMES,
    UntilError,
    evaluate,
    evaluate_body,
    evaluate_collection,
    evaluate_error,
    evaluate_named,
    evaluate_status,
    evaluate_until,
)
from test_suite.acts.schema import CollectionMatch, ExpectError, NamedAssertion
from test_suite.acts.variables import MISSING


CORPUS = (
    Path(__file__).resolve().parent.parent / 'scenarios' / 'acts' / 'suite.acts.yaml'
)


@pytest.fixture(scope='module')
def suite():
    return load_suite(CORPUS)


class TestExactMatch:
    @pytest.mark.parametrize(
        'value', ['TASK_STATE_COMPLETED', 1, 1.5, True, False, None]
    )
    def test_a_value_equals_itself(self, value):
        assert evaluate(value, value).ok

    def test_mismatch_reports_both_sides(self):
        result = evaluate('WANTED', 'GOT', path='body.state')
        assert not result.ok
        failure = result.first
        assert failure.path == 'body.state'
        assert failure.expected == 'WANTED'
        assert failure.actual == 'GOT'

    def test_bool_is_not_one(self):
        """Python says `True == 1`; JSON does not."""
        assert not evaluate(1, True).ok
        assert not evaluate(True, 1).ok

    def test_missing_never_matches(self):
        assert not evaluate('x', MISSING).ok

    def test_null_matches_null(self):
        assert evaluate(None, None).ok
        assert not evaluate(None, MISSING).ok


class TestTypeOperator:
    @pytest.mark.parametrize(
        ('name', 'value'),
        [
            ('string', 'a'),
            ('number', 1),
            ('number', 1.5),
            ('boolean', True),
            ('array', [1]),
            ('object', {'a': 1}),
            ('null', None),
        ],
    )
    def test_accepts(self, name, value):
        assert evaluate({'type': name}, value).ok

    @pytest.mark.parametrize(
        ('name', 'value'),
        [
            ('string', 1),
            ('number', True),
            ('number', '1'),
            ('boolean', 1),
            ('array', {'a': 1}),
            ('object', [1]),
            ('null', 'null'),
        ],
    )
    def test_rejects(self, name, value):
        assert not evaluate({'type': name}, value).ok

    def test_every_spec_type_name_is_implemented(self):
        assert set(TYPE_NAMES) == {
            'string', 'number', 'boolean', 'array', 'object', 'null'
        }

    def test_missing_is_not_null(self):
        assert not evaluate({'type': 'null'}, MISSING).ok


class TestPresenceOperators:
    def test_exists_on_a_value(self):
        assert evaluate({'exists': True}, 'x').ok

    def test_exists_on_a_null(self):
        """Present-and-null is present."""
        assert evaluate({'exists': True}, None).ok

    def test_exists_on_a_miss(self):
        assert not evaluate({'exists': True}, MISSING).ok

    def test_absent_on_a_miss(self):
        assert evaluate({'absent': True}, MISSING).ok

    def test_absent_on_a_null(self):
        assert not evaluate({'absent': True}, None).ok

    def test_exists_false_is_absent(self):
        assert evaluate({'exists': False}, MISSING).ok
        assert not evaluate({'exists': False}, 'x').ok

    def test_the_corpus_idiom_for_optional_fields(self):
        """`any_of: [absent, null]` is how the corpus says "omitted or null"."""
        assertion = {'any_of': [{'absent': True}, {'type': 'null'}]}
        assert evaluate(assertion, MISSING).ok
        assert evaluate(assertion, None).ok
        assert not evaluate(assertion, [1]).ok


class TestStringOperators:
    @pytest.mark.parametrize(
        ('assertion', 'value', 'expected'),
        [
            ({'contains': 'ell'}, 'hello', True),
            ({'contains': 'xyz'}, 'hello', False),
            ({'contains': 'a'}, ['a', 'b'], True),
            ({'contains': 'z'}, ['a', 'b'], False),
            ({'starts_with': 'he'}, 'hello', True),
            ({'starts_with': 'lo'}, 'hello', False),
            ({'ends_with': 'lo'}, 'hello', True),
            ({'ends_with': 'he'}, 'hello', False),
            ({'matches': r'^\d{4}-\d{2}'}, '2026-09-02T00:00:00Z', True),
            ({'matches': r'^\d{4}'}, 'nope', False),
            ({'matches': '.+'}, '', False),
        ],
    )
    def test_cases(self, assertion, value, expected):
        assert evaluate(assertion, value).ok is expected

    def test_matches_searches_rather_than_anchoring(self):
        """ECMA-262 `RegExp.test` semantics: unanchored search."""
        assert evaluate({'matches': 'ErrorInfo'}, 'type.googleapis.com/ErrorInfo').ok

    def test_string_operator_on_a_non_string_fails_rather_than_raises(self):
        assert not evaluate({'starts_with': 'a'}, 5).ok


class TestNumericOperators:
    @pytest.mark.parametrize(
        ('assertion', 'value', 'expected'),
        [
            ({'gte': 1}, 1, True),
            ({'gte': 2}, 1, False),
            ({'lte': 1}, 1, True),
            ({'lte': 0}, 1, False),
            ({'gt': 0}, 1, True),
            ({'gt': 1}, 1, False),
            ({'lt': 2}, 1, True),
            ({'lt': 1}, 1, False),
        ],
    )
    def test_cases(self, assertion, value, expected):
        assert evaluate(assertion, value).ok is expected

    def test_a_bool_is_not_comparable(self):
        assert not evaluate({'gte': 0}, True).ok

    def test_a_string_is_not_comparable(self):
        assert not evaluate({'gte': 0}, '5').ok


class TestCountOperators:
    @pytest.mark.parametrize(
        ('assertion', 'value', 'expected'),
        [
            ({'count': 0}, [], True),
            ({'count': 2}, [1, 2], True),
            ({'count': 1}, [1, 2], False),
            ({'count_gte': 1}, [1], True),
            ({'count_gte': 2}, [1], False),
            ({'count_lte': 2}, [1, 2], True),
            ({'count_lte': 1}, [1, 2], False),
        ],
    )
    def test_cases(self, assertion, value, expected):
        assert evaluate(assertion, value).ok is expected

    def test_count_needs_an_array(self):
        """§5.1 calls this "array length"; a string's length is not it."""
        assert not evaluate({'count': 3}, 'abc').ok
        assert not evaluate({'count': 1}, {'a': 1}).ok


class TestOneOf:
    def test_membership(self):
        assert evaluate({'one_of': ['A', 'B']}, 'B').ok
        assert not evaluate({'one_of': ['A', 'B']}, 'C').ok

    def test_numeric_alternatives(self):
        assert evaluate({'one_of': [-32600, -32602]}, -32602).ok

    def test_does_not_confuse_true_with_one(self):
        assert not evaluate({'one_of': [1]}, True).ok


class TestCombinators:
    def test_all_of_requires_every_branch(self):
        assert evaluate({'all_of': [{'type': 'string'}, {'contains': 'a'}]}, 'abc').ok
        assert not evaluate(
            {'all_of': [{'type': 'string'}, {'contains': 'z'}]}, 'abc'
        ).ok

    def test_any_of_requires_one(self):
        assert evaluate({'any_of': [{'type': 'number'}, {'type': 'string'}]}, 'a').ok

    def test_any_of_reports_one_failure_not_every_branch(self):
        result = evaluate({'any_of': [{'type': 'number'}, {'type': 'array'}]}, 'a')
        assert len(result.failures) == 1
        assert result.first.operator == 'any_of'

    def test_not_inverts(self):
        assert evaluate({'not': {'type': 'number'}}, 'a').ok
        assert not evaluate({'not': {'type': 'string'}}, 'a').ok

    def test_operators_in_one_map_are_anded(self):
        """§5.4: multiple operators combine with AND."""
        assert evaluate({'type': 'array', 'count_gte': 1}, [1]).ok
        assert not evaluate({'type': 'array', 'count_gte': 2}, [1]).ok


class TestNesting:
    RESPONSE = {
        'task': {
            'id': 'T1',
            'status': {'state': 'TASK_STATE_COMPLETED'},
            'artifacts': [{'parts': [{'text': 'hi'}]}],
        }
    }

    def test_yaml_nesting_mirrors_the_response(self):
        assert evaluate_body(
            {'task': {'status': {'state': 'TASK_STATE_COMPLETED'}}}, self.RESPONSE
        ).ok

    def test_a_deep_mismatch_reports_its_full_path(self):
        result = evaluate_body(
            {'task': {'status': {'state': 'TASK_STATE_FAILED'}}}, self.RESPONSE
        )
        assert result.first.path == 'body.task.status.state'

    def test_a_list_matches_by_position(self):
        """§5.2's array form."""
        assert evaluate_body(
            {'task': {'artifacts': [{'parts': [{'text': {'type': 'string'}}]}]}},
            self.RESPONSE,
        ).ok

    def test_positional_paths_are_bracketed(self):
        result = evaluate_body(
            {'task': {'artifacts': [{'parts': [{'text': 42}]}]}}, self.RESPONSE
        )
        assert result.first.path == 'body.task.artifacts[0].parts[0].text'

    def test_a_list_longer_than_the_response_fails(self):
        result = evaluate_body({'task': {'artifacts': [{}, {}]}}, self.RESPONSE)
        assert not result.ok

    def test_descending_into_a_missing_field_fails(self):
        result = evaluate_body({'task': {'nope': {'deeper': 1}}}, self.RESPONSE)
        assert not result.ok
        assert result.first.path == 'body.task.nope.deeper'


class TestDisambiguation:
    """The one part of the format that cannot be decided syntactically."""

    def test_a_known_name_with_a_valid_argument_is_an_operator(self):
        assert evaluate({'type': 'string'}, 'x').ok

    def test_a_known_name_with_an_invalid_argument_is_a_field(self):
        """RFC 9457 problem details really do have a member named `type`."""
        body = {'type': {'type': 'string'}, 'title': {'type': 'string'}}
        assert evaluate_body(body, {'type': 'about:blank', 'title': 'Not Found'}).ok

    def test_that_field_is_still_checked(self):
        body = {'type': {'type': 'string'}}
        result = evaluate_body(body, {'type': 404})
        assert not result.ok
        assert result.first.path == 'body.type'

    def test_operators_and_fields_can_share_a_map(self):
        """`{type: array, count_gte: 1, items: ...}` — the corpus does this."""
        assertion = {'type': 'array', 'count_gte': 1, 'items': {'id': {'exists': True}}}
        assert evaluate(assertion, [{'id': 'a'}]).ok
        assert not evaluate(assertion, []).ok

    def test_a_plain_field_named_like_an_operator_with_a_scalar(self):
        """`{'status': 404}` — `status` is not an operator, so it descends."""
        assert evaluate_body({'status': 404}, {'status': 404}).ok

    def test_the_documented_irreducible_ambiguity(self):
        """A field named `type` whose expected value is a type name.

        Nothing in A2A hits this and the format offers no escape, so the
        behavior is pinned rather than fixed: the operator wins.
        """
        assert evaluate({'type': 'string'}, 'anything at all').ok
        assert not evaluate({'type': 'string'}, {'type': 'string'}).ok


class TestItemsExtension:
    """Not in §5.1 — see the module docstring. JSON Schema semantics."""

    def test_a_map_applies_to_every_element(self):
        assertion = {'items': {'id': {'exists': True}}}
        assert evaluate(assertion, [{'id': 1}, {'id': 2}]).ok
        assert not evaluate(assertion, [{'id': 1}, {}]).ok

    def test_a_list_applies_positionally(self):
        assertion = {'items': [{'id': 'first'}]}
        assert evaluate(assertion, [{'id': 'first'}, {'id': 'second'}]).ok
        assert not evaluate(assertion, [{'id': 'second'}]).ok

    def test_it_needs_an_array(self):
        assert not evaluate({'items': {'a': 1}}, {'a': 1}).ok

    def test_element_failures_are_located(self):
        result = evaluate({'items': {'id': {'type': 'string'}}}, [{'id': 'a'}, {'id': 2}])
        assert result.first.path == '[1].id'


class TestResults:
    def test_checks_counts_what_actually_ran(self):
        """Distinguishes "passed" from "compared nothing"."""
        assert evaluate_body({}, {'a': 1}).checks == 0
        assert evaluate_body({'a': 1, 'b': {'type': 'null'}}, {'a': 1, 'b': None}).checks == 2

    def test_failures_accumulate_rather_than_short_circuiting(self):
        result = evaluate_body({'a': 1, 'b': 2}, {'a': 9, 'b': 9})
        assert len(result.failures) == 2

    def test_first_is_the_one_a_report_shows(self):
        result = evaluate_body({'a': 1}, {'a': 9})
        assert result.first is result.failures[0]

    def test_truthiness_tracks_ok(self):
        assert evaluate(1, 1)
        assert not evaluate(1, 2)

    def test_as_detail_matches_the_report_schema(self):
        """Spec §13.3 `failure-detail`; `step_id` is the runner's to add."""
        detail = evaluate_body({'a': 1}, {'a': 9}).first.as_detail()
        assert set(detail) == {'message', 'expected', 'actual', 'assertion_path'}
        assert detail['assertion_path'] == 'body.a'
        assert all(isinstance(v, str) for v in detail.values())


class TestExpectStatus:
    def test_a_bare_code(self):
        assert evaluate_status(200, 200).ok
        assert not evaluate_status(200, 404).ok

    def test_a_matcher(self):
        """The `SEC-AUTH-*` shape."""
        assert evaluate_status({'one_of': [200, 401, 403]}, 403).ok
        assert not evaluate_status({'one_of': [401, 403]}, 200).ok

    def test_path_is_named_for_the_report(self):
        assert evaluate_status(200, 500).first.path == 'status'


class TestExpectError:
    def test_a_literal_error_name(self):
        expected = ExpectError(error_type='TaskNotFoundError')
        assert evaluate_error(expected, {'error_type': 'TaskNotFoundError'}).ok

    def test_a_wrong_error_name(self):
        expected = ExpectError(error_type='TaskNotFoundError')
        result = evaluate_error(expected, {'error_type': 'InternalError'})
        assert not result.ok
        assert result.first.path == 'error.error_type'

    def test_an_unnamed_error(self):
        """A SUT that omits the `ErrorInfo` leaves the name unrecoverable."""
        expected = ExpectError(error_type='TaskNotFoundError')
        assert not evaluate_error(expected, {'message': 'not found'}).ok

    def test_error_type_may_be_an_assertion(self):
        expected = ExpectError(
            error_type={'one_of': ['TaskNotFoundError', 'InternalError']}
        )
        assert evaluate_error(expected, {'error_type': 'InternalError'}).ok

    def test_message_assertions(self):
        expected = ExpectError(error_type='TaskNotFoundError', message={'type': 'string'})
        observed = {'error_type': 'TaskNotFoundError', 'message': 'no such task'}
        assert evaluate_error(expected, observed).ok

    def test_omitting_error_type_asserts_only_what_is_given(self):
        """Five corpus tests assert only a message — "any A2A error"."""
        expected = ExpectError(message={'contains': 'not found'})
        assert evaluate_error(expected, {'message': 'task not found'}).ok
        assert evaluate_error(expected, {'message': 'nope'}).ok is False

    def test_details_are_a_nested_tree(self):
        expected = ExpectError(
            error_type='TaskNotFoundError', details={'reason': 'TASK_NOT_FOUND'}
        )
        observed = {
            'error_type': 'TaskNotFoundError',
            'details': {'reason': 'TASK_NOT_FOUND'},
        }
        assert evaluate_error(expected, observed).ok


class TestCollections:
    SOURCE = {
        'task': {
            'artifacts': [
                {'parts': [{'text': 'a'}, {'data': {}}]},
                {'parts': [{'text': 'b'}]},
            ]
        }
    }

    def match(self, **kwargs):
        return CollectionMatch(path='task.artifacts[*].parts[*]', match=kwargs)

    def test_any_passes_when_one_element_matches(self):
        assert evaluate_collection(
            self.match(text={'type': 'string'}), 'any', self.SOURCE
        ).ok

    def test_any_fails_when_none_do(self):
        assert not evaluate_collection(
            self.match(nope={'exists': True}), 'any', self.SOURCE
        ).ok

    def test_all_requires_every_element(self):
        """One of the three parts here has no `text`, so `all` must fail."""
        assert not evaluate_collection(
            self.match(text={'type': 'string'}), 'all', self.SOURCE
        ).ok

    def test_all_passes_when_every_element_matches(self):
        assert evaluate_collection(
            CollectionMatch(path='task.artifacts[*]', match={'parts': {'type': 'array'}}),
            'all',
            self.SOURCE,
        ).ok

    def test_an_empty_match_reports_no_checks(self):
        """Degenerate, but `checks` must not overstate what was compared."""
        result = evaluate_collection(self.match(**{}), 'all', self.SOURCE)
        assert result.ok
        assert result.checks == 0

    def test_none_passes_when_nothing_matches(self):
        assert evaluate_collection(
            self.match(nope={'exists': True}), 'none', self.SOURCE
        ).ok

    def test_none_fails_and_names_the_offenders(self):
        result = evaluate_collection(
            self.match(text={'type': 'string'}), 'none', self.SOURCE
        )
        assert not result.ok
        assert 'task.artifacts[0].parts[0]' in result.first.message

    def test_any_over_nothing_fails(self):
        """Reporting success having inspected zero elements is not allowed."""
        result = evaluate_collection(self.match(text={'exists': True}), 'any', {})
        assert not result.ok
        assert 'checked nothing' in result.first.message

    def test_all_over_nothing_fails_too(self):
        result = evaluate_collection(self.match(text={'exists': True}), 'all', {})
        assert not result.ok

    def test_none_over_nothing_passes(self):
        """"No element is X" is genuinely satisfied by having no elements."""
        assert evaluate_collection(self.match(text={'exists': True}), 'none', {}).ok

    def test_an_unknown_mode_is_a_programming_error(self):
        with pytest.raises(ValueError, match='mode'):
            evaluate_collection(self.match(), 'some', self.SOURCE)


class TestNamedAssertions:
    SOURCE = {'tasks': [{'id': 'T1'}, {'id': 'T2'}]}

    def test_match_against_the_whole_source(self):
        assertion = NamedAssertion(source='{{list.response}}', match={'type': 'object'})
        assert evaluate_named(assertion, self.SOURCE).ok

    def test_path_narrows_first(self):
        assertion = NamedAssertion(
            source='{{list.response}}', path='tasks', match={'count': 2}
        )
        assert evaluate_named(assertion, self.SOURCE).ok

    def test_a_collection_check(self):
        assertion = NamedAssertion(
            source='{{list.response}}',
            any=CollectionMatch(path='tasks[*]', match={'id': 'T2'}),
        )
        assert evaluate_named(assertion, self.SOURCE).ok

    def test_a_narrowing_path_that_misses(self):
        assertion = NamedAssertion(
            source='{{list.response}}', path='nope', match={'exists': True}
        )
        assert not evaluate_named(assertion, self.SOURCE).ok


class TestUntilExpressions:
    """Spec §9.1 — three forms, no more."""

    RESPONSE = {'status': {'state': 'TASK_STATE_WORKING'}, 'n': 3, 'done': False}

    @pytest.mark.parametrize(
        ('expression', 'expected'),
        [
            ('status.state == TASK_STATE_WORKING', True),
            ('status.state == TASK_STATE_COMPLETED', False),
            ('status.state != TASK_STATE_COMPLETED', True),
            ('status.state != TASK_STATE_WORKING', False),
            ('status.state in [TASK_STATE_COMPLETED, TASK_STATE_WORKING]', True),
            ('status.state in [TASK_STATE_COMPLETED, TASK_STATE_FAILED]', False),
            ('n == 3', True),
            ('n == 4', False),
            ('done == false', True),
            ('done == true', False),
            ("status.state == 'TASK_STATE_WORKING'", True),
        ],
    )
    def test_forms(self, expression, expected):
        assert evaluate_until(expression, self.RESPONSE) is expected

    def test_a_missing_path_is_simply_not_yet_true(self):
        """The ordinary state of a task being polled, not an error."""
        assert evaluate_until('status.state == X', {}) is False
        assert evaluate_until('status.state != X', {}) is True

    def test_whitespace_is_flexible(self):
        assert evaluate_until('status.state in [ TASK_STATE_WORKING ]', self.RESPONSE)

    @pytest.mark.parametrize(
        'expression', ['status.state', '== done', 'status.state in []', '']
    )
    def test_malformed_expressions_are_rejected(self, expression):
        with pytest.raises(UntilError):
            evaluate_until(expression, self.RESPONSE)


class TestOperatorCoverage:
    def test_every_spec_operator_is_implemented(self):
        """§5.1's grammar, in full — including the six the corpus never uses."""
        assert OPERATORS >= {
            'type', 'exists', 'absent', 'contains', 'matches', 'starts_with',
            'ends_with', 'gte', 'lte', 'gt', 'lt', 'count', 'count_gte',
            'count_lte', 'one_of', 'all_of', 'any_of', 'not',
        }

    def test_the_only_extension_is_items(self):
        """A new non-spec operator should have to be added deliberately."""
        assert OPERATORS - {
            'type', 'exists', 'absent', 'contains', 'matches', 'starts_with',
            'ends_with', 'gte', 'lte', 'gt', 'lt', 'count', 'count_gte',
            'count_lte', 'one_of', 'all_of', 'any_of', 'not',
        } == {'items'}

    def test_only_presence_operators_tolerate_a_missing_value(self):
        tolerant = {n for n, op in LEAF_OPERATORS.items() if op.tolerates_missing}
        assert tolerant == {'exists', 'absent'}


class TestCorpus:
    """Statements about the shipped corpus, so a refresh cannot drift."""

    def _assertion_trees(self, suite):
        for loaded in suite.tests:
            for step in loaded.test.steps:
                if step.expect and step.expect.body:
                    yield loaded.test.id, step.id, step.expect.body
                if step.expect_parsed:
                    yield loaded.test.id, step.id, step.expect_parsed

    @pytest.mark.parametrize(
        'probe',
        [
            {},
            None,
            [],
            'a string',
            {'task': {'status': {'state': 'X'}}, 'tasks': [], 'type': 'x'},
        ],
    )
    def test_every_expect_tree_evaluates_without_raising(self, suite, probe):
        """A hostile response must produce failures, never a crashed run."""
        for test_id, step_id, tree in self._assertion_trees(suite):
            try:
                evaluate_body(tree, probe)
            except Exception as exc:  # pragma: no cover - the assertion reports it
                pytest.fail(f'{test_id}/{step_id} raised {exc!r}')

    def test_exactly_one_key_shadows_an_operator_name(self, suite):
        """The disambiguation rule, measured against the real corpus.

        `REST-PD-001` asserts on an RFC 9457 body whose member is named
        `type`. If a refresh adds another such site, or if the rule regresses
        so that this one is read as an operator, this fails.
        """
        problem_details = suite.by_id('REST-PD-001')
        body = problem_details.test.steps[0].expect.body
        assert set(body) == {'type', 'title', 'status'}
        # Read as fields: a well-formed problem-details body passes.
        assert evaluate_body(
            body, {'type': 'about:blank', 'title': 'Not Found', 'status': 404}
        ).ok
        # Read as fields: a body whose `type` is not a string fails on it.
        result = evaluate_body(body, {'type': 404, 'title': 'x', 'status': 404})
        assert [f.path for f in result.failures] == ['body.type']

    def test_every_until_expression_parses(self, suite):
        seen = 0
        for loaded in suite.tests:
            for step in loaded.test.steps:
                if step.repeat:
                    evaluate_until(step.repeat.until, {})
                    seen += 1
        assert seen == 3

    def test_every_named_assertion_evaluates(self, suite):
        seen = 0
        for loaded in suite.tests:
            test = loaded.test
            named = list(test.assertions or [])
            for step in test.steps:
                named.extend(step.assertions or [])
            for assertion in named:
                evaluate_named(assertion, {})
                seen += 1
        assert seen == 11
