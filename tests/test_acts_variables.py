"""Substitution, paths and capture.

The behaviors pinned here are the ones a later change is most likely to break
without noticing: that a whole-string reference keeps the referenced value's
*type*, that a missing field stays distinguishable from a null one, and that
an unresolved reference stops the step instead of travelling to the SUT as
literal `{{...}}` text.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_suite.acts import load_suite
from test_suite.acts.variables import (
    MISSING,
    Index,
    Key,
    PathError,
    Scope,
    UnresolvedVariable,
    format_path,
    parse_path,
    read_path,
    read_path_all,
    references,
)


CORPUS = (
    Path(__file__).resolve().parent.parent / 'scenarios' / 'acts' / 'suite.acts.yaml'
)


@pytest.fixture(scope='module')
def suite():
    return load_suite(CORPUS)


class TestParsePath:
    @pytest.mark.parametrize(
        ('path', 'expected'),
        [
            ('', ()),
            ('id', (Key('id'),)),
            ('task.id', (Key('task'), Key('id'))),
            ('tasks[0]', (Key('tasks'), Index(0))),
            ('a[0][1]', (Key('a'), Index(0), Index(1))),
            ('task.artifacts[0].parts[1].text',
             (Key('task'), Key('artifacts'), Index(0),
              Key('parts'), Index(1), Key('text'))),
            ('@type', (Key('@type'),)),
        ],
    )
    def test_shapes(self, path, expected):
        assert parse_path(path) == expected

    def test_wildcard_parses(self):
        segments = parse_path('task.artifacts[*].parts[*]')
        assert format_path(segments) == 'task.artifacts[*].parts[*]'

    @pytest.mark.parametrize('path', ['task..id', 'task.[0', 'a]b'])
    def test_malformed_is_rejected(self, path):
        with pytest.raises(PathError):
            parse_path(path)

    def test_round_trips(self):
        for path in ('task.id', 'a[0].b[12].c', 'x'):
            assert format_path(parse_path(path)) == path


class TestReadPath:
    RESPONSE = {
        'task': {
            'id': 'T1',
            'status': {'state': 'TASK_STATE_COMPLETED'},
            'artifacts': [{'parts': [{'text': 'hi'}, {'data': {}}]}],
            'cursor': None,
        }
    }

    def test_reads_nested(self):
        assert read_path(self.RESPONSE, 'task.status.state') == 'TASK_STATE_COMPLETED'

    def test_reads_through_index(self):
        assert read_path(self.RESPONSE, 'task.artifacts[0].parts[0].text') == 'hi'

    def test_empty_path_is_the_root(self):
        assert read_path(self.RESPONSE, '') is self.RESPONSE

    @pytest.mark.parametrize(
        'path',
        [
            'task.nope',
            'task.artifacts[9]',
            'task.id.deeper',
            'nothing.at.all',
        ],
    )
    def test_a_miss_is_MISSING_not_an_error(self, path):
        assert read_path(self.RESPONSE, path) is MISSING

    def test_null_is_not_missing(self):
        """The whole reason `MISSING` exists rather than reusing `None`."""
        assert read_path(self.RESPONSE, 'task.cursor') is None
        assert read_path(self.RESPONSE, 'task.cursor') is not MISSING

    def test_a_string_is_not_indexable(self):
        """`'abc'[0]` is a Python truth, not a JSON one."""
        assert read_path({'a': 'abc'}, 'a[0]') is MISSING

    def test_wildcards_are_refused(self):
        """A capture or an `until` needs exactly one value."""
        with pytest.raises(PathError):
            read_path(self.RESPONSE, 'task.artifacts[*]')


class TestReadPathAll:
    RESPONSE = {
        'task': {
            'artifacts': [
                {'parts': [{'text': 'a'}, {'text': 'b'}]},
                {'parts': [{'text': 'c'}]},
            ]
        }
    }

    def test_expands_every_branch(self):
        found = list(read_path_all(self.RESPONSE, 'task.artifacts[*].parts[*].text'))
        assert [value for _, value in found] == ['a', 'b', 'c']

    def test_reports_concrete_paths(self):
        found = dict(read_path_all(self.RESPONSE, 'task.artifacts[*].parts[*]'))
        assert sorted(found) == [
            'task.artifacts[0].parts[0]',
            'task.artifacts[0].parts[1]',
            'task.artifacts[1].parts[0]',
        ]

    def test_missing_branches_are_skipped_not_raised(self):
        response = {'task': {'artifacts': [{'parts': [{'text': 'a'}]}, {}]}}
        found = list(read_path_all(response, 'task.artifacts[*].parts[*].text'))
        assert [v for _, v in found] == ['a']

    def test_no_match_yields_nothing(self):
        assert list(read_path_all({}, 'task.artifacts[*]')) == []

    def test_works_without_a_wildcard(self):
        assert list(read_path_all({'a': 1}, 'a')) == [('a', 1)]


class TestSubstitution:
    def test_whole_string_reference_keeps_the_value_type(self):
        """`'{{n}}'` yields the value; a captured int must stay an int."""
        scope = Scope({'n': 7, 'flag': True, 'obj': {'a': 1}})
        assert scope.substitute('{{n}}') == 7
        assert scope.substitute('{{flag}}') is True
        assert scope.substitute('{{obj}}') == {'a': 1}

    def test_embedded_reference_interpolates(self):
        scope = Scope({'token': 'abc'})
        assert scope.substitute('Bearer {{token}}') == 'Bearer abc'

    def test_several_embedded_references(self):
        scope = Scope({'a': 1, 'b': 2})
        assert scope.substitute('{{a}}-{{b}}') == '1-2'

    def test_recurses_into_structures(self):
        scope = Scope({'id': 'T1'})
        assert scope.substitute(
            {'params': {'id': '{{id}}', 'list': ['{{id}}', 2]}}
        ) == {'params': {'id': 'T1', 'list': ['T1', 2]}}

    def test_keys_are_left_alone(self):
        """§8.1 places references "within text values"; keys are field names."""
        scope = Scope({'k': 'other'})
        assert scope.substitute({'{{k}}': 1}) == {'{{k}}': 1}

    def test_whitespace_inside_braces_is_tolerated(self):
        assert Scope({'a': 1}).substitute('{{  a  }}') == 1

    def test_text_without_references_is_untouched(self):
        scope = Scope()
        assert scope.substitute('plain') == 'plain'
        assert scope.substitute(None) is None
        assert scope.substitute(3) == 3


class TestResolutionOrder:
    """Spec §12.2 precedence, highest first."""

    def test_step_capture_beats_a_document_variable_of_the_same_name(self):
        scope = Scope({'send.taskId': 'from-document'})
        scope.record('send', 'taskId', 'from-capture')
        assert scope.resolve('send.taskId') == 'from-capture'

    def test_dotted_document_variable_is_used_when_no_step_captured_it(self):
        scope = Scope({'send.taskId': 'from-document'})
        assert scope.resolve('send.taskId') == 'from-document'

    def test_env_reads_the_environment(self):
        scope = Scope(env={'TOKEN': 'shh'})
        assert scope.resolve('env.TOKEN') == 'shh'

    def test_uuid_is_fresh_at_every_occurrence(self):
        """Spec §8.1 says so explicitly."""
        seen = []
        scope = Scope(new_uuid=lambda: f'uuid-{len(seen)}')
        for _ in range(3):
            seen.append(scope.substitute('{{$uuid}}'))
        assert seen == ['uuid-0', 'uuid-1', 'uuid-2']

    def test_two_uuids_in_one_string_differ(self):
        counter = iter(['a', 'b'])
        scope = Scope(new_uuid=lambda: next(counter))
        assert scope.substitute('{{$uuid}}/{{$uuid}}') == 'a/b'


class TestUnresolved:
    """§12.2: an unresolvable reference MUST fail the step, clearly."""

    def test_unknown_bare_variable(self):
        with pytest.raises(UnresolvedVariable, match='nope'):
            Scope({'a': 1}).resolve('nope')

    def test_unknown_step(self):
        with pytest.raises(UnresolvedVariable, match='no step'):
            Scope().resolve('ghost.taskId')

    def test_known_step_unknown_capture_lists_what_it_has(self):
        scope = Scope()
        scope.record('send', 'taskId', 'T1')
        with pytest.raises(UnresolvedVariable, match=r"taskId"):
            scope.resolve('send.contextId')

    def test_unset_environment_variable(self):
        with pytest.raises(UnresolvedVariable, match='environment'):
            Scope(env={}).resolve('env.NOPE')

    def test_substitution_propagates_the_error(self):
        with pytest.raises(UnresolvedVariable):
            Scope().substitute({'params': {'id': '{{send.taskId}}'}})


class TestCapture:
    RESPONSE = {'task': {'id': 'T1', 'contextId': 'C1', 'cursor': None}}

    def test_captures_and_makes_them_resolvable(self):
        scope = Scope()
        captured = scope.capture(
            'send', {'taskId': 'task.id', 'contextId': 'task.contextId'}, self.RESPONSE
        )
        assert captured == {'taskId': 'T1', 'contextId': 'C1'}
        assert scope.resolve('send.taskId') == 'T1'

    def test_capturing_null_is_allowed(self):
        scope = Scope()
        assert scope.capture('s', {'c': 'task.cursor'}, self.RESPONSE) == {'c': None}

    def test_capturing_a_missing_path_raises(self):
        """Binding nothing would turn one failure into a cascade downstream."""
        scope = Scope()
        with pytest.raises(UnresolvedVariable, match='cannot capture'):
            scope.capture('send', {'taskId': 'task.nope'}, self.RESPONSE)

    def test_response_is_implicitly_available(self):
        scope = Scope()
        scope.record_response('get', self.RESPONSE)
        assert scope.resolve('get.response') is self.RESPONSE

    def test_an_explicit_capture_named_response_wins(self):
        scope = Scope()
        scope.record('get', 'response', 'explicit')
        scope.record_response('get', self.RESPONSE)
        assert scope.resolve('get.response') == 'explicit'

    def test_captures_are_per_scope(self):
        """A fresh scope per test is the isolation guarantee."""
        first = Scope()
        first.record('send', 'taskId', 'T1')
        with pytest.raises(UnresolvedVariable):
            Scope().resolve('send.taskId')


class TestReferences:
    def test_finds_every_reference(self):
        assert references(
            {'a': '{{x}}', 'b': ['Bearer {{y}}', {'c': '{{x}}'}]}
        ) == {'x', 'y'}

    def test_ignores_plain_text(self):
        assert references({'a': 'nothing here'}) == set()


class TestCorpusReferences:
    """What the shipped corpus actually asks a runner to supply."""

    def test_every_reference_is_a_capture_a_document_var_or_injected(self, suite):
        """Nothing in the corpus needs `env.` or `$uuid`.

        The two that resolve to neither a capture nor a document variable are
        the runner-injected pair; story 4.6 owns supplying them. If a corpus
        refresh adds a third, this is where it surfaces.
        """
        injected = set()
        for loaded in suite.tests:
            test = loaded.test
            declared = {
                f'{step.id}.{name}'
                for step in test.steps
                for name in (step.capture or {})
            }
            declared |= {f'{step.id}.response' for step in test.steps}
            for reference in references(test.model_dump(exclude_none=True, mode='json')):
                if reference not in declared and reference not in suite.variables:
                    injected.add(reference)
        assert injected == {'insufficientAuthToken', 'otherUserTaskId'}

    def test_every_capture_path_parses(self, suite):
        for loaded in suite.tests:
            for step in loaded.test.steps:
                for name, path in (step.capture or {}).items():
                    assert parse_path(path), f'{loaded.test.id}/{step.id} {name}'

    def test_every_collection_path_parses(self, suite):
        seen = 0
        for loaded in suite.tests:
            test = loaded.test
            named = list(test.assertions or [])
            for step in test.steps:
                named.extend(step.assertions or [])
            for assertion in named:
                for mode in ('any', 'all', 'none'):
                    collection = getattr(assertion, mode)
                    if collection is not None:
                        parse_path(collection.path)
                        seen += 1
        assert seen > 0
