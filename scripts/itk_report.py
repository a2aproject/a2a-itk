#!/usr/bin/env python3
"""Validate and summarise an ITK ``/run`` response.

Split out of the per-SDK ``run_itk.sh`` scripts, which each grew their own
inline ``python3 -c`` heredoc for this and drifted apart:

  * python, go and java shared a bug — ``results[name]`` is an object
    (``{"passed": ..., "sdks": [...]}``), and ``if passed:`` on a non-empty
    dict is always true, so every scenario printed PASSED regardless of its
    real outcome. Only the ``all_passed`` line was truthful.
  * rust alone validated the response shape before using it, catching the
    case where the service returned a FastAPI ``{"detail": ...}`` error and
    the summariser or the metrics processor would otherwise consume garbage.
  * js alone printed a summary on the nightly path too.

This module is the union of the correct behaviours.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


_RULE = '-' * 56


class InvalidResponse(ValueError):
    """The payload is not a well-formed ITK ``/run`` response."""


def validate(data: Any) -> dict[str, Any]:
    """Check the payload is a usable ``/run`` response and return it.

    Guards both consumers: the summariser below, and
    ``scripts/process_results.py``, which would otherwise write an empty
    history entry to the nightly release asset.

    Raises:
        InvalidResponse: Not an object, an error envelope, or missing a
            well-formed ``results`` map.
    """
    if not isinstance(data, dict):
        raise InvalidResponse(
            f'response is not a JSON object (got {type(data).__name__})'
        )
    if 'detail' in data:
        raise InvalidResponse(f'service returned an error: {data["detail"]}')
    if 'results' not in data:
        raise InvalidResponse(
            f'response missing "results" field. Keys: {sorted(data)}'
        )
    if not isinstance(data['results'], dict):
        raise InvalidResponse(
            f'"results" is not an object (got {type(data["results"]).__name__})'
        )
    if not data['results']:
        # A run that executed nothing is not a pass. Rejected here rather than
        # downstream, because process_results.py would append a run with zero
        # scenarios and — at the history limit — push a real entry off the end
        # of the rolling window.
        raise InvalidResponse('"results" is empty; nothing ran')
    return data


def scenario_passed(value: Any) -> bool:
    """Read one scenario's outcome.

    ``/run`` returns ``{"passed": bool, "sdks": [...], "edges": [...]}`` per
    scenario. Anything that isn't such an object — including a bare bool — is
    treated as a failure rather than trusted.
    """
    return isinstance(value, dict) and bool(value.get('passed', False))


def startup_lines(startup: Any) -> list[str]:
    """Render the startup summary, empty when the cluster came up clean.

    Two shapes. A *total* failure (``cluster_error`` set) means the cluster
    never produced a testable run, so every scenario is a failed placeholder —
    say so plainly, as that is why the run is red and, on nightly, why the
    rolling entry exists at all. A *partial* failure lists the peers dropped
    and the coverage lost: not a scenario failure (the run stays green), but
    it has to be visible, because on a passing run the container log (where
    the service logged it) is never dumped.
    """
    if not isinstance(startup, dict):
        return []

    cluster_error = startup.get('cluster_error')
    if cluster_error:
        return [
            _RULE,
            f'CLUSTER STARTUP FAILED: {cluster_error}',
            'Every scenario is recorded as FAILED for this run.',
        ]

    if not startup.get('dropped_peers'):
        return []

    lines = [_RULE, 'PEERS DROPPED (failed to build/start, excluded):']
    for agent, detail in sorted(startup['dropped_peers'].items()):
        lines.append(f'  {agent}: {detail}')

    trimmed = startup.get('trimmed') or []
    if trimmed:
        lines.append('Scenarios run with a peer removed:')
        for entry in trimmed:
            dropped = ', '.join(entry.get('dropped', []))
            lines.append(f'  {entry.get("name")} (without {dropped})')

    skipped = startup.get('skipped') or []
    if skipped:
        lines.append('Scenarios SKIPPED (a required peer was down):')
        for entry in skipped:
            missing = ', '.join(entry.get('missing', []))
            lines.append(f'  {entry.get("name")} (needs {missing})')
    return lines


def format_report(data: dict[str, Any], title: str) -> tuple[str, bool]:
    """Render the human-readable summary and report whether everything passed.

    ``all_passed`` is taken from the response rather than recomputed, so the
    printed verdict matches what the service concluded. An empty result set
    counts as a failure: a run that executed nothing did not pass. A dropped
    peer is reported but does not change the verdict — the surviving
    scenarios stand on their own.
    """
    results = data['results']
    lines = [_RULE, f'{title}:', _RULE]
    lines.extend(
        f'{name}: {"PASSED" if scenario_passed(value) else "FAILED"}'
        for name, value in results.items()
    )
    lines.extend(startup_lines(data.get('startup')))
    all_passed = bool(data.get('all_passed', False)) and bool(results)
    lines.append(_RULE)
    lines.append(f'OVERALL STATUS: {"PASSED" if all_passed else "FAILED"}')
    return '\n'.join(lines), all_passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--response-file',
        required=True,
        help="Path to the saved /run response body, or '-' for stdin.",
    )
    parser.add_argument(
        '--title',
        default='ITK TEST RESULTS',
        help='Heading for the summary block.',
    )
    parser.add_argument(
        '--require-all-passed',
        action='store_true',
        help='Exit non-zero when any scenario failed. Off for nightly runs, '
             'where process_results.py owns the exit code.',
    )
    args = parser.parse_args(argv)

    raw = (
        sys.stdin.read()
        if args.response_file == '-'
        else open(args.response_file, encoding='utf-8').read()  # noqa: SIM115
    )

    try:
        data = validate(json.loads(raw))
    except json.JSONDecodeError as e:
        print(f'ERROR: could not parse ITK response JSON: {e}', file=sys.stderr)
        print(f'Raw response: {raw}', file=sys.stderr)
        return 1
    except InvalidResponse as e:
        print(f'ERROR: {e}', file=sys.stderr)
        print(f'Raw response: {raw}', file=sys.stderr)
        return 1

    report, all_passed = format_report(data, args.title)
    print(report)
    return 1 if args.require_all_passed and not all_passed else 0


if __name__ == '__main__':
    sys.exit(main())
