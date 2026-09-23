"""The §13 report document, and the SUT behaviour contract it reports against.

The report format is frozen: dashboards and certification portals read it, and
§13 makes it a MUST. So these tests are mostly shape assertions — they exist to
fail when a field is renamed or dropped, which is the change most likely to be
made casually and most expensive downstream.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from test_suite.acts import behaviors, report
from test_suite.acts.loader import LoadedSuite, LoadedTest
from test_suite.acts.runner import FailureDetail, Outcome, StepResult, TestResult
from test_suite.acts.schema import Level, Test, TransportBinding


WHEN = datetime(2026, 9, 3, 8, 42, 0, tzinfo=timezone.utc)
SDK = {'name': 'a2a-python', 'version': '1.0', 'language': 'python'}


def a_test(test_id: str, level: Level = Level.MUST) -> Test:
    return Test(
        id=test_id,
        name=f'test {test_id}',
        level=level,
        steps=[{'id': 's', 'operation': 'get_task', 'params': {'id': 'x'}}],
    )


def a_suite(*tests: Test, suite_id: str = 'core') -> LoadedSuite:
    return LoadedSuite(
        tests=[
            LoadedTest(
                test=t, suite_id=suite_id, suite_name='Core',
                source=Path('core.acts.yaml'),
            )
            for t in tests
        ]
    )


def a_result(
    test_id: str,
    outcome: Outcome = Outcome.PASS,
    level: Level = Level.MUST,
    **kwargs,
) -> TestResult:
    return TestResult(
        id=test_id, name=f'test {test_id}', level=level,
        result=outcome, duration_ms=5, **kwargs,
    )


class TestSummary:
    def test_counts_every_outcome(self):
        results = [
            a_result('A'),
            a_result('B', Outcome.FAIL),
            a_result('C', Outcome.SKIP),
            a_result('D', Outcome.ERROR),
        ]
        summary = report.summarize(results, duration_ms=100)
        assert summary['total'] == 4
        assert (summary['passed'], summary['failed']) == (1, 1)
        assert (summary['skipped'], summary['errors']) == (1, 1)
        assert summary['duration_ms'] == 100

    def test_errors_are_not_folded_into_failures(self):
        """The runner's fail/error split has to survive into the report."""
        summary = report.summarize([a_result('A', Outcome.ERROR)], 1)
        assert summary['failed'] == 0
        assert summary['errors'] == 1

    def test_every_level_is_present_even_when_empty(self):
        summary = report.summarize([a_result('A')], 1)
        assert set(summary['by_level']) == {'must', 'should', 'may'}
        assert summary['by_level']['may']['total'] == 0

    def test_levels_partition_the_results(self):
        results = [
            a_result('A', level=Level.MUST),
            a_result('B', level=Level.SHOULD),
            a_result('C', level=Level.MAY),
        ]
        summary = report.summarize(results, 1)
        assert sum(c['total'] for c in summary['by_level'].values()) == 3


class TestDocument:
    def build(self, results, suite=None):
        return report.build(
            results,
            suite or a_suite(*[a_test(r.id) for r in results]),
            sdk=SDK,
            transport=TransportBinding.JSONRPC,
            duration_ms=42,
            generated_at=WHEN,
            env={'runner': 'test'},
        )

    def test_top_level_keys_match_the_spec(self):
        doc = self.build([a_result('A')])
        assert set(doc) == {
            'acts_version', 'spec_version', 'generated_at', 'sdk',
            'transport', 'environment', 'summary', 'suites',
        }

    def test_timestamp_is_iso_8601_zulu(self):
        assert self.build([a_result('A')])['generated_at'] == '2026-09-03T08:42:00Z'

    def test_sdk_info_needs_its_required_fields(self):
        with pytest.raises(ValueError, match='language'):
            report.build(
                [], a_suite(), sdk={'name': 'x', 'version': '1'},
                transport=TransportBinding.REST, duration_ms=0,
            )

    def test_results_are_grouped_under_their_suites(self):
        doc = self.build([a_result('A'), a_result('B')])
        assert [s['id'] for s in doc['suites']] == ['core']
        assert [t['id'] for t in doc['suites'][0]['tests']] == ['A', 'B']

    def test_suite_order_follows_the_corpus_not_the_results(self):
        """A report whose suites move between runs makes every diff unreadable."""
        suite = a_suite(a_test('A'), a_test('B'))
        doc = self.build([a_result('B'), a_result('A')], suite)
        assert [t['id'] for t in doc['suites'][0]['tests']] == ['A', 'B']

    def test_a_result_with_no_suite_is_surfaced_not_dropped(self):
        doc = self.build([a_result('GHOST')], a_suite(a_test('A')))
        assert [s['id'] for s in doc['suites']] == ['(unknown)']

    def test_optional_test_fields_are_omitted_when_unset(self):
        doc = self.build([a_result('A')])
        assert set(doc['suites'][0]['tests'][0]) == {
            'id', 'name', 'level', 'result', 'duration_ms'
        }

    def test_a_skip_carries_its_reason(self):
        doc = self.build([a_result('A', Outcome.SKIP, skip_reason='no webhook')])
        assert doc['suites'][0]['tests'][0]['skip_reason'] == 'no webhook'

    def test_a_failure_carries_its_detail(self):
        failure = FailureDetail(
            message='boom', step_id='s', expected='1', actual='2',
            assertion_path='body.id',
        )
        doc = self.build([a_result('A', Outcome.FAIL, failure=failure)])
        assert doc['suites'][0]['tests'][0]['failure'] == {
            'message': 'boom', 'step_id': 's', 'expected': '1',
            'actual': '2', 'assertion_path': 'body.id',
        }

    def test_steps_are_included_when_present(self):
        steps = (StepResult(id='s1', result=Outcome.PASS, duration_ms=3),)
        doc = self.build([a_result('A', steps=steps)])
        assert doc['suites'][0]['tests'][0]['steps'] == [
            {'id': 's1', 'result': 'pass', 'duration_ms': 3}
        ]

    def test_the_whole_document_is_json_serializable(self):
        """It is written to a file and POSTed over HTTP; nothing exotic."""
        doc = self.build([a_result('A', Outcome.FAIL, failure=FailureDetail('x'))])
        assert json.loads(json.dumps(doc)) == doc


class TestFilenameAndWriting:
    def test_naming_follows_the_convention(self):
        name = report.filename('a2a-python', TransportBinding.JSONRPC, WHEN)
        assert name == 'acts-report-a2a-python-jsonrpc-20260903T084200Z.json'

    def test_write_derives_the_name_from_the_report(self, tmp_path):
        """So a file cannot claim to be a run it is not."""
        doc = report.build(
            [a_result('A')], a_suite(a_test('A')), sdk=SDK,
            transport=TransportBinding.GRPC, duration_ms=1, generated_at=WHEN,
        )
        path = report.write(doc, tmp_path / 'out')
        assert path.name.startswith('acts-report-a2a-python-grpc-')
        assert json.loads(path.read_text()) == doc

    def test_write_creates_the_directory(self, tmp_path):
        doc = report.build(
            [], a_suite(), sdk=SDK, transport=TransportBinding.REST,
            duration_ms=0, generated_at=WHEN,
        )
        assert report.write(doc, tmp_path / 'a' / 'b').is_file()


class TestConformance:
    def build(self, results):
        return report.build(
            results, a_suite(*[a_test(r.id, r.level) for r in results]),
            sdk=SDK, transport=TransportBinding.JSONRPC, duration_ms=1,
            generated_at=WHEN,
        )

    def test_all_must_passing_is_conformant(self):
        assert report.is_conformant(self.build([a_result('A')]))

    def test_a_failed_must_is_not(self):
        assert not report.is_conformant(self.build([a_result('A', Outcome.FAIL)]))

    def test_an_errored_must_is_not_either(self):
        assert not report.is_conformant(self.build([a_result('A', Outcome.ERROR)]))

    def test_a_failed_should_still_is(self):
        """§12.7: conformance is a statement about `must` only."""
        doc = self.build([a_result('A', Outcome.FAIL, level=Level.SHOULD)])
        assert report.is_conformant(doc)

    def test_a_skipped_must_does_not_block(self):
        assert report.is_conformant(self.build([a_result('A', Outcome.SKIP)]))

    def test_summary_lines_exclude_skips_from_the_denominator(self):
        doc = self.build([a_result('A'), a_result('B', Outcome.SKIP)])
        assert report.conformance_lines(doc)[0].startswith('MUST   1/1 passed')
        assert '1 skipped' in report.conformance_lines(doc)[0]

    def test_failures_lists_both_fails_and_errors(self):
        doc = self.build([
            a_result('A', Outcome.FAIL, failure=FailureDetail('bad body')),
            a_result('B', Outcome.ERROR, failure=FailureDetail('refused')),
            a_result('C'),
        ])
        assert dict(report.failures(doc)) == {'A': 'bad body', 'B': 'refused'}


class TestBehaviorContract:
    CONTRACT = """
acts_version: "1.0"
behaviors:
  - prefix: "tck-complete-task"
    description: "Complete the task"
    response_type: task
    terminal_state: TASK_STATE_COMPLETED
  - prefix: "tck-cancel"
    description: "Stay working until canceled"
"""

    def test_parses_and_exposes_its_prefixes(self, tmp_path):
        path = tmp_path / 'sut-behaviors.yaml'
        path.write_text(self.CONTRACT)
        assert behaviors.load(path).prefixes() == {'tck-complete-task', 'tck-cancel'}

    def test_declared_by_reads_the_conventional_path(self, tmp_path):
        (tmp_path / 'acts').mkdir()
        (tmp_path / behaviors.CONTRACT_PATH).write_text(self.CONTRACT)
        assert behaviors.declared_by(tmp_path) == {'tck-complete-task', 'tck-cancel'}

    def test_no_contract_is_none_not_an_empty_set(self):
        """They mean opposite things: no file disables gating, an empty file
        fails every behaviour test."""
        assert behaviors.declared_by(Path('/nonexistent')) is None

    def test_a_malformed_contract_is_rejected(self, tmp_path):
        path = tmp_path / 'sut-behaviors.yaml'
        path.write_text('acts_version: "1.0"\n')  # no behaviors
        with pytest.raises(behaviors.BehaviorsFileError):
            behaviors.load(path)

    def test_unknown_keys_on_a_behavior_are_tolerated(self, tmp_path):
        """§11.1 will grow fields; a runner should not reject a newer file."""
        path = tmp_path / 'sut-behaviors.yaml'
        path.write_text(
            'acts_version: "1.0"\nbehaviors:\n'
            '  - prefix: "tck-x"\n    description: "d"\n    future_key: 1\n'
        )
        assert behaviors.load(path).prefixes() == {'tck-x'}


class TestAgainstTheRealContract:
    """The python SDK's own contract file, if this checkout has one beside it."""

    CONTRACT = (
        Path(__file__).resolve().parent.parent.parent
        / 'a2a-python' / behaviors.CONTRACT_PATH
    )

    @pytest.mark.skipif(
        not CONTRACT.is_file(), reason='no sibling a2a-python checkout'
    )
    def test_it_parses(self):
        declared = behaviors.load(self.CONTRACT).prefixes()
        assert 'tck-complete-task' in declared

    @pytest.mark.skipif(
        not CONTRACT.is_file(), reason='no sibling a2a-python checkout'
    )
    def test_it_covers_what_the_corpus_needs(self):
        from test_suite.acts import load_suite

        corpus = (
            Path(__file__).resolve().parent.parent
            / 'scenarios' / 'acts' / 'suite.acts.yaml'
        )
        needed = load_suite(corpus).required_behaviors()
        assert not needed - behaviors.load(self.CONTRACT).prefixes()
