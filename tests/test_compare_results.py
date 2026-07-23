"""Unit tests for scripts.compare_results (comparison harness + classifier).

The oracle is ``scenario.expected_pass``, NOT the baked baseline. This test
suite locks that invariant in — the classifier must catch a regression even
when OLD and NEW agree.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from scripts import compare_results as cr


# -----------------------------------------------------------------------------
# Outcome / Result — construction & sanity
# -----------------------------------------------------------------------------


class TestOutcomeConstruction:
    def test_pass_outcome(self):
        o = cr.Outcome(passed=True)
        assert o.passed is True
        assert o.error is None
        assert o.transient is False

    def test_transient_error_outcome(self):
        o = cr.Outcome(error='connection refused', transient=True)
        assert o.passed is None
        assert o.error == 'connection refused'
        assert o.transient is True

    def test_deterministic_error_outcome(self):
        o = cr.Outcome(error='AssertionError: expected 200', transient=False)
        assert o.transient is False
        assert o.error is not None


# -----------------------------------------------------------------------------
# Scenario loader — normalizes `expected: pass|fail` -> expected_pass: bool
# -----------------------------------------------------------------------------


class TestScenarioLoader:
    def test_normalizes_expected_pass(self, tmp_path: pathlib.Path):
        p = tmp_path / 's.json'
        p.write_text(
            json.dumps(
                {
                    'tests': [
                        {'name': 'a', 'sdks': ['x'], 'behavior': 'send_message', 'expected': 'pass'},
                        {'name': 'b', 'sdks': ['x'], 'behavior': 'send_message', 'expected': 'fail'},
                    ]
                }
            )
        )
        scenarios = cr.load_scenarios(p)
        assert scenarios['a'].expected_pass is True
        assert scenarios['b'].expected_pass is False

    def test_defaults_expected_to_pass_when_missing(self, tmp_path: pathlib.Path):
        # Today's per-SDK scenarios*.json have no `expected` field. The
        # legacy assumption is: they exist because they should pass. Loader
        # defaults expected_pass=True and records a warning-worthy flag so
        # migration can surface these later.
        p = tmp_path / 's.json'
        p.write_text(json.dumps({'tests': [{'name': 'a', 'sdks': ['x'], 'behavior': 'send_message'}]}))
        scenarios = cr.load_scenarios(p)
        assert scenarios['a'].expected_pass is True

    def test_rejects_unknown_expected_value(self, tmp_path: pathlib.Path):
        p = tmp_path / 's.json'
        p.write_text(json.dumps({'tests': [{'name': 'a', 'sdks': ['x'], 'behavior': 'send_message', 'expected': 'maybe'}]}))
        with pytest.raises(ValueError, match='expected'):
            cr.load_scenarios(p)


# -----------------------------------------------------------------------------
# evaluate() — the absolute-oracle classifier
# -----------------------------------------------------------------------------


def _scn(name='s', expected_pass=True):
    return cr.Scenario(name=name, expected_pass=expected_pass)


class TestEvaluateAbsoluteOracle:
    """The absolute oracle catches a regression BOTH paths share.

    If OLD and NEW both fail a scenario whose expected_pass=True, the
    classifier must return REAL_FAILURE, not MATCH. The baked baseline is
    NOT trusted; the scenario's expected value is.
    """

    def test_new_pass_expected_pass_match(self):
        r = cr.evaluate(_scn(expected_pass=True), cr.Outcome(passed=True), cr.Outcome(passed=True))
        assert r is cr.Result.MATCH

    def test_new_fail_expected_pass_is_real_failure_even_when_old_also_fails(self):
        # The important case: agreement of two wrongs is still wrong.
        r = cr.evaluate(_scn(expected_pass=True), cr.Outcome(passed=False), cr.Outcome(passed=False))
        assert r is cr.Result.REAL_FAILURE

    def test_new_fail_expected_pass_is_real_failure_when_old_passes(self):
        r = cr.evaluate(_scn(expected_pass=True), cr.Outcome(passed=True), cr.Outcome(passed=False))
        assert r is cr.Result.REAL_FAILURE

    def test_new_pass_expected_fail_is_real_failure(self):
        # Scenario is designed to fail; NEW passed — that's a regression.
        r = cr.evaluate(_scn(expected_pass=False), cr.Outcome(passed=False), cr.Outcome(passed=True))
        assert r is cr.Result.REAL_FAILURE

    def test_new_fail_expected_fail_match(self):
        r = cr.evaluate(_scn(expected_pass=False), cr.Outcome(passed=False), cr.Outcome(passed=False))
        assert r is cr.Result.MATCH


class TestEvaluateErrorHandling:
    """Deterministic NEW error is NEVER laundered to infra.

    Retry lives in the runner; by the time evaluate() sees an Outcome, retry
    is exhausted. A non-transient error at that point is a REAL_FAILURE.
    """

    def test_new_transient_error_is_infra_failure(self):
        r = cr.evaluate(
            _scn(),
            old=cr.Outcome(passed=True),
            new=cr.Outcome(error='connection refused', transient=True),
        )
        assert r is cr.Result.INFRA_FAILURE

    def test_new_deterministic_error_is_real_failure(self):
        # Non-transient error surviving retry ⇒ REAL_FAILURE.
        r = cr.evaluate(
            _scn(),
            old=cr.Outcome(passed=True),
            new=cr.Outcome(error='AssertionError: mismatch', transient=False),
        )
        assert r is cr.Result.REAL_FAILURE

    def test_old_transient_error_but_new_correct_is_infra(self):
        # OLD errored transiently; NEW satisfies the oracle. We can't
        # cross-check the baseline, but we don't fail the run for that —
        # tag as infra so the runner retries.
        r = cr.evaluate(
            _scn(expected_pass=True),
            old=cr.Outcome(error='timeout', transient=True),
            new=cr.Outcome(passed=True),
        )
        assert r is cr.Result.INFRA_FAILURE

    def test_old_deterministic_error_but_new_correct_is_behavioral_divergence(self):
        # OLD (the baked baseline) crashed non-transiently; NEW satisfied
        # the oracle. That's a case for human adjudication — the baseline
        # is broken, but the SDK is fine.
        r = cr.evaluate(
            _scn(expected_pass=True),
            old=cr.Outcome(error='panic: nil pointer', transient=False),
            new=cr.Outcome(passed=True),
        )
        assert r is cr.Result.BEHAVIORAL_DIVERGENCE


class TestEvaluateStaleBaseline:
    """NEW correct, OLD disagrees ⇒ BEHAVIORAL_DIVERGENCE.

    The baked baseline drifted from itk/main. This is exactly the state the
    consolidation is trying to end; a human accepts it and it becomes a
    recorded delta.
    """

    def test_new_pass_old_fail_expected_pass(self):
        r = cr.evaluate(
            _scn(expected_pass=True),
            old=cr.Outcome(passed=False),
            new=cr.Outcome(passed=True),
        )
        assert r is cr.Result.BEHAVIORAL_DIVERGENCE

    def test_new_fail_old_pass_expected_fail(self):
        r = cr.evaluate(
            _scn(expected_pass=False),
            old=cr.Outcome(passed=True),
            new=cr.Outcome(passed=False),
        )
        assert r is cr.Result.BEHAVIORAL_DIVERGENCE


# -----------------------------------------------------------------------------
# accepted_deltas.json — suppresses already-adjudicated behavioral divergence
# -----------------------------------------------------------------------------


class TestAcceptedDeltas:
    def _write(self, path: pathlib.Path, entries: list[dict]) -> None:
        path.write_text(json.dumps({'deltas': entries}))

    def test_missing_file_returns_empty_set(self, tmp_path: pathlib.Path):
        # A fresh repo has no accepted deltas yet — must not crash.
        deltas = cr.load_accepted_deltas(tmp_path / 'nope.json')
        assert deltas == set()

    def test_loader_returns_scenario_keys(self, tmp_path: pathlib.Path):
        p = tmp_path / 'accepted.json'
        self._write(
            p,
            [
                {
                    'sdk': 'python',
                    'line': 'v03',
                    'scenario': 'No Backwards Compatibility - GRPC',
                    'expected': 'pass',
                    'old_passed': True,
                    'new_passed': True,
                    'reason': 'baseline stale',
                    'adjudicated_by': 'jakubworek',
                    'adjudicated_at': '2026-07-23T00:00:00Z',
                }
            ],
        )
        keys = cr.load_accepted_deltas(p)
        assert ('python', 'v03', 'No Backwards Compatibility - GRPC') in keys

    def test_classify_run_suppresses_accepted(self, tmp_path: pathlib.Path):
        accepted_file = tmp_path / 'accepted.json'
        self._write(
            accepted_file,
            [
                {
                    'sdk': 'python',
                    'line': 'v10',
                    'scenario': 's-drifted',
                    'expected': 'pass',
                    'old_passed': False,
                    'new_passed': True,
                    'reason': 'stale',
                    'adjudicated_by': 'a',
                    'adjudicated_at': '2026-07-23T00:00:00Z',
                }
            ],
        )
        scenarios = {
            's-drifted': _scn('s-drifted', expected_pass=True),
            's-fresh': _scn('s-fresh', expected_pass=True),
        }
        old = {'s-drifted': cr.Outcome(passed=False), 's-fresh': cr.Outcome(passed=True)}
        new = {'s-drifted': cr.Outcome(passed=True), 's-fresh': cr.Outcome(passed=True)}

        report = cr.classify_run(
            sdk='python',
            line='v10',
            scenarios=scenarios,
            old=old,
            new=new,
            accepted_deltas=cr.load_accepted_deltas(accepted_file),
        )
        # Suppressed → does not appear as a divergence; counted separately.
        assert report.behavioral_divergences == []
        assert report.suppressed_count == 1
        assert report.matches == ['s-drifted', 's-fresh'] or set(report.matches) == {'s-drifted', 's-fresh'}


# -----------------------------------------------------------------------------
# classify_run — aggregate report
# -----------------------------------------------------------------------------


class TestClassifyRun:
    def test_clean_run_all_matches(self):
        scenarios = {'a': _scn('a', True), 'b': _scn('b', True)}
        old = {'a': cr.Outcome(passed=True), 'b': cr.Outcome(passed=True)}
        new = {'a': cr.Outcome(passed=True), 'b': cr.Outcome(passed=True)}
        report = cr.classify_run(
            sdk='python', line='v10', scenarios=scenarios, old=old, new=new, accepted_deltas=set()
        )
        assert report.is_clean is True
        assert report.real_failures == []
        assert report.infra_failures == []
        assert report.behavioral_divergences == []
        assert set(report.matches) == {'a', 'b'}

    def test_real_failure_marks_run_dirty(self):
        scenarios = {'a': _scn('a', True)}
        old = {'a': cr.Outcome(passed=True)}
        new = {'a': cr.Outcome(passed=False)}
        report = cr.classify_run(
            sdk='python', line='v10', scenarios=scenarios, old=old, new=new, accepted_deltas=set()
        )
        assert report.is_clean is False
        assert report.real_failures == ['a']

    def test_infra_failure_marks_run_dirty(self):
        # Cutover gate requires N clean days with ZERO infra_failure.
        scenarios = {'a': _scn('a', True)}
        old = {'a': cr.Outcome(passed=True)}
        new = {'a': cr.Outcome(error='timeout', transient=True)}
        report = cr.classify_run(
            sdk='python', line='v10', scenarios=scenarios, old=old, new=new, accepted_deltas=set()
        )
        assert report.is_clean is False
        assert report.infra_failures == ['a']

    def test_unaccepted_divergence_marks_run_dirty(self):
        scenarios = {'a': _scn('a', True)}
        old = {'a': cr.Outcome(passed=False)}
        new = {'a': cr.Outcome(passed=True)}
        report = cr.classify_run(
            sdk='python', line='v10', scenarios=scenarios, old=old, new=new, accepted_deltas=set()
        )
        assert report.is_clean is False
        assert report.behavioral_divergences == ['a']

    def test_missing_new_outcome_is_infra_failure(self):
        # If the NEW path didn't produce an Outcome at all (runner error),
        # treat as infra (retryable at the runner level upstream).
        scenarios = {'a': _scn('a', True)}
        old = {'a': cr.Outcome(passed=True)}
        new: dict[str, cr.Outcome] = {}
        report = cr.classify_run(
            sdk='python', line='v10', scenarios=scenarios, old=old, new=new, accepted_deltas=set()
        )
        assert report.infra_failures == ['a']


# -----------------------------------------------------------------------------
# Cutover streak (N=7 clean days per SDK; JSON persisted).
# -----------------------------------------------------------------------------


class TestCutoverStreak:
    def test_clean_day_extends_streak(self, tmp_path: pathlib.Path):
        streak_file = tmp_path / 'streak.json'
        cr.record_run(streak_file, sdk='python', run_date='2026-07-20', clean=True)
        cr.record_run(streak_file, sdk='python', run_date='2026-07-21', clean=True)
        assert cr.current_streak_days(streak_file, sdk='python') == 2

    def test_dirty_day_resets_streak(self, tmp_path: pathlib.Path):
        streak_file = tmp_path / 'streak.json'
        cr.record_run(streak_file, sdk='python', run_date='2026-07-20', clean=True)
        cr.record_run(streak_file, sdk='python', run_date='2026-07-21', clean=False)
        cr.record_run(streak_file, sdk='python', run_date='2026-07-22', clean=True)
        assert cr.current_streak_days(streak_file, sdk='python') == 1

    def test_streak_is_per_sdk(self, tmp_path: pathlib.Path):
        streak_file = tmp_path / 'streak.json'
        cr.record_run(streak_file, sdk='python', run_date='2026-07-20', clean=True)
        cr.record_run(streak_file, sdk='go', run_date='2026-07-20', clean=False)
        assert cr.current_streak_days(streak_file, sdk='python') == 1
        assert cr.current_streak_days(streak_file, sdk='go') == 0

    def test_cutover_gate_requires_seven_days(self, tmp_path: pathlib.Path):
        streak_file = tmp_path / 'streak.json'
        for day in range(20, 26):  # 6 days
            cr.record_run(streak_file, sdk='python', run_date=f'2026-07-{day:02d}', clean=True)
        assert cr.cutover_gate_passes(streak_file, sdk='python', required_days=7) is False

        cr.record_run(streak_file, sdk='python', run_date='2026-07-26', clean=True)
        assert cr.cutover_gate_passes(streak_file, sdk='python', required_days=7) is True

    def test_idempotent_same_day_record(self, tmp_path: pathlib.Path):
        # Nightly might record twice on the same date (retry, manual re-run);
        # the second record for the same (sdk, date) should overwrite, not
        # double-count.
        streak_file = tmp_path / 'streak.json'
        cr.record_run(streak_file, sdk='python', run_date='2026-07-20', clean=True)
        cr.record_run(streak_file, sdk='python', run_date='2026-07-20', clean=False)
        assert cr.current_streak_days(streak_file, sdk='python') == 0

    def test_streak_file_survives_json_round_trip(self, tmp_path: pathlib.Path):
        streak_file = tmp_path / 'streak.json'
        cr.record_run(streak_file, sdk='python', run_date='2026-07-20', clean=True)
        raw = json.loads(streak_file.read_text())
        assert 'python' in raw
        # Must be a stable, human-readable format (this file lives in a
        # GitHub Release asset — humans inspect it).
        assert isinstance(raw['python'], list)


# -----------------------------------------------------------------------------
# Runner integration — classify a raw_results.json pair (OLD vs NEW)
# -----------------------------------------------------------------------------


class TestRawResultsAdapter:
    """Adapter that maps ``raw_results.json`` (existing itk_service.py output)
    to a dict[name -> Outcome] the classifier consumes. Keeps the classifier
    schema-agnostic and pins the interop point to one place.
    """

    def test_maps_passed_true_to_passing_outcome(self):
        raw = {'all_passed': True, 'results': {'a': {'passed': True, 'sdks': ['x'], 'edges': None}}}
        outcomes = cr.raw_to_outcomes(raw)
        assert outcomes['a'].passed is True
        assert outcomes['a'].error is None

    def test_maps_passed_false_to_failing_outcome(self):
        raw = {'all_passed': False, 'results': {'a': {'passed': False, 'sdks': ['x']}}}
        outcomes = cr.raw_to_outcomes(raw)
        assert outcomes['a'].passed is False

    def test_maps_bool_shorthand(self):
        # process_results.py already handles the `isinstance(details, bool)`
        # legacy shape; the classifier adapter must too, to avoid a schema
        # divergence between the two consumers.
        raw = {'all_passed': True, 'results': {'a': True, 'b': False}}
        outcomes = cr.raw_to_outcomes(raw)
        assert outcomes['a'].passed is True
        assert outcomes['b'].passed is False


# -----------------------------------------------------------------------------
# CLI — smoke test the `--sdk python` entry point
# -----------------------------------------------------------------------------


class TestCLI:
    def _write_scenarios(self, path: pathlib.Path, names: list[str]) -> None:
        path.write_text(
            json.dumps(
                {
                    'tests': [
                        {'name': n, 'sdks': ['current'], 'behavior': 'send_message', 'expected': 'pass'}
                        for n in names
                    ]
                }
            )
        )

    def _write_raw(self, path: pathlib.Path, outcomes: dict[str, bool]) -> None:
        path.write_text(
            json.dumps(
                {
                    'all_passed': all(outcomes.values()),
                    'results': {n: {'passed': p, 'sdks': ['current']} for n, p in outcomes.items()},
                }
            )
        )

    def test_cli_exits_zero_on_clean_run(self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture):
        scenarios = tmp_path / 'scenarios.json'
        old = tmp_path / 'old.json'
        new = tmp_path / 'new.json'
        streak = tmp_path / 'streak.json'
        accepted = tmp_path / 'accepted.json'

        self._write_scenarios(scenarios, ['a'])
        self._write_raw(old, {'a': True})
        self._write_raw(new, {'a': True})

        rc = cr.main(
            [
                '--sdk', 'python',
                '--line', 'v10',
                '--scenarios', str(scenarios),
                '--old', str(old),
                '--new', str(new),
                '--streak-file', str(streak),
                '--accepted-deltas', str(accepted),
                '--run-date', '2026-07-23',
            ]
        )
        assert rc == 0

    def test_cli_exits_nonzero_on_real_failure(self, tmp_path: pathlib.Path):
        scenarios = tmp_path / 'scenarios.json'
        old = tmp_path / 'old.json'
        new = tmp_path / 'new.json'
        streak = tmp_path / 'streak.json'

        self._write_scenarios(scenarios, ['a'])
        self._write_raw(old, {'a': True})
        self._write_raw(new, {'a': False})  # regression!

        rc = cr.main(
            [
                '--sdk', 'python',
                '--line', 'v10',
                '--scenarios', str(scenarios),
                '--old', str(old),
                '--new', str(new),
                '--streak-file', str(streak),
                '--run-date', '2026-07-23',
            ]
        )
        assert rc != 0
