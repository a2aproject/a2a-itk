"""The ACTS report document (spec §13).

`runner.py` produces :class:`~test_suite.acts.runner.TestResult` objects; this
turns them into the JSON every ACTS runner is required to emit, so a dashboard
can read a run from any implementation without knowing who produced it.

**The format is frozen.** Dashboards and certification portals consume it, and
§13 makes it a MUST rather than a MAY. Same rule as ITK's `process_results.py`:
add to it only additively, and never rename or drop a field while passing
through.

Two details that are easy to get wrong. `errors` counts *runner* errors, not
test failures — the distinction the runner keeps everywhere, carried through
here so a broken harness cannot be read as a non-conformant SDK. And a test's
suite membership comes from the loader's provenance, not from the result, so
building a report needs the `LoadedSuite` the run came from.
"""

from __future__ import annotations

import json
import platform
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from test_suite.acts.loader import LoadedSuite
from test_suite.acts.runner import Outcome, TestResult
from test_suite.acts.schema import ACTS_VERSION, Level, TransportBinding


#: §13.5's naming convention. `SHOULD`, but there is no reason to differ.
FILENAME = 'acts-report-{sdk}-{transport}-{timestamp}.json'

_TIMESTAMP = '%Y%m%dT%H%M%SZ'


def _now() -> datetime:
    return datetime.now(timezone.utc)


def environment() -> dict[str, str]:
    """What ran the suite. §13.1 leaves the keys open."""
    return {
        'python': platform.python_version(),
        'platform': platform.platform(),
        'runner': 'a2a-itk',
    }


def _level_summary(results: Sequence[TestResult], level: Level) -> dict[str, int]:
    at_level = [r for r in results if r.level is level]
    return {
        'total': len(at_level),
        'passed': sum(r.result is Outcome.PASS for r in at_level),
        'failed': sum(r.result is Outcome.FAIL for r in at_level),
        'skipped': sum(r.result is Outcome.SKIP for r in at_level),
        'errors': sum(r.result is Outcome.ERROR for r in at_level),
    }


def summarize(results: Sequence[TestResult], duration_ms: int) -> dict[str, Any]:
    """§13.2 `summary`, including the per-level breakdown."""
    return {
        'total': len(results),
        'passed': sum(r.result is Outcome.PASS for r in results),
        'failed': sum(r.result is Outcome.FAIL for r in results),
        'skipped': sum(r.result is Outcome.SKIP for r in results),
        'errors': sum(r.result is Outcome.ERROR for r in results),
        'duration_ms': duration_ms,
        'by_level': {
            level.value: _level_summary(results, level) for level in Level
        },
    }


def _test_result(result: TestResult) -> dict[str, Any]:
    """§13.3 `test-result`. Optional keys are omitted when unset."""
    out: dict[str, Any] = {
        'id': result.id,
        'name': result.name,
        'level': result.level.value,
        'result': result.result.value,
        'duration_ms': result.duration_ms,
    }
    if result.skip_reason:
        out['skip_reason'] = result.skip_reason
    if result.failure is not None:
        out['failure'] = result.failure.as_json()
    if result.steps:
        out['steps'] = [
            {
                'id': step.id,
                'result': step.result.value,
                'duration_ms': step.duration_ms,
                **(
                    {'failure': step.failure.as_json()}
                    if step.failure is not None
                    else {}
                ),
            }
            for step in result.steps
        ]
    return out


def _suites(
    results: Sequence[TestResult], suite: LoadedSuite
) -> list[dict[str, Any]]:
    """Group results back under the suites they were loaded from.

    Order follows the loader's, not the results': a report whose suites move
    around between runs makes every diff unreadable.
    """
    provenance = {loaded.id: loaded for loaded in suite.tests}
    by_id = {r.id: r for r in results}

    grouped: dict[str, dict[str, Any]] = {}
    for loaded in suite.tests:
        if loaded.id not in by_id:
            continue
        entry = grouped.setdefault(
            loaded.suite_id,
            {'id': loaded.suite_id, 'name': loaded.suite_name, 'tests': []},
        )
        entry['tests'].append(_test_result(by_id[loaded.id]))

    # A result the suite never produced would vanish silently otherwise.
    orphans = [r for r in results if r.id not in provenance]
    if orphans:
        grouped['(unknown)'] = {
            'id': '(unknown)',
            'name': 'Results with no suite in the loaded corpus',
            'tests': [_test_result(r) for r in orphans],
        }
    return list(grouped.values())


def build(
    results: Sequence[TestResult],
    suite: LoadedSuite,
    *,
    sdk: Mapping[str, str],
    transport: TransportBinding,
    duration_ms: int,
    spec_version: str = '1.0',
    acts_version: str = ACTS_VERSION,
    env: Mapping[str, str] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a §13.1 report document.

    Args:
        results: What the runner produced, in run order.
        suite: The loaded corpus, for suite grouping and names.
        sdk: §13.1 `sdk-info` — `name`, `version`, `language`, optional
            `repository`.
        transport: Which binding the run used.
        duration_ms: Wall time for the whole run.
        spec_version: A2A protocol version tested.
        acts_version: ACTS format version.
        env: Overrides :func:`environment`.
        generated_at: Overrides the clock, for reproducible tests.
    """
    missing = {'name', 'version', 'language'} - set(sdk)
    if missing:
        raise ValueError(f'sdk-info needs {sorted(missing)} (spec §13.1)')

    return {
        'acts_version': acts_version,
        'spec_version': spec_version,
        'generated_at': (generated_at or _now()).isoformat().replace(
            '+00:00', 'Z'
        ),
        'sdk': dict(sdk),
        'transport': transport.value,
        'environment': dict(env) if env is not None else environment(),
        'summary': summarize(results, duration_ms),
        'suites': _suites(results, suite),
    }


def filename(
    sdk: str,
    transport: TransportBinding,
    when: datetime | None = None,
) -> str:
    """§13.5's `acts-report-{sdk}-{transport}-{timestamp}.json`."""
    return FILENAME.format(
        sdk=sdk,
        transport=transport.value,
        timestamp=(when or _now()).strftime(_TIMESTAMP),
    )


def write(report: Mapping[str, Any], directory: Path) -> Path:
    """Write a report under ``directory``, returning the path.

    The name is derived from the report's own `sdk` and `transport`, so a
    file cannot end up claiming to be a run it is not.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename(
        report['sdk']['name'], TransportBinding(report['transport'])
    )
    path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return path


def conformance_lines(report: Mapping[str, Any]) -> list[str]:
    """§12.7's human-readable summary, one line per level.

    `MUST:   45/47 passed (2 skipped)` — the shape the spec prints.
    """
    lines = []
    for level in Level:
        counts = report['summary']['by_level'][level.value]
        graded = counts['total'] - counts['skipped']
        extra: list[str] = []
        if counts['failed']:
            extra.append(f'{counts["failed"]} failed')
        if counts['errors']:
            extra.append(f'{counts["errors"]} errors')
        if counts['skipped']:
            extra.append(f'{counts["skipped"]} skipped')
        suffix = f' ({", ".join(extra)})' if extra else ''
        lines.append(
            f'{level.value.upper():7}{counts["passed"]}/{graded} passed{suffix}'
        )
    return lines


def is_conformant(report: Mapping[str, Any]) -> bool:
    """§12.7: conformant iff every graded `must` test passed."""
    must = report['summary']['by_level'][Level.MUST.value]
    return must['failed'] == 0 and must['errors'] == 0


def render(report: Mapping[str, Any], stream: Any = None) -> None:
    """Print the §12.7 summary. For a human watching a local run."""
    out = stream or sys.stdout
    summary = report['summary']
    print(
        f'ACTS {report["sdk"]["name"]} / {report["transport"]} — '
        f'{summary["total"]} tests in {summary["duration_ms"]}ms',
        file=out,
    )
    for line in conformance_lines(report):
        print(f'  {line}', file=out)
    verdict = 'CONFORMANT' if is_conformant(report) else 'NOT CONFORMANT'
    print(f'  => {verdict}', file=out)


def failures(report: Mapping[str, Any]) -> Iterable[tuple[str, str]]:
    """Every failed or errored test, as `(id, message)`. For a run log."""
    for suite in report['suites']:
        for test in suite['tests']:
            if test['result'] in ('fail', 'error'):
                detail = test.get('failure') or {}
                yield test['id'], detail.get('message', '(no detail)')


__all__ = [
    'FILENAME',
    'build',
    'conformance_lines',
    'environment',
    'failures',
    'filename',
    'is_conformant',
    'render',
    'summarize',
    'write',
]
