"""Loading ACTS documents and flattening a manifest.

The checks that matter here are the cross-file ones — a duplicate test id
between two suite files, an include reaching outside the corpus, a file that
loads but silently contributes nothing — because no amount of single-file
validation can see them, and each corrupts a conformance report quietly
rather than loudly.
"""

from __future__ import annotations

import textwrap

import pytest

from test_suite.acts.loader import (
    ActsFileError,
    load_document,
    load_suite,
    parse_document,
)
from test_suite.acts.schema import Level, TransportBinding


HEADER = 'acts_version: "1.0"\nspec_version: "1.0"\n'


def _suites_block(suite_id='core', test_id='CORE-001'):
    """A `suites:` block at zero indentation, with one trivial test."""
    return textwrap.dedent(f"""\
        suites:
          - id: {suite_id}
            name: {suite_id.title()}
            tests:
              - id: {test_id}
                name: A test
                level: must
                steps:
                  - id: send
                    operation: send_message
                    params:
                      message:
                        role: ROLE_USER
                        parts:
                          - text: hello
                    expect:
                      status: 200
        """)


def _suite_file(suite_id='core', test_id='CORE-001', extra=''):
    return HEADER + extra + _suites_block(suite_id, test_id)


def _include_block(*includes):
    return 'include:\n' + ''.join(f'  - {name}\n' for name in includes)


def _manifest(*includes, extra=''):
    return HEADER + extra + _include_block(*includes)


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content, encoding='utf-8')
    return p


class TestLoadDocument:
    def test_loads_a_suite_file(self, tmp_path):
        p = _write(tmp_path, 'core.acts.yaml', _suite_file())
        doc = load_document(p)
        assert doc.suites[0].tests[0].id == 'CORE-001'

    def test_missing_file_names_the_path(self, tmp_path):
        with pytest.raises(ActsFileError, match='ACTS file not found'):
            load_document(tmp_path / 'nope.acts.yaml')

    def test_malformed_yaml_is_reported_as_a_file_error(self, tmp_path):
        p = _write(tmp_path, 'bad.acts.yaml', 'acts_version: "1.0"\n  bad: indent\n')
        with pytest.raises(ActsFileError, match='could not parse'):
            load_document(p)

    def test_empty_file_is_rejected(self, tmp_path):
        p = _write(tmp_path, 'empty.acts.yaml', '')
        with pytest.raises(ActsFileError, match='file is empty'):
            load_document(p)

    def test_validation_failure_names_the_file(self, tmp_path):
        p = _write(tmp_path, 'core.acts.yaml', HEADER + 'suites: []\n')
        with pytest.raises(ActsFileError, match='core.acts.yaml'):
            load_document(p)


class TestParseDocument:
    def test_accepts_an_already_parsed_mapping(self):
        """A document delivered over HTTP must take the same path as one
        read off disk."""
        doc = parse_document({
            'acts_version': '1.0',
            'spec_version': '1.0',
            'suites': [{
                'id': 'core', 'name': 'Core',
                'tests': [{
                    'id': 'T1', 'name': 'n', 'level': 'may',
                    'steps': [{'id': 's', 'operation': 'get_agent_card', 'params': {}}],
                }],
            }],
        })
        assert doc.suites[0].tests[0].level is Level.MAY

    def test_a_list_is_rejected(self):
        with pytest.raises(ActsFileError, match='expected a mapping'):
            parse_document([{'acts_version': '1.0'}])


class TestManifestFlattening:
    def test_manifest_pulls_in_its_includes(self, tmp_path):
        _write(tmp_path, 'a.acts.yaml', _suite_file('alpha', 'A-001'))
        _write(tmp_path, 'b.acts.yaml', _suite_file('beta', 'B-001'))
        m = _write(tmp_path, 'suite.acts.yaml',
                   _manifest('a.acts.yaml', 'b.acts.yaml'))

        loaded = load_suite(m)
        assert [t.id for t in loaded] == ['A-001', 'B-001']
        assert loaded.suite_ids() == ['alpha', 'beta']

    def test_include_order_is_preserved(self, tmp_path):
        """Report ordering follows load order, so it has to be stable."""
        for n in 'cab':
            _write(tmp_path, f'{n}.acts.yaml', _suite_file(n, f'{n.upper()}-001'))
        m = _write(tmp_path, 'suite.acts.yaml',
                   _manifest('c.acts.yaml', 'a.acts.yaml', 'b.acts.yaml'))

        assert [t.id for t in load_suite(m)] == ['C-001', 'A-001', 'B-001']

    def test_nested_includes_are_followed(self, tmp_path):
        _write(tmp_path, 'leaf.acts.yaml', _suite_file('leaf', 'L-001'))
        _write(tmp_path, 'mid.acts.yaml',
               HEADER + _include_block('leaf.acts.yaml') + _suites_block('mid', 'M-001'))
        m = _write(tmp_path, 'suite.acts.yaml', _manifest('mid.acts.yaml'))

        assert sorted(t.id for t in load_suite(m)) == ['L-001', 'M-001']

    def test_a_plain_suite_file_loads_without_includes(self, tmp_path):
        p = _write(tmp_path, 'core.acts.yaml', _suite_file())
        assert len(load_suite(p)) == 1

    def test_a_file_included_twice_yields_its_tests_once(self, tmp_path):
        """Two suites may legitimately share a fixture file; its tests must
        not be counted twice."""
        _write(tmp_path, 'leaf.acts.yaml', _suite_file('leaf', 'L-001'))
        _write(tmp_path, 'one.acts.yaml',
               HEADER + _include_block('leaf.acts.yaml') + _suites_block('one', 'O-001'))
        _write(tmp_path, 'two.acts.yaml',
               HEADER + _include_block('leaf.acts.yaml') + _suites_block('two', 'T-001'))
        m = _write(tmp_path, 'suite.acts.yaml',
                   _manifest('one.acts.yaml', 'two.acts.yaml'))

        loaded = load_suite(m)
        assert sorted(t.id for t in loaded) == ['L-001', 'O-001', 'T-001']

    def test_variables_merge_with_later_files_winning(self, tmp_path):
        _write(tmp_path, 'a.acts.yaml',
               _suite_file('alpha', 'A-001', extra='variables:\n  webhookUrl: "https://a"\n'))
        m = _write(tmp_path, 'suite.acts.yaml',
                   _manifest('a.acts.yaml',
                             extra='variables:\n  baseUrl: "{{SUT}}"\n  webhookUrl: "https://manifest"\n'))

        loaded = load_suite(m)
        assert loaded.variables == {'baseUrl': '{{SUT}}', 'webhookUrl': 'https://a'}

    def test_every_source_is_recorded(self, tmp_path):
        _write(tmp_path, 'a.acts.yaml', _suite_file('alpha', 'A-001'))
        m = _write(tmp_path, 'suite.acts.yaml', _manifest('a.acts.yaml'))

        loaded = load_suite(m)
        assert [p.name for p in loaded.sources] == ['suite.acts.yaml', 'a.acts.yaml']


class TestIncludeErrors:
    def test_an_include_cycle_terminates_with_each_file_loaded_once(self, tmp_path):
        """Mutual includes are legitimate — either file loads the pair — so
        the walk visits each once rather than rejecting the arrangement."""
        _write(tmp_path, 'a.acts.yaml',
               HEADER + _include_block('b.acts.yaml') + _suites_block('alpha', 'A-001'))
        _write(tmp_path, 'b.acts.yaml',
               HEADER + _include_block('a.acts.yaml') + _suites_block('beta', 'B-001'))
        m = _write(tmp_path, 'suite.acts.yaml', _manifest('a.acts.yaml'))

        loaded = load_suite(m)
        assert sorted(t.id for t in loaded) == ['A-001', 'B-001']

    def test_missing_include_names_the_entry(self, tmp_path):
        m = _write(tmp_path, 'suite.acts.yaml', _manifest('ghost.acts.yaml'))
        with pytest.raises(ActsFileError, match='included file not found: ghost.acts.yaml'):
            load_suite(m)

    def test_include_escaping_the_corpus_is_rejected(self, tmp_path):
        """An include is a filename within the corpus, not a path into the
        filesystem; one that reaches out does not survive a fresh checkout."""
        outside = tmp_path / 'outside'
        outside.mkdir()
        _write(outside, 'evil.acts.yaml', _suite_file('evil', 'E-001'))
        corpus = tmp_path / 'corpus'
        corpus.mkdir()
        m = _write(corpus, 'suite.acts.yaml', _manifest('../outside/evil.acts.yaml'))

        with pytest.raises(ActsFileError, match='escapes the suite directory'):
            load_suite(m)

    def test_non_list_include_is_rejected(self, tmp_path):
        m = _write(tmp_path, 'suite.acts.yaml', HEADER + 'include: discovery.acts.yaml\n')
        with pytest.raises(ActsFileError, match='`include` must be a list'):
            load_suite(m)


class TestCrossFileUniqueness:
    def test_duplicate_test_id_across_files_is_rejected(self, tmp_path):
        """Report rows are keyed by test id, so a duplicate silently
        overwrites a result rather than failing anything."""
        _write(tmp_path, 'a.acts.yaml', _suite_file('alpha', 'SHARED-001'))
        _write(tmp_path, 'b.acts.yaml', _suite_file('beta', 'SHARED-001'))
        m = _write(tmp_path, 'suite.acts.yaml',
                   _manifest('a.acts.yaml', 'b.acts.yaml'))

        with pytest.raises(ActsFileError, match='duplicate test id'):
            load_suite(m)

    def test_duplicate_suite_id_across_files_is_rejected(self, tmp_path):
        _write(tmp_path, 'a.acts.yaml', _suite_file('core', 'A-001'))
        _write(tmp_path, 'b.acts.yaml', _suite_file('core', 'B-001'))
        m = _write(tmp_path, 'suite.acts.yaml',
                   _manifest('a.acts.yaml', 'b.acts.yaml'))

        with pytest.raises(ActsFileError, match='duplicate suite id'):
            load_suite(m)


class TestStrictness:
    """`strict` decides whether one bad test stops the load.

    Default on, so a mistake in a file we author is loud. Off for the pinned
    upstream corpus, where a refresh should report every new defect at once
    instead of one per run-fix-rerun cycle.
    """

    BAD_TEST = HEADER + textwrap.dedent("""
        suites:
          - id: core
            name: Core
            tests:
              - id: GOOD-001
                name: Fine
                level: must
                steps:
                  - id: s
                    operation: get_agent_card
                    params: {}
              - id: BAD-001
                name: Broken
                level: must
                steps:
                  - id: s
                    operation: get_task
                    params: {id: x}
                    expect_error: {error_type: NoSuchErrorName}
        """)

    def test_strict_raises_on_an_invalid_test(self, tmp_path):
        p = _write(tmp_path, 'core.acts.yaml', self.BAD_TEST)
        with pytest.raises(ActsFileError, match='unknown error_type'):
            load_suite(p)

    def test_non_strict_keeps_the_good_tests_and_reports_the_bad(self, tmp_path):
        p = _write(tmp_path, 'core.acts.yaml', self.BAD_TEST)
        loaded = load_suite(p, strict=False)

        assert [t.id for t in loaded] == ['GOOD-001']
        assert len(loaded.errors) == 1
        assert 'BAD-001' in str(loaded.errors[0])
        assert 'unknown error_type' in str(loaded.errors[0])

    def test_non_strict_reports_every_bad_test_not_just_the_first(self, tmp_path):
        """One run-fix-rerun cycle per defect is what makes a refresh
        painful; report them all at once."""
        # Appended at the indentation of BAD_TEST's own test entries.
        second = (
            '      - id: BAD-002\n'
            '        name: Also broken\n'
            '        level: must\n'
            '        steps:\n'
            '          - id: s\n'
            '            operation: not_an_operation\n'
            '            params: {}\n'
        )
        p = _write(tmp_path, 'core.acts.yaml', self.BAD_TEST + second)
        loaded = load_suite(p, strict=False)

        assert [t.id for t in loaded] == ['GOOD-001']
        assert len(loaded.errors) == 2
        assert 'BAD-001' in str(loaded.errors[0])
        assert 'BAD-002' in str(loaded.errors[1])

    def test_non_strict_still_raises_on_a_broken_envelope(self, tmp_path):
        """A bad envelope is not a per-test problem, and dropping a whole
        file quietly is the exact failure this loader exists to prevent."""
        p = _write(tmp_path, 'core.acts.yaml', 'spec_version: "1.0"\nsuites: []\n')
        with pytest.raises(ActsFileError):
            load_suite(p, strict=False)


class TestLoadedSuiteQueries:
    @pytest.fixture
    def loaded(self, tmp_path):
        content = HEADER + textwrap.dedent("""
            suites:
              - id: core
                name: Core
                tests:
                  - id: T-MUST
                    name: must, all transports
                    level: must
                    requires_behaviors: [tck-complete-task]
                    steps:
                      - id: s
                        operation: send_message
                        params: {}
                  - id: T-SHOULD
                    name: should, jsonrpc only
                    level: should
                    transport: [jsonrpc]
                    requires_behaviors: [tck-multi-turn, tck-complete-task]
                    steps:
                      - id: s
                        operation: send_message
                        params: {}
            """)
        return load_suite(_write(tmp_path, 'core.acts.yaml', content))

    def test_len_and_iteration(self, loaded):
        assert len(loaded) == 2
        assert [t.id for t in loaded] == ['T-MUST', 'T-SHOULD']

    def test_by_id(self, loaded):
        assert loaded.by_id('T-MUST').test.name == 'must, all transports'
        assert loaded.by_id('nope') is None

    def test_by_level(self, loaded):
        assert [t.id for t in loaded.by_level(Level.MUST)] == ['T-MUST']
        assert loaded.by_level(Level.MAY) == []

    def test_for_transport_includes_unrestricted_tests(self, loaded):
        assert [t.id for t in loaded.for_transport(TransportBinding.JSONRPC)] == [
            'T-MUST', 'T-SHOULD',
        ]
        assert [t.id for t in loaded.for_transport(TransportBinding.GRPC)] == ['T-MUST']

    def test_required_behaviors_is_the_union(self, loaded):
        """The set an SDK's acts/sut-behaviors.yaml gets checked against."""
        assert loaded.required_behaviors() == frozenset({
            'tck-complete-task', 'tck-multi-turn',
        })

    def test_provenance_is_attached(self, loaded):
        entry = loaded.by_id('T-MUST')
        assert entry.suite_id == 'core'
        assert entry.source.name == 'core.acts.yaml'
        assert 'T-MUST' in str(entry) and 'core.acts.yaml' in str(entry)


class TestCompatFlag:
    """`compat` decides whether the known upstream defects are rewritten.

    The rules themselves are tested in `test_acts_compat.py`; what matters
    here is that every entry point honours the flag, and that a document
    needing a rewrite is genuinely invalid without one.
    """

    DEFECTIVE = HEADER + textwrap.dedent("""\
        suites:
          - id: core
            name: Core
            tests:
              - id: CORE-001
                name: A test
                level: must
                steps:
                  - id: get
                    operation: get_task
                    expect_error:
                      code: TaskNotFoundError
        """)

    def test_load_document_rewrites_by_default(self, tmp_path):
        p = _write(tmp_path, 'core.acts.yaml', self.DEFECTIVE)
        doc = load_document(p)
        step = doc.suites[0].tests[0].steps[0]
        assert step.expect_error.error_type == 'TaskNotFoundError'

    def test_load_document_without_compat_rejects_it(self, tmp_path):
        p = _write(tmp_path, 'core.acts.yaml', self.DEFECTIVE)
        with pytest.raises(ActsFileError, match='expect_error.code'):
            load_document(p, compat=False)

    def test_parse_document_honours_the_flag(self):
        import yaml

        raw = yaml.safe_load(self.DEFECTIVE)
        assert parse_document(raw).suites[0].tests[0].steps[0].expect_error
        with pytest.raises(ActsFileError, match='expect_error.code'):
            parse_document(raw, compat=False)

    def test_load_suite_records_what_it_rewrote(self, tmp_path):
        p = _write(tmp_path, 'suite.acts.yaml', self.DEFECTIVE)
        loaded = load_suite(p)
        assert len(loaded) == 1
        assert [r.where for r in loaded.rewrites] == ['CORE-001.get']

    def test_load_suite_without_compat_records_nothing(self, tmp_path):
        p = _write(tmp_path, 'suite.acts.yaml', self.DEFECTIVE)
        loaded = load_suite(p, strict=False, compat=False)
        assert loaded.rewrites == []
        assert len(loaded) == 0
        assert len(loaded.errors) == 1

    def test_a_clean_suite_records_no_rewrites(self, tmp_path):
        p = _write(tmp_path, 'suite.acts.yaml', _suite_file())
        assert load_suite(p).rewrites == []
