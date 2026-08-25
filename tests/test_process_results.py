"""Nightly metrics compilation, and the silent drop it used to do.

``process_results.py`` recovered each scenario's definition by matching the
result name back against the local ``scenarios.json``, and ``continue``d past
anything it couldn't find. A renamed scenario, a scenario generated from the
shared set, or a run driven by a file the processor wasn't looking at, would
therefore disappear from the published history while the job still went
green. These tests pin the fix: metadata rides on the result, and an
unrecordable result fails the run instead of vanishing.
"""

from __future__ import annotations

import pytest

from scripts.process_results import (
    build_record,
    is_self_describing,
    load_scenarios,
)


def _result(**overrides):
    base = {
        'passed': True,
        'sdks': ['current', 'go_v10'],
        'edges': ['0->1', '1->0'],
        'protocols': ['jsonrpc'],
        'behavior': 'send_message',
        'streaming': False,
    }
    base.update(overrides)
    return base


class TestIsSelfDescribing:
    def test_result_with_metadata(self):
        assert is_self_describing(_result()) is True

    def test_legacy_result_without_metadata(self):
        assert is_self_describing(
            {'passed': True, 'sdks': ['current'], 'edges': None}
        ) is False

    def test_explicit_nulls_do_not_count(self):
        assert is_self_describing(
            {'passed': True, 'protocols': None, 'behavior': None}
        ) is False

    def test_bare_bool(self):
        assert is_self_describing(True) is False


class TestBuildRecord:
    def test_prefers_result_metadata_over_the_scenario_file(self):
        """The result describes what actually ran; the file describes what was
        requested. When they disagree, the run is the truth."""
        record = build_record(
            'x',
            _result(protocols=['grpc'], behavior='resubscribe'),
            {'name': 'x', 'protocols': ['jsonrpc'], 'behavior': 'send_message'},
        )
        assert record['protocols'] == ['grpc']
        assert record['behavior'] == 'resubscribe'

    def test_falls_back_to_the_scenario_file(self):
        record = build_record(
            'x',
            {'passed': True, 'sdks': ['current', 'go_v10'], 'edges': None},
            {'name': 'x', 'protocols': ['jsonrpc'], 'behavior': 'send_message',
             'edges': ['0->1', '1->0']},
        )
        assert record['protocols'] == ['jsonrpc']
        assert record['behavior'] == 'send_message'
        assert record['edges'] == ['0->1', '1->0']

    def test_works_with_no_scenario_file_at_all(self):
        """The case that used to be impossible: a scenario the local file has
        never heard of, recorded correctly anyway."""
        record = build_record('generated-scenario-42', _result(), None)
        assert record['name'] == 'generated-scenario-42'
        assert record['behavior'] == 'send_message'
        assert record['protocols'] == ['jsonrpc']
        assert record['passed'] is True

    def test_record_shape_is_unchanged(self):
        """The dashboard ingests this; altering its shape is a stated
        non-goal."""
        record = build_record('x', _result(), {'name': 'x', 'traversal': 'euler'})
        assert {
            'name', 'sdks', 'edges', 'protocols', 'behavior', 'traversal',
            'passed',
        } <= set(record)

    def test_traversal_defaults_to_euler(self):
        assert build_record('x', _result(), None)['traversal'] == 'euler'

    def test_subtest_sdks_come_from_the_result(self):
        """A subtest runs a smaller graph than its parent declares."""
        record = build_record(
            'parent-sub-current-go_v10',
            _result(sdks=['current', 'go_v10']),
            {'name': 'parent', 'sdks': ['current', 'go_v10', 'python_v10']},
        )
        assert record['sdks'] == ['current', 'go_v10']

    def test_failing_result_is_recorded_as_failing(self):
        assert build_record('x', _result(passed=False), None)['passed'] is False


class TestLoadScenarios:
    def test_missing_file_is_fatal_when_required(self, tmp_path):
        with pytest.raises(SystemExit):
            load_scenarios(str(tmp_path / 'nope.json'), required=True)

    def test_missing_file_is_tolerated_when_not_required(self, tmp_path):
        """A run driven by the shared set has no local scenarios.json."""
        assert load_scenarios(str(tmp_path / 'nope.json'), required=False) == []
