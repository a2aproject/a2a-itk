#!/usr/bin/env python3
"""Validate and summarise an ACTS ``/run-acts`` response.

The ACTS counterpart of :mod:`scripts.itk_report`, and it exists for the same
reason: to stop a FastAPI ``{"detail": ...}`` error envelope reaching the
metrics processor and landing an empty entry in the published history.

Stdlib only, and deliberately does no arithmetic. A §13 report carries its own
``summary`` block, so everything printed here is read out of the document
rather than recomputed — a second implementation of the tally is a second
thing that can disagree with the report a dashboard reads.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


_RULE = '-' * 56

LEVELS = ('must', 'should', 'may')


class InvalidReport(ValueError):
    """The payload is not a well-formed ACTS report document."""


def validate(data: Any) -> dict[str, Any]:
    """Check the payload is a usable §13 report and return it."""
    if not isinstance(data, dict):
        raise InvalidReport(f'expected a JSON object, got {type(data).__name__}')

    if 'detail' in data and 'summary' not in data:
        # FastAPI's error shape. Surfacing it verbatim beats "missing key
        # 'summary'", which sends the reader looking in the wrong place.
        raise InvalidReport(f'service returned an error: {data["detail"]}')

    for key in ('summary', 'suites', 'sdk', 'transport'):
        if key not in data:
            raise InvalidReport(f'report is missing {key!r}')

    summary = data['summary']
    if not isinstance(summary, dict) or 'by_level' not in summary:
        raise InvalidReport('report summary is malformed')

    if not summary.get('total'):
        # A run that executed nothing must not be published: it would push a
        # real entry off the rolling window and read as a clean night.
        raise InvalidReport('report contains no tests')

    return data


def is_conformant(report: dict[str, Any]) -> bool:
    """§12.7: conformant iff every graded ``must`` test passed."""
    must = report['summary']['by_level'].get('must', {})
    return not must.get('failed') and not must.get('errors')


def failures(report: dict[str, Any]):
    """Every failed or errored test, as ``(id, message)``."""
    for suite in report.get('suites', []):
        for test in suite.get('tests', []):
            if test.get('result') in ('fail', 'error'):
                detail = test.get('failure') or {}
                yield test['id'], detail.get('message', '(no detail)')


def format_report(report: dict[str, Any], title: str) -> tuple[str, bool]:
    """Render the §12.7 summary. Returns the text and whether it conformed."""
    summary = report['summary']
    sdk = report['sdk']
    lines = [
        _RULE,
        f'{title}: {sdk.get("name", "?")} / {report["transport"]}',
        _RULE,
        f'{summary["total"]} tests in {summary.get("duration_ms", 0)}ms',
    ]

    for level in LEVELS:
        counts = summary['by_level'].get(level)
        if not counts:
            continue
        graded = counts['total'] - counts['skipped']
        extra = [
            f'{counts[k]} {k}'
            for k in ('failed', 'errors', 'skipped')
            if counts.get(k)
        ]
        suffix = f' ({", ".join(extra)})' if extra else ''
        lines.append(f'  {level.upper():7}{counts["passed"]}/{graded} passed{suffix}')

    listed = list(failures(report))
    if listed:
        lines.append('')
        lines.append(f'{len(listed)} failing test(s):')
        lines.extend(f'  {test_id}: {message}' for test_id, message in listed)

    conformant = is_conformant(report)
    lines.append(_RULE)
    lines.append(
        f'CONFORMANCE: {"CONFORMANT" if conformant else "NOT CONFORMANT"}'
    )
    lines.append(_RULE)
    return '\n'.join(lines), conformant


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--response-file', required=True,
        help='The JSON body returned by POST /run-acts.',
    )
    parser.add_argument('--title', default='ACTS CONFORMANCE RESULTS')
    parser.add_argument(
        '--require-conformant', action='store_true',
        help='Exit non-zero unless every graded `must` test passed. Set on '
             'the PR path; left off on the nightly path, where a failure is '
             'a metric to record rather than a broken run.',
    )
    args = parser.parse_args(argv)

    try:
        with open(args.response_file, encoding='utf-8') as handle:
            report = validate(json.load(handle))
    except (OSError, json.JSONDecodeError) as exc:
        print(f'Error reading {args.response_file}: {exc}', file=sys.stderr)
        return 1
    except InvalidReport as exc:
        print(f'Invalid ACTS report: {exc}', file=sys.stderr)
        return 1

    text, conformant = format_report(report, args.title)
    print(text)
    return 0 if conformant or not args.require_conformant else 1


if __name__ == '__main__':
    sys.exit(main())
