"""scripts/itk_report.py — response validation and result summarising.

The behaviour under test is the one the per-SDK ``run_itk.sh`` scripts got
wrong: three of the five treated the per-scenario result *object* as a
boolean, so a failing scenario still printed PASSED. The first test class
below pins that.
"""

from __future__ import annotations

import json

import pytest

from scripts.itk_report import (
    InvalidResponse,
    format_report,
    main,
    scenario_passed,
    validate,
)


def _response(results, all_passed=None):
    if all_passed is None:
        all_passed = all(r['passed'] for r in results.values())
    return {'results': results, 'all_passed': all_passed}


# ---------------------------------------------------------------------------
# Reading one scenario's outcome
# ---------------------------------------------------------------------------


class TestScenarioPassed:
    def test_failing_result_object_is_not_passed(self):
        """The bug: a non-empty dict is truthy, so `if value` said PASSED."""
        value = {'passed': False, 'sdks': ['current', 'go_v10'], 'edges': None}
        assert bool(value) is True  # what the old scripts actually tested
        assert scenario_passed(value) is False

    def test_passing_result_object(self):
        assert scenario_passed({'passed': True, 'sdks': []}) is True

    @pytest.mark.parametrize('value', [None, 'yes', 0, [], {}, True, False])
    def test_unusable_values_are_failures(self, value):
        """Only a result *object* with ``passed: true`` counts; a bare bool —
        the pre-schema shape — is no longer trusted."""
        assert scenario_passed(value) is False


# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------


class TestValidate:
    def test_accepts_a_well_formed_response(self):
        data = _response({'a': {'passed': True, 'sdks': []}})
        assert validate(data) is data

    def test_rejects_non_object(self):
        with pytest.raises(InvalidResponse, match='not a JSON object'):
            validate([1, 2, 3])

    def test_rejects_fastapi_error_envelope(self):
        with pytest.raises(InvalidResponse, match='service returned an error'):
            validate({'detail': 'unknown agent id'})

    def test_rejects_missing_results(self):
        with pytest.raises(InvalidResponse, match='missing "results"'):
            validate({'all_passed': True})

    def test_rejects_non_object_results(self):
        with pytest.raises(InvalidResponse, match='"results" is not an object'):
            validate({'results': []})


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestFormatReport:
    def test_marks_each_scenario_individually(self):
        data = _response({
            'passing one': {'passed': True, 'sdks': []},
            'failing one': {'passed': False, 'sdks': []},
        })
        report, all_passed = format_report(data, 'ITK TEST RESULTS')
        assert 'passing one: PASSED' in report
        assert 'failing one: FAILED' in report
        assert all_passed is False
        assert 'OVERALL STATUS: FAILED' in report

    def test_all_passing(self):
        data = _response({'a': {'passed': True, 'sdks': []}})
        report, all_passed = format_report(data, 'T')
        assert all_passed is True
        assert 'OVERALL STATUS: PASSED' in report

    def test_title_is_used(self):
        data = _response({'a': {'passed': True, 'sdks': []}})
        report, _ = format_report(data, 'NIGHTLY ITK SUMMARY')
        assert 'NIGHTLY ITK SUMMARY:' in report

    def test_empty_result_set_is_a_failure(self):
        """A run that executed nothing did not pass, whatever the flag says."""
        report, all_passed = format_report(
            {'results': {}, 'all_passed': True}, 'T'
        )
        assert all_passed is False
        assert 'OVERALL STATUS: FAILED' in report

    def test_trusts_the_services_verdict_over_recomputing(self):
        """all_passed comes from the response, so the printed verdict matches
        what the service concluded even if the two ever disagree."""
        data = _response(
            {'a': {'passed': True, 'sdks': []}}, all_passed=False
        )
        _, all_passed = format_report(data, 'T')
        assert all_passed is False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestMain:
    def _write(self, tmp_path, payload):
        p = tmp_path / 'raw_results.json'
        p.write_text(
            payload if isinstance(payload, str) else json.dumps(payload),
            encoding='utf-8',
        )
        return p

    def test_exit_zero_when_all_passed(self, tmp_path, capsys):
        path = self._write(tmp_path, _response({'a': {'passed': True, 'sdks': []}}))
        code = main(['--response-file', str(path), '--require-all-passed'])
        assert code == 0
        assert 'a: PASSED' in capsys.readouterr().out

    def test_exit_one_on_failure_when_required(self, tmp_path, capsys):
        path = self._write(tmp_path, _response({'a': {'passed': False, 'sdks': []}}))
        code = main(['--response-file', str(path), '--require-all-passed'])
        assert code == 1
        assert 'a: FAILED' in capsys.readouterr().out

    def test_nightly_path_reports_but_does_not_fail(self, tmp_path, capsys):
        """Without --require-all-passed, process_results.py owns the exit code,
        so scenario failures are recorded as metrics rather than breaking the
        run."""
        path = self._write(tmp_path, _response({'a': {'passed': False, 'sdks': []}}))
        code = main(['--response-file', str(path)])
        assert code == 0
        assert 'a: FAILED' in capsys.readouterr().out

    def test_malformed_json_exits_one(self, tmp_path, capsys):
        path = self._write(tmp_path, 'not json at all')
        code = main(['--response-file', str(path)])
        assert code == 1
        assert 'could not parse' in capsys.readouterr().err

    def test_error_envelope_exits_one(self, tmp_path, capsys):
        path = self._write(tmp_path, {'detail': 'unknown agent id "nope_v10"'})
        code = main(['--response-file', str(path), '--require-all-passed'])
        assert code == 1
        err = capsys.readouterr().err
        assert 'service returned an error' in err
        assert 'nope_v10' in err

    def test_reads_stdin(self, tmp_path, capsys, monkeypatch):
        import io

        payload = json.dumps(_response({'a': {'passed': True, 'sdks': []}}))
        monkeypatch.setattr('sys.stdin', io.StringIO(payload))
        assert main(['--response-file', '-']) == 0
        assert 'a: PASSED' in capsys.readouterr().out
