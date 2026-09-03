"""The host-side ACTS pipeline: `scripts/acts_report.py` and its processor.

Both run outside the image, on whatever python a CI runner has, so they are
stdlib-only and tested here rather than through the service. The behaviours
worth pinning are the guard rails: a run that tested nothing, or a FastAPI
error envelope, must never reach the rolling history — either would push a
real entry off the 50-run window and read as a clean night.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from scripts.acts_report import (
    InvalidReport,
    failures,
    format_report,
    is_conformant,
    validate,
)
from scripts.acts_report import main as report_main
from scripts.process_acts_results import (
    DEFAULT_HISTORY_LIMIT,
    build_run,
    compile_tests,
)
from scripts.process_acts_results import main as process_main


def a_report(tests=None, transport='jsonrpc', sdk='a2a-python'):
    tests = tests or [{'id': 'CORE-SEND-001', 'level': 'must', 'result': 'pass'}]
    counts = {'total': 0, 'passed': 0, 'failed': 0, 'skipped': 0, 'errors': 0}
    by_level = {lvl: dict(counts) for lvl in ('must', 'should', 'may')}
    plural = {'pass': 'passed', 'fail': 'failed', 'skip': 'skipped', 'error': 'errors'}
    for t in tests:
        by_level[t['level']]['total'] += 1
        by_level[t['level']][plural[t['result']]] += 1
    return {
        'acts_version': '1.0',
        'spec_version': '1.0',
        'generated_at': '2026-09-03T09:00:00Z',
        'sdk': {'name': sdk, 'version': '1.0', 'language': 'python'},
        'transport': transport,
        'summary': {
            'total': len(tests),
            'passed': sum(t['result'] == 'pass' for t in tests),
            'failed': sum(t['result'] == 'fail' for t in tests),
            'skipped': sum(t['result'] == 'skip' for t in tests),
            'errors': sum(t['result'] == 'error' for t in tests),
            'duration_ms': 100,
            'by_level': by_level,
        },
        'suites': [{'id': 'core', 'name': 'Core', 'tests': tests}],
    }


class TestValidation:
    def test_a_good_report_passes(self):
        assert validate(a_report())['transport'] == 'jsonrpc'

    def test_a_fastapi_error_is_named_as_such(self):
        """"missing key 'summary'" would send the reader hunting."""
        with pytest.raises(InvalidReport, match='service returned an error'):
            validate({'detail': 'the code under test failed to start'})

    def test_a_report_with_no_tests_is_refused(self):
        """Publishing it would push a real entry off the rolling window."""
        empty = a_report()
        empty['summary']['total'] = 0
        with pytest.raises(InvalidReport, match='no tests'):
            validate(empty)

    @pytest.mark.parametrize('missing', ['summary', 'suites', 'sdk', 'transport'])
    def test_a_missing_top_level_key_is_refused(self, missing):
        report = a_report()
        del report[missing]
        with pytest.raises(InvalidReport, match=missing):
            validate(report)

    def test_a_non_object_payload_is_refused(self):
        with pytest.raises(InvalidReport, match='JSON object'):
            validate([1, 2, 3])


class TestConformance:
    def test_all_must_passing(self):
        assert is_conformant(a_report())

    def test_a_failed_must(self):
        report = a_report([{'id': 'A', 'level': 'must', 'result': 'fail'}])
        assert not is_conformant(report)

    def test_an_errored_must(self):
        report = a_report([{'id': 'A', 'level': 'must', 'result': 'error'}])
        assert not is_conformant(report)

    def test_a_failed_should_does_not_block(self):
        """§12.7 makes conformance a statement about `must` only."""
        report = a_report([
            {'id': 'A', 'level': 'must', 'result': 'pass'},
            {'id': 'B', 'level': 'should', 'result': 'fail'},
        ])
        assert is_conformant(report)

    def test_failures_lists_ids_and_messages(self):
        report = a_report([{'id': 'A', 'level': 'must', 'result': 'fail'}])
        report['suites'][0]['tests'][0]['failure'] = {'message': 'boom'}
        assert list(failures(report)) == [('A', 'boom')]


class TestFormatting:
    def test_the_summary_names_the_sdk_and_transport(self):
        text, ok = format_report(a_report(), 'ACTS')
        assert 'a2a-python / jsonrpc' in text
        assert ok

    def test_skips_leave_the_denominator(self):
        report = a_report([
            {'id': 'A', 'level': 'must', 'result': 'pass'},
            {'id': 'B', 'level': 'must', 'result': 'skip'},
        ])
        text, _ = format_report(report, 'ACTS')
        assert 'MUST   1/1 passed (1 skipped)' in text

    def test_failing_tests_are_listed(self):
        report = a_report([{'id': 'A', 'level': 'must', 'result': 'fail'}])
        report['suites'][0]['tests'][0]['failure'] = {'message': 'bad body'}
        text, ok = format_report(report, 'ACTS')
        assert 'A: bad body' in text
        assert not ok


class TestReportExitCodes:
    """The PR path gates on conformance; the nightly path records it."""

    def write(self, tmp_path, report):
        path = tmp_path / 'acts_results.json'
        path.write_text(json.dumps(report))
        return str(path)

    def test_conformant_run_exits_zero(self, tmp_path, capsys):
        path = self.write(tmp_path, a_report())
        assert report_main(['--response-file', path, '--require-conformant']) == 0

    def test_non_conformant_fails_the_pr_path(self, tmp_path, capsys):
        report = a_report([{'id': 'A', 'level': 'must', 'result': 'fail'}])
        path = self.write(tmp_path, report)
        assert report_main(['--response-file', path, '--require-conformant']) == 1

    def test_non_conformant_passes_the_nightly_path(self, tmp_path, capsys):
        """A conformance failure is a metric to record, not a broken run."""
        report = a_report([{'id': 'A', 'level': 'must', 'result': 'fail'}])
        path = self.write(tmp_path, report)
        assert report_main(['--response-file', path]) == 0

    def test_an_error_envelope_always_fails(self, tmp_path, capsys):
        path = self.write(tmp_path, {'detail': 'boom'})
        assert report_main(['--response-file', path]) == 1


class TestHistoryEntry:
    def test_tests_are_flattened_to_a_verdict_each(self):
        assert compile_tests(a_report()) == [
            {'id': 'CORE-SEND-001', 'level': 'must', 'result': 'pass'}
        ]

    def test_failure_detail_is_carried(self):
        """A history that only says "fail" cannot answer the question anyone
        brings to it: is this the same failure as last night?"""
        report = a_report([{'id': 'A', 'level': 'must', 'result': 'fail'}])
        detail = {'message': 'bad body', 'assertion_path': 'body.id',
                  'expected': '1', 'actual': '2'}
        report['suites'][0]['tests'][0]['failure'] = detail
        assert compile_tests(report)[0]['failure'] == detail

    def test_skip_reason_is_carried(self):
        report = a_report([{'id': 'A', 'level': 'must', 'result': 'skip'}])
        report['suites'][0]['tests'][0]['skip_reason'] = 'no webhook endpoint'
        assert compile_tests(report)[0]['skip_reason'] == 'no webhook endpoint'

    def test_a_passing_test_stays_lean(self):
        """The cost of detail falls on the minority that did not pass."""
        assert set(compile_tests(a_report())[0]) == {'id', 'level', 'result'}

    def test_bulk_fields_are_left_in_the_artifact(self):
        """`name` is static per id, `duration_ms` is noise, and `steps` is the
        bulkiest field of all — it lives in the per-run report instead."""
        report = a_report()
        report['suites'][0]['tests'][0].update(
            {'name': 'a long test name', 'duration_ms': 12,
             'steps': [{'id': 's', 'result': 'pass', 'duration_ms': 12}]}
        )
        assert set(compile_tests(report)[0]) == {'id', 'level', 'result'}

    def test_one_run_over_three_bindings_is_ONE_entry(self):
        """A commit tested over three transports is one run of the SDK."""
        entry = build_run([
            a_report(transport='jsonrpc'),
            a_report(transport='grpc'),
            a_report(transport='rest'),
        ])
        assert entry['transports'] == ['grpc', 'jsonrpc', 'rest']
        assert set(entry['results']) == {'jsonrpc', 'grpc', 'rest'}

    def test_an_entry_carries_the_verdict_precomputed(self, monkeypatch):
        monkeypatch.setenv('GITHUB_SHA', 'deadbeef')
        monkeypatch.setenv('GITHUB_RUN_ID', '77')
        entry = build_run([a_report()])
        assert entry['conformant'] is True
        assert entry['commit_sha'] == 'deadbeef'
        assert entry['github_run_id'] == '77'
        assert entry['sdk'] == 'a2a-python'

    def test_conformance_is_the_conjunction_over_bindings(self):
        """Passing on JSON-RPC and failing on gRPC is not conformant."""
        entry = build_run([
            a_report(transport='jsonrpc'),
            a_report([{'id': 'A', 'level': 'must', 'result': 'fail'}], transport='grpc'),
        ])
        assert entry['conformant'] is False
        assert entry['results']['jsonrpc']['conformant'] is True
        assert entry['results']['grpc']['conformant'] is False

    def test_the_top_level_summary_sums_the_bindings(self):
        entry = build_run([
            a_report(transport='jsonrpc'), a_report(transport='grpc')
        ])
        assert entry['summary']['total'] == 2
        assert entry['results']['jsonrpc']['summary']['total'] == 1


class TestProcessing:
    def run(self, tmp_path, reports, history=None, url=None):
        if isinstance(reports, dict):
            reports = [reports]
        args = []
        for i, report in enumerate(reports):
            path = tmp_path / f'acts_results_{i}.json'
            path.write_text(json.dumps(report))
            args += ['--report-file', str(path)]
        out = tmp_path / 'acts_python.json'
        if history is not None:
            asset = tmp_path / 'existing.json'
            asset.write_text(json.dumps(history))
            url = asset.as_uri()
        code = process_main([
            *args,
            '--history_output_file', str(out),
            '--history_url', url or (tmp_path / 'absent.json').as_uri(),
        ])
        return code, json.loads(out.read_text()) if out.exists() else None

    def test_a_missing_asset_starts_a_fresh_history(self, tmp_path, monkeypatch):
        """GitHub 404s an asset that was never uploaded."""
        def not_found(url):
            raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)

        monkeypatch.setattr(
            'scripts.process_acts_results.urllib.request.urlopen', not_found
        )
        code, history = self.run(tmp_path, a_report())
        assert code == 0
        assert len(history) == 1

    def test_any_other_fetch_error_refuses_to_publish(self, tmp_path, monkeypatch):
        """A 503 must not overwrite a real history with an empty list."""
        def unavailable(url):
            raise urllib.error.HTTPError(url, 503, 'Service Unavailable', {}, None)

        monkeypatch.setattr(
            'scripts.process_acts_results.urllib.request.urlopen', unavailable
        )
        with pytest.raises(SystemExit):
            self.run(tmp_path, a_report())

    def test_a_run_is_appended_to_the_existing_history(self, tmp_path):
        code, history = self.run(tmp_path, a_report(), history=[{'timestamp': 'old'}])
        assert code == 0
        assert len(history) == 2
        assert history[0] == {'timestamp': 'old'}

    def test_the_default_window_is_a_week(self):
        """Deliberately shorter than ITK's 50: an ACTS entry covers every
        binding and carries failure detail, so it is far larger."""
        assert DEFAULT_HISTORY_LIMIT == 7

    def test_the_window_prunes_at_the_default(self, tmp_path):
        code, history = self.run(
            tmp_path, a_report(),
            history=[{'timestamp': str(i)} for i in range(DEFAULT_HISTORY_LIMIT + 3)],
        )
        assert code == 0
        assert len(history) == DEFAULT_HISTORY_LIMIT
        assert history[-1]['sdk'] == 'a2a-python'

    def test_the_window_is_pruned(self, tmp_path, monkeypatch):
        monkeypatch.setenv('ACTS_HISTORY_LIMIT', '5')
        code, history = self.run(
            tmp_path, a_report(), history=[{'timestamp': str(i)} for i in range(10)]
        )
        assert code == 0
        assert len(history) == 5
        assert history[-1]['sdk'] == 'a2a-python'

    def test_a_three_transport_run_adds_exactly_one_entry(self, tmp_path):
        code, history = self.run(
            tmp_path,
            [a_report(transport=t) for t in ('jsonrpc', 'grpc', 'rest')],
            history=[{'timestamp': 'last night'}],
        )
        assert code == 0
        assert len(history) == 2
        assert history[-1]['transports'] == ['grpc', 'jsonrpc', 'rest']

    def test_an_empty_report_is_refused(self, tmp_path):
        report = a_report()
        report['suites'] = []
        with pytest.raises(SystemExit):
            self.run(tmp_path, report)

    def test_two_reports_for_one_transport_are_refused(self, tmp_path):
        """A duplicated --report-file would silently drop one of them."""
        with pytest.raises(SystemExit):
            self.run(tmp_path, [a_report(transport='grpc')] * 2)

    def test_both_sdks_can_share_one_asset(self, tmp_path):
        """Nothing keys the history by SDK, so a shared asset stays coherent."""
        first = build_run([a_report(sdk='a2a-python')])
        code, history = self.run(tmp_path, a_report(sdk='a2a-js'), history=[first])
        assert code == 0
        assert [e['sdk'] for e in history] == ['a2a-python', 'a2a-js']
