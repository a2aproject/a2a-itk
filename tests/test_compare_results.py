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

    def test_load_real_failure_deltas_defaults_off(self, tmp_path: pathlib.Path):
        """Entries without ``suppresses_real_failure`` don't leak into the
        real-failure set (backwards compat)."""
        p = tmp_path / 'accepted.json'
        self._write(p, [
            {'sdk': 'python', 'line': 'v10', 'scenario': 's-a',
             'expected': 'pass', 'old_passed': False, 'new_passed': True,
             'reason': 'stale', 'adjudicated_by': 'a',
             'adjudicated_at': '2026-07-23T00:00:00Z'},
        ])
        assert cr.load_accepted_real_failures(p) == set()
        # But divergence-suppression still picks it up:
        assert cr.load_accepted_deltas(p) == {('python', 'v10', 's-a')}

    def test_load_real_failure_deltas_opt_in(self, tmp_path: pathlib.Path):
        """`suppresses_real_failure: true` opts an entry into the real-failure set."""
        p = tmp_path / 'accepted.json'
        self._write(p, [
            {'sdk': 'rust', 'line': 'v10',
             'scenario': 'Nightly - HTTP_JSON - Resubscribe',
             'expected': 'pass', 'old_passed': False, 'new_passed': False,
             'reason': 'shared java resub bug — tracking upstream',
             'adjudicated_by': 'jakubworek',
             'adjudicated_at': '2026-08-11T00:00:00Z',
             'suppresses_real_failure': True},
        ])
        key = ('rust', 'v10', 'Nightly - HTTP_JSON - Resubscribe')
        assert cr.load_accepted_real_failures(p) == {key}
        # Divergence set still contains it too (an entry can suppress both).
        assert cr.load_accepted_deltas(p) == {key}

    def test_classify_run_suppresses_real_failure_when_opted_in(
        self, tmp_path: pathlib.Path,
    ):
        """A ``suppresses_real_failure: true`` entry demotes REAL_FAILURE to
        suppressed-match (with a bumped suppressed_count)."""
        p = tmp_path / 'accepted.json'
        self._write(p, [
            {'sdk': 'rust', 'line': 'v10', 'scenario': 's-shared-bug',
             'expected': 'pass', 'old_passed': False, 'new_passed': False,
             'reason': 'tracked at #123', 'adjudicated_by': 'a',
             'adjudicated_at': '2026-08-11T00:00:00Z',
             'suppresses_real_failure': True},
        ])
        scenarios = {'s-shared-bug': _scn('s-shared-bug', expected_pass=True)}
        # NEW fails oracle → REAL_FAILURE before suppression.
        old = {'s-shared-bug': cr.Outcome(passed=False)}
        new = {'s-shared-bug': cr.Outcome(passed=False)}

        report = cr.classify_run(
            sdk='rust', line='v10', scenarios=scenarios, old=old, new=new,
            accepted_deltas=cr.load_accepted_deltas(p),
            accepted_real_failures=cr.load_accepted_real_failures(p),
        )
        assert report.real_failures == []
        assert report.suppressed_count == 1
        assert 's-shared-bug' in report.matches
        assert report.is_clean is True

    def test_classify_run_does_not_suppress_real_failure_without_opt_in(
        self, tmp_path: pathlib.Path,
    ):
        """A plain accepted-delta entry (no ``suppresses_real_failure``)
        must NOT hide REAL_FAILUREs — historical semantic preserved."""
        p = tmp_path / 'accepted.json'
        self._write(p, [
            {'sdk': 'rust', 'line': 'v10', 'scenario': 's-shared-bug',
             'expected': 'pass', 'old_passed': False, 'new_passed': True,
             'reason': 'stale', 'adjudicated_by': 'a',
             'adjudicated_at': '2026-08-11T00:00:00Z'},  # no suppresses_real_failure
        ])
        scenarios = {'s-shared-bug': _scn('s-shared-bug', expected_pass=True)}
        old = {'s-shared-bug': cr.Outcome(passed=False)}
        new = {'s-shared-bug': cr.Outcome(passed=False)}  # NEW fails → REAL_FAILURE

        report = cr.classify_run(
            sdk='rust', line='v10', scenarios=scenarios, old=old, new=new,
            accepted_deltas=cr.load_accepted_deltas(p),
            accepted_real_failures=cr.load_accepted_real_failures(p),
        )
        assert report.real_failures == ['s-shared-bug']
        assert report.suppressed_count == 0
        assert report.is_clean is False


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
# Sub-test expansion — scenarios with ``build_subtests=True`` emit multiple
# result keys of the form ``<name>-sub-<sdks>`` (testlib.py:719). The
# classifier MUST evaluate every such key against the parent scenario's
# oracle, not silently drop them.
# -----------------------------------------------------------------------------


class TestSubTestExpansion:
    def test_sub_test_keys_are_matched_to_parent_scenario(self):
        # Scenario "foo" (build_subtests=True) produced no base "foo" outcome,
        # only sub-test keys. Each must be evaluated against foo's oracle.
        scenarios = {'foo': _scn('foo', expected_pass=True)}
        old = {
            'foo-sub-python-go': cr.Outcome(passed=True),
            'foo-sub-python-java': cr.Outcome(passed=True),
        }
        new = {
            'foo-sub-python-go': cr.Outcome(passed=True),
            'foo-sub-python-java': cr.Outcome(passed=True),
        }
        report = cr.classify_run(
            sdk='python', line='v10', scenarios=scenarios, old=old, new=new, accepted_deltas=set()
        )
        assert report.is_clean is True
        assert set(report.matches) == {'foo-sub-python-go', 'foo-sub-python-java'}
        assert report.infra_failures == []

    def test_sub_test_failure_marks_run_dirty(self):
        # If ANY sub-test fails, the run is dirty — matches how
        # itk_service.py aggregates: `all(res_dict.values())`.
        scenarios = {'foo': _scn('foo', expected_pass=True)}
        old = {
            'foo-sub-python-go': cr.Outcome(passed=True),
            'foo-sub-python-java': cr.Outcome(passed=True),
        }
        new = {
            'foo-sub-python-go': cr.Outcome(passed=True),
            'foo-sub-python-java': cr.Outcome(passed=False),
        }
        report = cr.classify_run(
            sdk='python', line='v10', scenarios=scenarios, old=old, new=new, accepted_deltas=set()
        )
        assert report.is_clean is False
        assert report.real_failures == ['foo-sub-python-java']

    def test_base_and_sub_test_keys_are_both_evaluated(self):
        # testlib.py:716-719: when the sub-graph == full sdks, the outcome
        # uses the bare ``label``; otherwise it's ``<label>-sub-…``. Both can
        # co-exist for a single scenario across a run.
        scenarios = {'foo': _scn('foo', expected_pass=True)}
        old = {
            'foo': cr.Outcome(passed=True),
            'foo-sub-python-go': cr.Outcome(passed=True),
        }
        new = {
            'foo': cr.Outcome(passed=True),
            'foo-sub-python-go': cr.Outcome(passed=True),
        }
        report = cr.classify_run(
            sdk='python', line='v10', scenarios=scenarios, old=old, new=new, accepted_deltas=set()
        )
        assert set(report.matches) == {'foo', 'foo-sub-python-go'}

    def test_accepted_delta_on_parent_suppresses_sub_test_divergence(self):
        # Adjudication is at parent-scenario granularity — a
        # (sdk, line, parent) entry suppresses any sub-test's divergence.
        scenarios = {'foo': _scn('foo', expected_pass=True)}
        old = {'foo-sub-python-go': cr.Outcome(passed=False)}
        new = {'foo-sub-python-go': cr.Outcome(passed=True)}
        accepted = {('python', 'v10', 'foo')}
        report = cr.classify_run(
            sdk='python', line='v10', scenarios=scenarios, old=old, new=new, accepted_deltas=accepted
        )
        assert report.behavioral_divergences == []
        assert report.suppressed_count == 1


# -----------------------------------------------------------------------------
# Orphan result keys — matches ``process_results.py``'s existing warning
# pattern ("No matching base scenario found for result key: %s"). Also
# surfaces new-vs-old count mismatch: whichever side has extra keys, its
# orphans are recorded so the drift is not silent.
# -----------------------------------------------------------------------------


class TestOrphanResultKeys:
    def test_orphan_key_in_new_is_recorded_and_warned(self, caplog):
        scenarios = {'known': _scn('known', True)}
        old = {'known': cr.Outcome(passed=True)}
        new = {'known': cr.Outcome(passed=True), 'ghost': cr.Outcome(passed=True)}
        with caplog.at_level('WARNING'):
            report = cr.classify_run(
                sdk='python', line='v10', scenarios=scenarios, old=old, new=new, accepted_deltas=set()
            )
        assert report.orphan_result_keys['new'] == ['ghost']
        assert any('ghost' in rec.message for rec in caplog.records)

    def test_orphan_key_in_old_is_recorded_and_warned(self, caplog):
        # NEW has one extra scenario removed since the OLD run; OLD's key is
        # orphaned in the current scenario map. We still flag it.
        scenarios = {'known': _scn('known', True)}
        old = {'known': cr.Outcome(passed=True), 'stale': cr.Outcome(passed=True)}
        new = {'known': cr.Outcome(passed=True)}
        with caplog.at_level('WARNING'):
            report = cr.classify_run(
                sdk='python', line='v10', scenarios=scenarios, old=old, new=new, accepted_deltas=set()
            )
        assert report.orphan_result_keys['old'] == ['stale']
        assert any('stale' in rec.message for rec in caplog.records)

    def test_orphans_do_not_affect_is_clean(self):
        # Orphans are observational — they don't reset the streak on their
        # own (that would be too aggressive; they're likely renames/drift).
        scenarios = {'known': _scn('known', True)}
        old = {'known': cr.Outcome(passed=True)}
        new = {'known': cr.Outcome(passed=True), 'ghost': cr.Outcome(passed=True)}
        report = cr.classify_run(
            sdk='python', line='v10', scenarios=scenarios, old=old, new=new, accepted_deltas=set()
        )
        assert report.is_clean is True

    def test_sub_test_keys_are_not_orphans(self):
        # A ``<name>-sub-…`` key whose parent IS a known scenario must not
        # be flagged as an orphan.
        scenarios = {'foo': _scn('foo', True)}
        old = {'foo-sub-python-go': cr.Outcome(passed=True)}
        new = {'foo-sub-python-go': cr.Outcome(passed=True)}
        report = cr.classify_run(
            sdk='python', line='v10', scenarios=scenarios, old=old, new=new, accepted_deltas=set()
        )
        assert report.orphan_result_keys == {'new': [], 'old': []}


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
                '--accepted-deltas', str(accepted),
            ]
        )
        assert rc == 0

    def test_cli_exits_nonzero_on_real_failure(self, tmp_path: pathlib.Path):
        scenarios = tmp_path / 'scenarios.json'
        old = tmp_path / 'old.json'
        new = tmp_path / 'new.json'

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
            ]
        )
        assert rc != 0

    def test_cli_report_file_contains_full_report(self, tmp_path: pathlib.Path):
        scenarios = tmp_path / 'scenarios.json'
        old = tmp_path / 'old.json'
        new = tmp_path / 'new.json'
        report_file = tmp_path / 'report.json'

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
                '--report-file', str(report_file),
            ]
        )
        assert rc == 0
        report = json.loads(report_file.read_text())
        assert report['is_clean'] is True
        assert report['matches'] == ['a']
        assert report['real_failures'] == []
        assert report['orphan_result_keys'] == {'new': [], 'old': []}


# -----------------------------------------------------------------------------
# Self-comparison plumbing check: any raw_results.json fed as both --old and
# --new MUST report clean iff every outcome matches its oracle. Validates
# the end-to-end wiring (loader → adapter → classifier → report) without
# needing a separate NEW path — running the OLD path against itself must
# always report "identical".
# -----------------------------------------------------------------------------


class TestRollingHistory:
    """Rolling-history mode: --history_url + --history_output_file.

    Mirrors the pattern process_results.py uses so shadow classification
    history publishes through the same GHA release asset lifecycle as the
    nightly metrics it lives alongside.
    """

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

    def _run_with_history(
        self,
        tmp_path: pathlib.Path,
        history_url: str | None,
        history_output_file: pathlib.Path,
        *,
        monkeypatch,
        env: dict[str, str] | None = None,
    ) -> int:
        scenarios = tmp_path / 'scenarios.json'
        old = tmp_path / 'old.json'
        new = tmp_path / 'new.json'
        self._write_scenarios(scenarios, ['a', 'b'])
        self._write_raw(old, {'a': True, 'b': True})
        self._write_raw(new, {'a': True, 'b': True})

        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)

        argv = [
            '--sdk', 'ts',
            '--line', 'v10',
            '--scenarios', str(scenarios),
            '--old', str(old),
            '--new', str(new),
            '--history_output_file', str(history_output_file),
        ]
        if history_url is not None:
            argv += ['--history_url', history_url]
        return cr.main(argv)

    def test_new_history_file_when_no_url(self, tmp_path, monkeypatch):
        """Without --history_url, a fresh history is started."""
        out = tmp_path / 'shadow.json'
        rc = self._run_with_history(
            tmp_path, history_url=None, history_output_file=out,
            monkeypatch=monkeypatch,
        )
        assert rc == 0
        history = json.loads(out.read_text())
        assert isinstance(history, list) and len(history) == 1
        assert history[0]['is_clean'] is True
        assert set(history[0]) >= {
            'timestamp', 'commit_sha', 'github_run_id', 'a2a_itk_sha',
            'sdk', 'line', 'is_clean', 'matches', 'real_failures',
            'infra_failures', 'behavioral_divergences',
            'suppressed_count', 'orphan_result_keys',
        }

    def test_metadata_from_env(self, tmp_path, monkeypatch):
        """commit_sha / github_run_id / a2a_itk_sha are picked up from env,
        matching process_results.py's convention for GHA."""
        out = tmp_path / 'shadow.json'
        self._run_with_history(
            tmp_path, history_url=None, history_output_file=out,
            monkeypatch=monkeypatch,
            env={
                'GITHUB_SHA': 'aaaaaaaaaaaa',
                'GITHUB_RUN_ID': '12345',
                'A2A_ITK_SHA': 'bbbbbbbbbbbb',
            },
        )
        record = json.loads(out.read_text())[0]
        assert record['commit_sha'] == 'aaaaaaaaaaaa'
        assert record['github_run_id'] == '12345'
        assert record['a2a_itk_sha'] == 'bbbbbbbbbbbb'

    def test_default_metadata_when_env_absent(self, tmp_path, monkeypatch):
        """When env vars aren't set (local dev), the record still has
        legible placeholder values — not None or crash."""
        out = tmp_path / 'shadow.json'
        # Nuke the env vars in case they leak from the host / other tests.
        for k in ('GITHUB_SHA', 'GITHUB_RUN_ID', 'A2A_ITK_SHA'):
            monkeypatch.delenv(k, raising=False)
        self._run_with_history(
            tmp_path, history_url=None, history_output_file=out,
            monkeypatch=monkeypatch,
        )
        record = json.loads(out.read_text())[0]
        assert record['commit_sha'] == 'local-dev'
        assert record['github_run_id'] == '0'
        assert record['a2a_itk_sha'] == 'unset'

    def test_timestamp_is_iso8601_utc(self, tmp_path, monkeypatch):
        import datetime
        out = tmp_path / 'shadow.json'
        self._run_with_history(
            tmp_path, history_url=None, history_output_file=out,
            monkeypatch=monkeypatch,
        )
        ts = json.loads(out.read_text())[0]['timestamp']
        # Parsable as ISO-8601 with tz — round-trips cleanly.
        parsed = datetime.datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None

    def test_appends_to_fetched_history(self, tmp_path, monkeypatch):
        """--history_url pulls existing entries; new run is appended."""
        # Pre-existing entries in the "fetched" release asset.
        prior = [
            {'timestamp': '2026-08-01T00:00:00+00:00', 'sdk': 'ts', 'line': 'v10',
             'is_clean': True, 'commit_sha': 'old1', 'github_run_id': '1',
             'a2a_itk_sha': 'aitk1', 'matches': [], 'real_failures': [],
             'infra_failures': [], 'behavioral_divergences': [],
             'suppressed_count': 0,
             'orphan_result_keys': {'new': [], 'old': []}},
            {'timestamp': '2026-08-02T00:00:00+00:00', 'sdk': 'ts', 'line': 'v10',
             'is_clean': True, 'commit_sha': 'old2', 'github_run_id': '2',
             'a2a_itk_sha': 'aitk2', 'matches': [], 'real_failures': [],
             'infra_failures': [], 'behavioral_divergences': [],
             'suppressed_count': 0,
             'orphan_result_keys': {'new': [], 'old': []}},
        ]

        # Stub the URL fetch (no network in tests).
        monkeypatch.setattr(cr, 'fetch_existing_history', lambda _url: prior[:])

        out = tmp_path / 'shadow.json'
        self._run_with_history(
            tmp_path, history_url='https://example.com/shadow_ts.json',
            history_output_file=out, monkeypatch=monkeypatch,
        )
        history = json.loads(out.read_text())
        assert len(history) == 3, 'appended, not replaced'
        # Order preserved — prior first, new last.
        assert history[0]['commit_sha'] == 'old1'
        assert history[1]['commit_sha'] == 'old2'
        assert history[2]['is_clean'] is True

    def test_prune_respects_ITK_HISTORY_LIMIT(self, tmp_path, monkeypatch):
        """History is pruned to the last N entries (default 50, env-overridable)."""
        # Pretend we already have 4 entries; limit=3 means final size is 3
        # (2 old + 1 new = 3 after pruning drops the oldest).
        prior = [
            {'timestamp': f'2026-08-0{i}T00:00:00+00:00', 'sdk': 'ts', 'line': 'v10',
             'is_clean': True, 'commit_sha': f'old{i}', 'github_run_id': str(i),
             'a2a_itk_sha': f'aitk{i}', 'matches': [], 'real_failures': [],
             'infra_failures': [], 'behavioral_divergences': [],
             'suppressed_count': 0,
             'orphan_result_keys': {'new': [], 'old': []}}
            for i in range(1, 5)
        ]
        monkeypatch.setattr(cr, 'fetch_existing_history', lambda _url: prior[:])

        out = tmp_path / 'shadow.json'
        self._run_with_history(
            tmp_path, history_url='https://example.com/shadow_ts.json',
            history_output_file=out, monkeypatch=monkeypatch,
            env={'ITK_HISTORY_LIMIT': '3'},
        )
        history = json.loads(out.read_text())
        assert len(history) == 3, 'must prune to limit'
        # Oldest entries evicted from the front.
        assert history[0]['commit_sha'] == 'old3'
        assert history[1]['commit_sha'] == 'old4'
        # Newest (this run) at the tail.
        assert history[2]['is_clean'] is True

    def test_fetch_from_file_url(self, tmp_path):
        """Regression: fetch_existing_history must accept file:// URLs.

        The shadow simulator bootstraps consecutive local runs by pointing
        --history_url at the previous run's output via file://. Before
        the fix, urlopen returned a response with .status=None (only HTTP
        responses set status), which fell through the OK check and errored
        'Unexpected HTTP status: None'.
        """
        prior = [{'timestamp': '2026-08-01T00:00:00+00:00', 'is_clean': True}]
        prior_file = tmp_path / 'prior.json'
        prior_file.write_text(json.dumps(prior))
        got = cr.fetch_existing_history(f'file://{prior_file}')
        assert got == prior

    def test_no_history_output_file_writes_nothing(self, tmp_path, monkeypatch):
        """History mode is entirely optional — omitting --history_output_file
        must not fetch anything or write anything."""
        scenarios = tmp_path / 'scenarios.json'
        old = tmp_path / 'old.json'
        new = tmp_path / 'new.json'
        self._write_scenarios(scenarios, ['a'])
        self._write_raw(old, {'a': True})
        self._write_raw(new, {'a': True})

        # Fail loudly if fetch is called — it must not be.
        def fail(_url):
            raise AssertionError('fetch_existing_history must not be called without --history_output_file')
        monkeypatch.setattr(cr, 'fetch_existing_history', fail)

        rc = cr.main([
            '--sdk', 'ts', '--line', 'v10',
            '--scenarios', str(scenarios),
            '--old', str(old), '--new', str(new),
            # Even with --history_url, no output file → no fetch, no write.
            '--history_url', 'https://example.com/never.json',
        ])
        assert rc == 0


class TestPlumbingSelfComparison:
    """Same raw_results.json on both sides ⇒ zero divergence, zero infra."""

    def _raw(self, outcomes: dict[str, bool]) -> dict:
        return {
            'all_passed': all(outcomes.values()),
            'results': {n: {'passed': p, 'sdks': ['current']} for n, p in outcomes.items()},
        }

    def test_all_passing_self_comparison_reports_clean(self):
        # Feeding the same file as both --old and --new must report
        # identical (no divergence, no infra failure, all matches).
        raw = self._raw({'a': True, 'b': True, 'c': True})
        outcomes = cr.raw_to_outcomes(raw)
        scenarios = {n: _scn(n, expected_pass=True) for n in outcomes}
        report = cr.classify_run(
            sdk='python', line='v10', scenarios=scenarios, old=outcomes, new=outcomes, accepted_deltas=set()
        )
        assert report.is_clean is True
        assert report.behavioral_divergences == []
        assert report.infra_failures == []
        assert set(report.matches) == set(outcomes)

    def test_self_comparison_with_expected_failures_still_reports_clean(self):
        # Some scenarios are designed to fail (expected_pass=False). Feeding
        # the SAME results on both sides still produces zero divergence
        # because OLD == NEW; anything expected-to-fail that DID fail is a
        # correct outcome, so it also matches its oracle.
        raw = self._raw({'ok': True, 'expected_to_fail': False})
        outcomes = cr.raw_to_outcomes(raw)
        scenarios = {
            'ok': _scn('ok', expected_pass=True),
            'expected_to_fail': _scn('expected_to_fail', expected_pass=False),
        }
        report = cr.classify_run(
            sdk='python', line='v10', scenarios=scenarios, old=outcomes, new=outcomes, accepted_deltas=set()
        )
        assert report.is_clean is True
        assert set(report.matches) == {'ok', 'expected_to_fail'}

    def test_self_comparison_with_shared_regression_still_flagged(self):
        # The absolute-oracle invariant: agreement of two wrongs is still
        # wrong. Even if OLD == NEW, a scenario supposed to pass that fails
        # on both sides is a REAL_FAILURE — the plumbing test must not mask
        # a real regression baked into the OLD path.
        raw = self._raw({'a': False})  # supposed to pass but doesn't
        outcomes = cr.raw_to_outcomes(raw)
        scenarios = {'a': _scn('a', expected_pass=True)}
        report = cr.classify_run(
            sdk='python', line='v10', scenarios=scenarios, old=outcomes, new=outcomes, accepted_deltas=set()
        )
        assert report.real_failures == ['a']

    def test_self_comparison_with_subtests(self):
        # Self-comparison must also work when a scenario expands into
        # multiple sub-test result keys (``<name>-sub-<sdks>``).
        raw = {
            'all_passed': True,
            'results': {
                'foo-sub-python-go': {'passed': True, 'sdks': ['python', 'go']},
                'foo-sub-python-java': {'passed': True, 'sdks': ['python', 'java']},
            },
        }
        outcomes = cr.raw_to_outcomes(raw)
        scenarios = {'foo': _scn('foo', expected_pass=True)}
        report = cr.classify_run(
            sdk='python', line='v10', scenarios=scenarios, old=outcomes, new=outcomes, accepted_deltas=set()
        )
        assert report.is_clean is True
        assert set(report.matches) == {'foo-sub-python-go', 'foo-sub-python-java'}
