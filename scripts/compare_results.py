#!/usr/bin/env python3
"""Comparison harness for the ITK consolidation migration.

Diffs the OLD ITK path (baked ``a2a-itk/agents/<sdk>/<line>``) against the NEW
path (``itk/main`` fetched at ref by the launcher) and classifies each
scenario's outcome.

Key invariant:
    The oracle is the scenario's ``expected`` field, NOT the baked baseline.
    The baseline is a secondary cross-check that exists only during the
    migration; the durable pass/fail truth is ``scenario.expected``, which
    survives ``a2a-itk/agents/`` deletion.

Runner-vs-classifier boundary:
    Retry lives in the runner, not this classifier. By the time ``evaluate()``
    is called, retries are exhausted and the ``Outcome`` is final. A
    ``transient=False`` error at that point is a REAL_FAILURE — never
    laundered to infra.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import enum
import json
import logging
import pathlib
import sys


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Types — explicit ``bool`` vs ``str`` normalization keeps evaluate() a pure
# bool-vs-bool comparison (avoids accidental "pass" == True bugs).
# -----------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Outcome:
    """Final outcome of one scenario on one path (OLD or NEW).

    Post-retry: by the time this reaches ``evaluate()``, transient errors
    have been retried to exhaustion.

    - ``passed``: set iff the run completed (True/False from ``raw_results.json``).
    - ``error``: set iff the path errored (build fail, connect timeout, crash).
    - ``transient``: True ⇒ retryable infra (network, dep resolution, build
      timeout); False ⇒ deterministic (assertion, non-transient crash).
    """

    passed: bool | None = None
    error: str | None = None
    transient: bool = False


class Result(enum.Enum):
    """Per-scenario classification."""

    MATCH = 'match'
    """NEW is correct AND agrees with the (soon-deleted) baseline."""

    INFRA_FAILURE = 'infra_failure'
    """Transient error survived retry — runner-level infra hiccup."""

    REAL_FAILURE = 'real_failure'
    """NEW disagrees with the ``expected`` oracle. Blocks cutover.

    Also catches a regression that BOTH paths share — the baseline is
    not trusted.
    """

    BEHAVIORAL_DIVERGENCE = 'behavioral_divergence'
    """NEW satisfies the oracle but the baseline drifted — needs human
    adjudication; record in ``accepted_deltas.json`` once accepted."""


@dataclasses.dataclass(frozen=True)
class Scenario:
    """Minimal schema needed by the classifier.

    ``expected_pass`` is normalized at load time (``expected: "pass"|"fail"``
    → ``bool``) so the classifier does bool-vs-bool comparisons only.
    """

    name: str
    expected_pass: bool


@dataclasses.dataclass
class RunReport:
    """Per-SDK per-line aggregated classification for one comparison run."""

    sdk: str
    line: str
    matches: list[str] = dataclasses.field(default_factory=list)
    real_failures: list[str] = dataclasses.field(default_factory=list)
    infra_failures: list[str] = dataclasses.field(default_factory=list)
    behavioral_divergences: list[str] = dataclasses.field(default_factory=list)
    suppressed_count: int = 0  # accepted deltas that would otherwise divergence

    @property
    def is_clean(self) -> bool:
        """The cutover-gate day-level definition (IMPL_DETAILS §C).

        Clean ⇔ zero real_failures, zero infra_failures, zero
        un-adjudicated behavioral_divergences.
        """
        return (
            not self.real_failures
            and not self.infra_failures
            and not self.behavioral_divergences
        )


# -----------------------------------------------------------------------------
# Scenario loader — normalizes `expected` → `expected_pass` (bool)
# -----------------------------------------------------------------------------


_EXPECTED_MAP = {'pass': True, 'fail': False}


def load_scenarios(path: pathlib.Path) -> dict[str, Scenario]:
    """Load a ``scenarios.json``-shaped file into ``{name: Scenario}``.

    The file schema follows today's per-SDK ``scenarios{,_full}.json``:
    ``{"tests": [{"name": ..., "expected": "pass"|"fail", ...}]}``.

    Backwards compat: today's per-SDK scenarios files have no ``expected``
    field (they exist because they're supposed to pass). Missing ⇒ default
    to ``expected_pass=True``. Once the shared scenario schema lands,
    ``expected`` will be required.
    """
    data = json.loads(pathlib.Path(path).read_text())
    scenarios: dict[str, Scenario] = {}
    for test in data.get('tests', []):
        name = test['name']
        raw_expected = test.get('expected', 'pass')
        if raw_expected not in _EXPECTED_MAP:
            raise ValueError(
                f"Scenario {name!r}: unknown expected value {raw_expected!r}"
                f' (must be one of {sorted(_EXPECTED_MAP)})'
            )
        scenarios[name] = Scenario(name=name, expected_pass=_EXPECTED_MAP[raw_expected])
    return scenarios


# -----------------------------------------------------------------------------
# raw_results.json adapter — pins the interop shape in one place
# -----------------------------------------------------------------------------


def raw_to_outcomes(raw: dict) -> dict[str, Outcome]:
    """Convert one ``raw_results.json`` payload to ``{name: Outcome}``.

    Mirrors the shape ``process_results.py`` already handles:
    ``details`` may be either ``{"passed": bool, ...}`` or a plain ``bool``.
    Errors are not surfaced in this file today; the runner is responsible for
    producing an ``Outcome(error=..., transient=...)`` when the whole path
    blew up before writing raw_results.json.
    """
    outcomes: dict[str, Outcome] = {}
    for name, details in raw.get('results', {}).items():
        if isinstance(details, dict):
            passed = bool(details.get('passed', False))
        elif isinstance(details, bool):
            passed = details
        else:
            # Defensive: unknown shape ⇒ mark as deterministic error so the
            # classifier flags it as REAL_FAILURE rather than silently pass.
            outcomes[name] = Outcome(error=f'unknown result shape: {type(details).__name__}', transient=False)
            continue
        outcomes[name] = Outcome(passed=passed)
    return outcomes


# -----------------------------------------------------------------------------
# accepted_deltas.json — suppresses already-adjudicated divergences.
#
# Schema:
#   {
#     "deltas": [
#       {"sdk": "python", "line": "v10", "scenario": "<name>",
#        "expected": "pass", "old_passed": false, "new_passed": true,
#        "reason": "<why the delta is acceptable>",
#        "adjudicated_by": "<ldap>", "adjudicated_at": "<ISO-8601>"}
#     ]
#   }
# -----------------------------------------------------------------------------


AcceptedKey = tuple[str, str, str]  # (sdk, line, scenario)


def load_accepted_deltas(path: pathlib.Path) -> set[AcceptedKey]:
    """Load the accepted-deltas file into a set of (sdk, line, scenario) keys.

    Missing file → empty set (a fresh repo has no adjudicated deltas yet).
    """
    p = pathlib.Path(path)
    if not p.exists():
        return set()
    data = json.loads(p.read_text())
    return {(d['sdk'], d['line'], d['scenario']) for d in data.get('deltas', [])}


# -----------------------------------------------------------------------------
# evaluate() — pure classification
# -----------------------------------------------------------------------------


def evaluate(scenario: Scenario, old: Outcome, new: Outcome) -> Result:
    """Classify one (old, new) Outcome pair against the scenario's oracle.

    Both Outcomes are POST-retry finals (see module docstring). Order of
    checks matters:

    1. NEW error: transient ⇒ INFRA_FAILURE, deterministic ⇒ REAL_FAILURE
       (never launder a deterministic NEW failure to infra).
    2. NEW.passed vs scenario.expected_pass — the absolute oracle. Disagree
       ⇒ REAL_FAILURE, even if OLD also disagrees (catches shared regressions,
       stale baselines, etc.).
    3. NEW is correct. Cross-check the baseline (about to be deleted):
       a. OLD error transient ⇒ INFRA_FAILURE; deterministic ⇒ divergence.
       b. OLD.passed == NEW.passed ⇒ MATCH; else BEHAVIORAL_DIVERGENCE
          (baseline stale — adjudicate).
    """
    # 1. NEW is what we validate. Errors first.
    if new.error is not None:
        return Result.INFRA_FAILURE if new.transient else Result.REAL_FAILURE

    # 2. Absolute oracle: bool-vs-bool. Catches shared regressions where
    #    OLD and NEW both fail a scenario that's supposed to pass.
    if new.passed != scenario.expected_pass:
        return Result.REAL_FAILURE

    # 3. NEW is correct. Cross-check the (soon-deleted) baseline.
    if old.error is not None:
        return Result.INFRA_FAILURE if old.transient else Result.BEHAVIORAL_DIVERGENCE

    if old.passed == new.passed:
        return Result.MATCH

    return Result.BEHAVIORAL_DIVERGENCE


# -----------------------------------------------------------------------------
# Aggregation — classify a whole run, with accepted-delta suppression
# -----------------------------------------------------------------------------


def classify_run(
    sdk: str,
    line: str,
    scenarios: dict[str, Scenario],
    old: dict[str, Outcome],
    new: dict[str, Outcome],
    accepted_deltas: set[AcceptedKey],
) -> RunReport:
    """Classify every scenario and roll up into a RunReport.

    Missing outcomes are treated as infra failures on the responsible side:
    if NEW is missing entirely (runner never produced a result), that's a
    transient infra problem from the classifier's point of view — retry
    upstream. If OLD is missing but NEW satisfies the oracle, still infra
    on OLD (we can't cross-check).
    """
    report = RunReport(sdk=sdk, line=line)
    for name, scenario in scenarios.items():
        new_outcome = new.get(name)
        old_outcome = old.get(name)
        if new_outcome is None:
            report.infra_failures.append(name)
            continue
        if old_outcome is None:
            # No baseline to cross-check. Only judge NEW vs oracle.
            old_outcome = Outcome(error='baseline outcome missing', transient=True)

        result = evaluate(scenario, old_outcome, new_outcome)
        if result is Result.MATCH:
            report.matches.append(name)
        elif result is Result.REAL_FAILURE:
            report.real_failures.append(name)
        elif result is Result.INFRA_FAILURE:
            report.infra_failures.append(name)
        elif result is Result.BEHAVIORAL_DIVERGENCE:
            if (sdk, line, name) in accepted_deltas:
                report.suppressed_count += 1
                report.matches.append(name)  # treated as clean going forward
            else:
                report.behavioral_divergences.append(name)
        else:  # pragma: no cover - exhaustive above
            raise AssertionError(f'unreachable Result: {result!r}')
    return report


# -----------------------------------------------------------------------------
# Cutover streak (N clean days per SDK, persisted JSON).
#
# Storage shape (published as a GitHub Release asset alongside the ITK
# nightly history — same durable-store pattern process_results.py uses):
#
#     {
#       "python": [
#         {"date": "2026-07-20", "clean": true},
#         {"date": "2026-07-21", "clean": false},
#         ...
#       ],
#       "go": [...]
#     }
#
# Deliberately human-readable (release-asset consumers are humans).
# -----------------------------------------------------------------------------


def _read_streak_file(path: pathlib.Path) -> dict[str, list[dict]]:
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def _write_streak_file(path: pathlib.Path, data: dict[str, list[dict]]) -> None:
    pathlib.Path(path).write_text(json.dumps(data, indent=2, sort_keys=True))


def record_run(
    streak_file: pathlib.Path, *, sdk: str, run_date: str, clean: bool
) -> None:
    """Append (or overwrite same-date) one day's cleanliness for one SDK.

    Idempotent per ``(sdk, run_date)``: nightly re-runs and manual re-runs
    for the same date overwrite rather than double-count.
    """
    data = _read_streak_file(streak_file)
    entries = [e for e in data.get(sdk, []) if e['date'] != run_date]
    entries.append({'date': run_date, 'clean': clean})
    entries.sort(key=lambda e: e['date'])
    data[sdk] = entries
    _write_streak_file(streak_file, data)


def current_streak_days(streak_file: pathlib.Path, *, sdk: str) -> int:
    """Return the length of the current trailing streak of clean days."""
    data = _read_streak_file(streak_file)
    entries = data.get(sdk, [])
    streak = 0
    for entry in reversed(entries):
        if entry['clean']:
            streak += 1
        else:
            break
    return streak


def cutover_gate_passes(
    streak_file: pathlib.Path, *, sdk: str, required_days: int = 7
) -> bool:
    """Cutover gate: N consecutive clean days ⇒ safe to delete baseline.

    Only when this returns True for every in-scope SDK may
    ``a2a-itk/agents/<sdk>/*`` be deleted.
    """
    return current_streak_days(streak_file, sdk=sdk) >= required_days


# -----------------------------------------------------------------------------
# CLI entry point — used by the shadow CI job and per-SDK cutover checks.
# -----------------------------------------------------------------------------


def _load_raw(path: pathlib.Path) -> dict[str, Outcome]:
    return raw_to_outcomes(json.loads(pathlib.Path(path).read_text()))


def _default_run_date() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Comparison harness: classify NEW vs OLD ITK results against '
            'the scenario oracle and update the cutover-streak file.'
        )
    )
    parser.add_argument('--sdk', required=True, help='SDK being adjudicated (python, go, java, rust, ts).')
    parser.add_argument('--line', required=True, help='Version line (v10 | v03).')
    parser.add_argument('--scenarios', required=True, type=pathlib.Path, help='Path to scenarios.json.')
    parser.add_argument('--old', required=True, type=pathlib.Path, help='OLD path raw_results.json.')
    parser.add_argument('--new', required=True, type=pathlib.Path, help='NEW path raw_results.json.')
    parser.add_argument('--streak-file', required=True, type=pathlib.Path, help='Cutover-streak persistence file.')
    parser.add_argument(
        '--accepted-deltas',
        type=pathlib.Path,
        default=None,
        help='Path to accepted_deltas.json (optional; missing ⇒ none accepted).',
    )
    parser.add_argument(
        '--run-date',
        default=None,
        help='ISO date (YYYY-MM-DD) to record this run under. Defaults to today (UTC).',
    )
    parser.add_argument(
        '--required-days',
        type=int,
        default=7,
        help='Cutover-gate threshold in clean days (default: 7).',
    )
    parser.add_argument(
        '--report-file',
        type=pathlib.Path,
        default=None,
        help='Optional path to write a JSON report of this run for CI artifact upload.',
    )
    return parser


def _report_to_dict(report: RunReport) -> dict:
    return {
        'sdk': report.sdk,
        'line': report.line,
        'is_clean': report.is_clean,
        'matches': report.matches,
        'real_failures': report.real_failures,
        'infra_failures': report.infra_failures,
        'behavioral_divergences': report.behavioral_divergences,
        'suppressed_count': report.suppressed_count,
    }


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    scenarios = load_scenarios(args.scenarios)
    old_outcomes = _load_raw(args.old)
    new_outcomes = _load_raw(args.new)
    accepted = (
        load_accepted_deltas(args.accepted_deltas)
        if args.accepted_deltas is not None
        else set()
    )

    report = classify_run(
        sdk=args.sdk,
        line=args.line,
        scenarios=scenarios,
        old=old_outcomes,
        new=new_outcomes,
        accepted_deltas=accepted,
    )

    run_date = args.run_date or _default_run_date()
    record_run(args.streak_file, sdk=args.sdk, run_date=run_date, clean=report.is_clean)

    logger.info(
        'Comparison report — sdk=%s line=%s clean=%s matches=%d real=%d infra=%d div=%d suppressed=%d streak=%dd',
        report.sdk,
        report.line,
        report.is_clean,
        len(report.matches),
        len(report.real_failures),
        len(report.infra_failures),
        len(report.behavioral_divergences),
        report.suppressed_count,
        current_streak_days(args.streak_file, sdk=args.sdk),
    )

    if report.real_failures:
        logger.error('REAL_FAILURE scenarios (block cutover): %s', report.real_failures)
    if report.infra_failures:
        logger.warning('INFRA_FAILURE scenarios (retry upstream): %s', report.infra_failures)
    if report.behavioral_divergences:
        logger.warning(
            'BEHAVIORAL_DIVERGENCE (baseline stale — adjudicate in accepted_deltas.json): %s',
            report.behavioral_divergences,
        )

    if args.report_file is not None:
        args.report_file.write_text(json.dumps(_report_to_dict(report), indent=2, sort_keys=True))

    # Exit code: clean ⇒ 0; dirty ⇒ 1. Cutover gate (N-day streak) is
    # checked separately by CI at baseline-deletion time.
    return 0 if report.is_clean else 1


if __name__ == '__main__':
    sys.exit(main())
