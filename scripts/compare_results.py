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
    # Per-side (``"new"`` / ``"old"``) list of result keys that matched no
    # known scenario. See ``_log_orphan_keys``. Purely observational; does
    # not affect ``is_clean``.
    orphan_result_keys: dict[str, list[str]] = dataclasses.field(
        default_factory=lambda: {'new': [], 'old': []}
    )

    @property
    def is_clean(self) -> bool:
        """The cutover-gate day-level definition.

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


# `-sub-<sdks>` suffix produced by testlib.execute_itk_test when a scenario has
# ``build_subtests=True``. See testlib.py:719 (producer) and
# scripts/process_results.py:151 (existing consumer using the same convention).
_SUBTEST_SEP = '-sub-'


def _result_keys_for_scenario(name: str, outcomes: dict[str, Outcome]) -> list[str]:
    """Return every outcome key that belongs to a scenario.

    A scenario with ``build_subtests=True`` produces multiple result entries
    (``<name>-sub-<sdk1>-<sdk2>...``) plus optionally the base ``<name>`` key
    when the full-SDK subgraph is included. This resolver returns *all* such
    keys so the classifier evaluates every sub-outcome, not just the base one.
    """
    keys = []
    if name in outcomes:
        keys.append(name)
    sub_prefix = f'{name}{_SUBTEST_SEP}'
    keys.extend(sorted(k for k in outcomes if k.startswith(sub_prefix)))
    return keys


def _parent_scenario_name(result_key: str) -> str:
    """Strip the ``-sub-<sdks>`` suffix, matching process_results.py:151."""
    return result_key.split(_SUBTEST_SEP, 1)[0]


def classify_run(
    sdk: str,
    line: str,
    scenarios: dict[str, Scenario],
    old: dict[str, Outcome],
    new: dict[str, Outcome],
    accepted_deltas: set[AcceptedKey],
) -> RunReport:
    """Classify every scenario (including sub-tests) and roll up into a RunReport.

    Sub-tests (``<name>-sub-<sdks>`` keys, see testlib.py:719) are matched
    back to their parent scenario and each is classified against the parent's
    ``expected_pass`` oracle. Any real failure in a sub-test makes the parent
    real-failing, matching how ``itk_service.py`` treats the aggregate result.

    Missing outcomes are treated as infra failures on the responsible side:
    if NEW is missing entirely (runner never produced a result), that's a
    transient infra problem from the classifier's point of view — retry
    upstream. If OLD is missing but NEW satisfies the oracle, still infra
    on OLD (we can't cross-check).

    Orphan result keys (present in NEW/OLD but matching no known scenario) are
    logged at WARNING and counted; they mirror
    ``process_results.py``'s ``'No matching base scenario found for result
    key: %s'`` so both consumers surface the same drift signal.
    """
    report = RunReport(sdk=sdk, line=line)

    # Determine which result keys belong to a scenario (base + any sub-tests).
    for name, scenario in scenarios.items():
        result_keys = _result_keys_for_scenario(name, new) or [name]
        for key in result_keys:
            new_outcome = new.get(key)
            old_outcome = old.get(key)
            if new_outcome is None:
                report.infra_failures.append(key)
                continue
            if old_outcome is None:
                # No baseline to cross-check. Only judge NEW vs oracle.
                old_outcome = Outcome(error='baseline outcome missing', transient=True)

            result = evaluate(scenario, old_outcome, new_outcome)
            if result is Result.MATCH:
                report.matches.append(key)
            elif result is Result.REAL_FAILURE:
                report.real_failures.append(key)
            elif result is Result.INFRA_FAILURE:
                report.infra_failures.append(key)
            elif result is Result.BEHAVIORAL_DIVERGENCE:
                # Adjudication is per parent scenario — a sub-test drift is
                # covered by the parent's accepted-delta entry.
                if (sdk, line, name) in accepted_deltas:
                    report.suppressed_count += 1
                    report.matches.append(key)  # treated as clean going forward
                else:
                    report.behavioral_divergences.append(key)
            else:  # pragma: no cover - exhaustive above
                raise AssertionError(f'unreachable Result: {result!r}')

    # Orphan-key detection: any NEW/OLD result key that doesn't map to a
    # known scenario. Mirrors process_results.py's warning pattern so the two
    # consumers of raw_results.json flag drift the same way, and covers the
    # "new and old have different number of outcomes" case (either side's
    # orphan is reported).
    _log_orphan_keys('new', new, scenarios, report)
    _log_orphan_keys('old', old, scenarios, report)

    return report


def _log_orphan_keys(
    side: str,
    outcomes: dict[str, Outcome],
    scenarios: dict[str, Scenario],
    report: RunReport,
) -> None:
    """Warn about result keys with no matching scenario and record the count.

    A missing scenario for a produced result is usually one of:
    - a scenario was renamed/deleted upstream but the runner still emits it
    - a sub-test suffix format changed without the classifier being updated
    - the two paths produced different sets of scenarios (drift between OLD
      and NEW schedulers).

    Any of those is worth a loud log rather than silent ignore.
    """
    orphans = [
        key for key in outcomes if _parent_scenario_name(key) not in scenarios
    ]
    for key in orphans:
        logger.warning(
            'No matching base scenario found for %s result key: %s', side, key
        )
    report.orphan_result_keys[side] = orphans


# -----------------------------------------------------------------------------
# CLI entry point — one-shot: read OLD + NEW, classify, exit 0/1.
# -----------------------------------------------------------------------------


def _load_raw(path: pathlib.Path) -> dict[str, Outcome]:
    return raw_to_outcomes(json.loads(pathlib.Path(path).read_text()))


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Classify NEW vs OLD ITK results against the scenario oracle. '
            'Exits 0 if clean, 1 otherwise.'
        )
    )
    parser.add_argument('--sdk', required=True, help='SDK being classified (python, go, java, rust, ts).')
    parser.add_argument('--line', required=True, help='Version line (v10 | v03).')
    parser.add_argument('--scenarios', required=True, type=pathlib.Path, help='Path to scenarios.json.')
    parser.add_argument('--old', required=True, type=pathlib.Path, help='OLD path raw_results.json.')
    parser.add_argument('--new', required=True, type=pathlib.Path, help='NEW path raw_results.json.')
    parser.add_argument(
        '--accepted-deltas',
        type=pathlib.Path,
        default=None,
        help='Path to accepted_deltas.json (optional; missing ⇒ none accepted).',
    )
    parser.add_argument(
        '--report-file',
        type=pathlib.Path,
        default=None,
        help='Optional path to write the JSON report for CI artifact upload.',
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
        'orphan_result_keys': report.orphan_result_keys,
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

    logger.info(
        'Comparison report — sdk=%s line=%s clean=%s matches=%d real=%d infra=%d div=%d suppressed=%d',
        report.sdk,
        report.line,
        report.is_clean,
        len(report.matches),
        len(report.real_failures),
        len(report.infra_failures),
        len(report.behavioral_divergences),
        report.suppressed_count,
    )

    if report.real_failures:
        logger.error('REAL_FAILURE scenarios: %s', report.real_failures)
    if report.infra_failures:
        logger.warning('INFRA_FAILURE scenarios (retry upstream): %s', report.infra_failures)
    if report.behavioral_divergences:
        logger.warning(
            'BEHAVIORAL_DIVERGENCE (baseline stale — adjudicate in accepted_deltas.json): %s',
            report.behavioral_divergences,
        )

    if args.report_file is not None:
        args.report_file.write_text(
            json.dumps(_report_to_dict(report), indent=2, sort_keys=True)
        )

    return 0 if report.is_clean else 1


if __name__ == '__main__':
    sys.exit(main())
