"""The compat rules that make the verbatim ACTS corpus loadable.

These are unit tests on synthetic documents. What the rules do to the *real*
corpus — and how many sites each fires on — is pinned separately in
``test_acts_corpus.py``, so that a corpus refresh and a rule change fail in
different places.

The property that matters most here is restraint: a rule may rename or move
what the document already says, and may not invent a value. A rule that has to
guess belongs upstream as a bug report, not here.
"""

from __future__ import annotations

import copy

import pytest

from test_suite.acts.compat import (
    EXPECTED_SITES,
    RULE_ERROR_TYPE_KEY,
    RULE_EXPECT_BODY_FIELD,
    RULE_EXPECT_ERROR_BLOCK,
    RULE_PUSH_CONFIG_OPERATION,
    normalize_document,
    site_counts,
)


def _doc(*steps):
    """A minimal valid-enough document wrapping ``steps``."""
    return {
        'acts_version': '1.0',
        'spec_version': '1.0',
        'suites': [
            {
                'id': 'suite-1',
                'name': 'Suite one',
                'tests': [
                    {
                        'id': 'T-001',
                        'name': 'Test one',
                        'level': 'must',
                        'steps': list(steps),
                    }
                ],
            }
        ],
    }


def _only_step(doc):
    return doc['suites'][0]['tests'][0]['steps'][0]


class TestErrorTypeKeyRule:
    def test_code_is_renamed_to_error_type(self):
        out, rewrites = normalize_document(
            _doc({'id': 's', 'operation': 'get_task',
                  'expect_error': {'code': 'TaskNotFoundError'}})
        )
        assert _only_step(out)['expect_error'] == {'error_type': 'TaskNotFoundError'}
        assert [r.rule for r in rewrites] == [RULE_ERROR_TYPE_KEY]

    def test_an_assertion_object_is_carried_across_unchanged(self):
        """The value moves as-is; the rule renames a key, it does not read it."""
        assertion = {'one_of': ['TaskNotFoundError', 'TaskNotCancelableError']}
        out, _ = normalize_document(
            _doc({'id': 's', 'operation': 'get_task',
                  'expect_error': {'code': assertion}})
        )
        assert _only_step(out)['expect_error']['error_type'] == assertion

    def test_sibling_fields_survive(self):
        out, _ = normalize_document(
            _doc({'id': 's', 'operation': 'get_task',
                  'expect_error': {'code': 'TaskNotFoundError',
                                   'message': {'type': 'string'}}})
        )
        assert _only_step(out)['expect_error'] == {
            'error_type': 'TaskNotFoundError',
            'message': {'type': 'string'},
        }

    def test_both_spellings_at_once_is_left_for_the_schema(self):
        """A genuine ambiguity, not a known defect. Guessing which one the
        author meant is exactly what these rules must not do."""
        step = {'id': 's', 'operation': 'get_task',
                'expect_error': {'code': 'TaskNotFoundError',
                                 'error_type': 'InternalError'}}
        out, rewrites = normalize_document(_doc(step))
        assert rewrites == []
        assert _only_step(out)['expect_error'] == step['expect_error']

    def test_a_correct_block_is_untouched(self):
        out, rewrites = normalize_document(
            _doc({'id': 's', 'operation': 'get_task',
                  'expect_error': {'error_type': 'TaskNotFoundError'}})
        )
        assert rewrites == []
        assert _only_step(out)['expect_error'] == {'error_type': 'TaskNotFoundError'}


class TestPushConfigOperationRule:
    @pytest.mark.parametrize(
        ('legacy', 'expected'),
        [
            ('set_push_notification_config', 'create_push_config'),
            ('get_push_notification_config', 'get_push_config'),
            ('list_push_notification_configs', 'list_push_configs'),
            ('delete_push_notification_config', 'delete_push_config'),
        ],
    )
    def test_legacy_names_are_replaced(self, legacy, expected):
        out, rewrites = normalize_document(_doc({'id': 's', 'operation': legacy}))
        assert _only_step(out)['operation'] == expected
        assert [r.rule for r in rewrites] == [RULE_PUSH_CONFIG_OPERATION]

    def test_a_spec_name_is_untouched(self):
        out, rewrites = normalize_document(
            _doc({'id': 's', 'operation': 'create_push_config'})
        )
        assert rewrites == []
        assert _only_step(out)['operation'] == 'create_push_config'

    def test_an_unrecognised_name_is_left_for_the_schema(self):
        out, rewrites = normalize_document(_doc({'id': 's', 'operation': 'teleport'}))
        assert rewrites == []
        assert _only_step(out)['operation'] == 'teleport'


class TestExpectBlockRules:
    def test_a_stray_response_field_moves_under_body(self):
        out, rewrites = normalize_document(
            _doc({'id': 's', 'operation': 'send_message',
                  'expect': {'task': {'id': {'type': 'string'}}}})
        )
        assert _only_step(out)['expect'] == {'body': {'task': {'id': {'type': 'string'}}}}
        assert [r.rule for r in rewrites] == [RULE_EXPECT_BODY_FIELD]

    def test_a_stray_field_merges_into_an_existing_body(self):
        out, _ = normalize_document(
            _doc({'id': 's', 'operation': 'send_message',
                  'expect': {'status': 200,
                             'body': {'kept': True},
                             'task': {'id': {'type': 'string'}}}})
        )
        assert _only_step(out)['expect'] == {
            'status': 200,
            'body': {'kept': True, 'task': {'id': {'type': 'string'}}},
        }

    def test_expect_error_field_becomes_an_expect_error_block(self):
        out, rewrites = normalize_document(
            _doc({'id': 's', 'operation': 'send_message',
                  'expect': {'error': {'exists': True}}})
        )
        step = _only_step(out)
        assert 'expect' not in step
        assert step['expect_error'] == {}
        assert [r.rule for r in rewrites] == [RULE_EXPECT_ERROR_BLOCK]

    def test_a_non_integer_status_is_dropped_alongside_the_error(self):
        """`status: error` is not an HTTP status — it is the same "this
        failed" intent spelled twice, and `expect_error` now carries it."""
        out, _ = normalize_document(
            _doc({'id': 's', 'operation': 'send_message',
                  'expect': {'status': 'error', 'error': {'exists': True}}})
        )
        step = _only_step(out)
        assert 'expect' not in step
        assert step['expect_error'] == {}

    def test_an_integer_status_survives_the_error_rewrite(self):
        """A real status assertion is coherent beside a failure: a JSON-RPC
        error rides an HTTP 200."""
        out, _ = normalize_document(
            _doc({'id': 's', 'operation': 'send_message',
                  'expect': {'status': 200, 'error': {'exists': True}}})
        )
        step = _only_step(out)
        assert step['expect'] == {'status': 200}
        assert step['expect_error'] == {}

    def test_an_existing_expect_error_is_not_overwritten(self):
        out, _ = normalize_document(
            _doc({'id': 's', 'operation': 'send_message',
                  'expect': {'error': {'exists': True}},
                  'expect_error': {'error_type': 'TaskNotFoundError'}})
        )
        assert _only_step(out)['expect_error'] == {'error_type': 'TaskNotFoundError'}

    def test_both_stray_kinds_in_one_block(self):
        out, rewrites = normalize_document(
            _doc({'id': 's', 'operation': 'send_message',
                  'expect': {'error': {'exists': True}, 'task': {'id': 'x'}}})
        )
        step = _only_step(out)
        assert step['expect'] == {'body': {'task': {'id': 'x'}}}
        assert step['expect_error'] == {}
        assert sorted(r.rule for r in rewrites) == sorted(
            [RULE_EXPECT_BODY_FIELD, RULE_EXPECT_ERROR_BLOCK]
        )

    def test_a_correct_block_is_untouched(self):
        expect = {'status': 200, 'body': {'task': {'id': {'type': 'string'}}}}
        out, rewrites = normalize_document(
            _doc({'id': 's', 'operation': 'send_message', 'expect': expect})
        )
        assert rewrites == []
        assert _only_step(out)['expect'] == expect


class TestNormalizerContract:
    def test_the_input_is_not_mutated(self):
        """The loader keeps the raw mapping from its include walk, and a
        caller diffing against upstream should not find it rewritten."""
        doc = _doc({'id': 's', 'operation': 'set_push_notification_config',
                    'expect_error': {'code': 'TaskNotFoundError'}})
        before = copy.deepcopy(doc)
        normalize_document(doc)
        assert doc == before

    def test_normalizing_twice_changes_nothing_more(self):
        doc = _doc({'id': 's', 'operation': 'set_push_notification_config',
                    'expect': {'error': {'exists': True}}})
        once, first = normalize_document(doc)
        twice, second = normalize_document(once)
        assert twice == once
        assert second == []
        assert first

    def test_a_clean_document_produces_no_rewrites(self):
        doc = _doc({'id': 's', 'operation': 'get_task',
                    'expect': {'status': 200, 'body': {'id': {'type': 'string'}}}})
        out, rewrites = normalize_document(doc)
        assert out == doc
        assert rewrites == []

    @pytest.mark.parametrize('value', [None, [], 'text', 42])
    def test_a_non_document_passes_through(self, value):
        """Reporting malformed input is the schema's job, not this module's."""
        out, rewrites = normalize_document(value)
        assert out is value
        assert rewrites == []

    def test_malformed_suites_and_tests_are_skipped_not_crashed_on(self):
        doc = {'suites': ['not a mapping', {'tests': ['also not a mapping']}]}
        out, rewrites = normalize_document(doc)
        assert out == doc
        assert rewrites == []

    def test_a_rewrite_records_where_it_fired(self):
        _, rewrites = normalize_document(
            _doc({'id': 'step-7', 'operation': 'get_push_notification_config'})
        )
        assert rewrites[0].where == 'T-001.step-7'
        assert 'get_push_notification_config' in rewrites[0].detail
        assert rewrites[0].rule in str(rewrites[0])


class TestSiteCounts:
    def test_every_rule_is_reported_even_at_zero(self):
        """A rule that stops firing must show as 0 rather than vanish — that
        is the signal to delete it."""
        assert site_counts([]) == dict.fromkeys(EXPECTED_SITES, 0)

    def test_counts_group_by_rule(self):
        _, rewrites = normalize_document(
            _doc(
                {'id': 'a', 'operation': 'set_push_notification_config'},
                {'id': 'b', 'operation': 'delete_push_notification_config'},
                {'id': 'c', 'operation': 'get_task',
                 'expect_error': {'code': 'TaskNotFoundError'}},
            )
        )
        counts = site_counts(rewrites)
        assert counts[RULE_PUSH_CONFIG_OPERATION] == 2
        assert counts[RULE_ERROR_TYPE_KEY] == 1
        assert counts[RULE_EXPECT_BODY_FIELD] == 0

    def test_the_pinned_table_names_every_rule(self):
        """A rule added without an entry would fire unpinned, which is how a
        corpus refresh stops being noticed."""
        assert set(EXPECTED_SITES) == {
            RULE_ERROR_TYPE_KEY,
            RULE_PUSH_CONFIG_OPERATION,
            RULE_EXPECT_BODY_FIELD,
            RULE_EXPECT_ERROR_BLOCK,
        }
