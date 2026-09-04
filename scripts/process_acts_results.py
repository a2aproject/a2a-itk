#!/usr/bin/env python3
"""ACTS conformance metrics processor.

The ACTS counterpart of :mod:`scripts.process_results`: takes one §13 report,
fetches the rolling history from the ``nightly-metrics`` release asset,
appends a run entry and writes the updated file back for the workflow to
re-upload. Same shape, same 50-run window, same refusal to publish an empty
run — a dashboard reading both should not have to learn two idioms.

**A history entry is a tally, not a copy of the report.** Each test
contributes `{id, level, result}` and nothing else. Failure messages, step
breakdowns and assertion paths stay in the per-run report, which the workflow
keeps as a build artifact: the rolling asset is fetched by a browser on every
dashboard load, and 111 tests × 50 runs of failure prose would make it
unusable for the one thing it is for — showing a trend.

**One entry per run, not per transport.** A nightly that exercises jsonrpc,
grpc and rest is *one* run of the SDK at one commit, so it contributes one
entry with the three bindings nested under `results`. Appending three would
make the 50-run window cover ~17 nights instead of 50, and would force every
consumer to re-group by `commit_sha` to answer "how did last night go" — the
question the asset exists to answer. It also keeps the shape parallel to
`itk_<sdk>.json`, where one nightly is likewise one entry.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import pathlib
import sys
import urllib.error
import urllib.request


#: Runs kept in the rolling asset. Shorter than ITK's 50 on purpose: an ACTS
#: entry covers every binding and carries each test's failure detail, so it is
#: an order of magnitude bigger than a traversal entry. Seven keeps the asset
#: a browser fetches on every dashboard load to a few hundred KB, and a week
#: is the window anyone actually reads — "did last night regress" needs
#: yesterday, not last quarter. Raise it with `ACTS_HISTORY_LIMIT`.
DEFAULT_HISTORY_LIMIT = 7
HTTP_STATUS_NOT_FOUND = 404

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def load_report(filepath: str) -> dict:
    """Load one §13 report produced by ``POST /run-acts``."""
    path = pathlib.Path(filepath)
    if not path.exists():
        logger.error('Report file %s not found.', filepath)
        raise SystemExit(1)
    try:
        with path.open(encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        logger.exception('Error loading report JSON')
        raise SystemExit(1) from None


def fetch_existing_history(url: str) -> list:
    """Fetch the rolling history, or start a fresh one if the asset is absent.

    Any error other than 404 exits non-zero: overwriting a real history with
    an empty list because GitHub returned a 503 would silently destroy the
    trend the asset exists to show.
    """
    try:
        with urllib.request.urlopen(url) as response:  # noqa: S310
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        if exc.code == HTTP_STATUS_NOT_FOUND:
            logger.info('No history asset at %s yet; starting a new one.', url)
            return []
        logger.error('Fetching history from %s failed: %s', url, exc)
        raise SystemExit(1) from None
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        logger.error('Fetching history from %s failed: %s', url, exc)
        raise SystemExit(1) from None


_COUNTS = ('total', 'passed', 'failed', 'skipped', 'errors', 'duration_ms')


def compile_tests(report: dict) -> list[dict]:
    """Flatten a report's suites into one record per test.

    Carries *why* a test did not pass — the `failure` detail (message,
    expected, actual, assertion path, failing step) and the `skip_reason` —
    because a history that only says `"result": "fail"` cannot answer the
    question anyone actually brings to it: what changed, and is this the same
    failure as last night.

    Those keys exist only on tests that failed, errored or skipped, so the
    cost falls on the minority of records rather than all of them.

    Deliberately **not** carried: `name`, which is static per test id and
    would be duplicated across every run in the window; `duration_ms`, which
    is noise at this resolution; and `steps`, the per-step breakdown, which is
    the bulkiest field by far and already sits in the per-run report the
    workflow keeps as a build artifact.
    """
    records = []
    for suite in report.get('suites', []):
        for test in suite.get('tests', []):
            record = {
                'id': test['id'],
                'level': test.get('level', 'must'),
                'result': test.get('result', 'error'),
            }
            if test.get('failure'):
                record['failure'] = test['failure']
            if test.get('skip_reason'):
                record['skip_reason'] = test['skip_reason']
            records.append(record)
    return records


def is_conformant(report: dict) -> bool:
    """§12.7: conformant iff every graded ``must`` test passed."""
    must = ((report.get('summary') or {}).get('by_level') or {}).get('must') or {}
    return not must.get('failed') and not must.get('errors')


def compile_transport(report: dict) -> dict:
    """One binding's slice of a run entry."""
    summary = report.get('summary') or {}
    return {
        'conformant': is_conformant(report),
        'summary': {k: summary.get(k, 0) for k in _COUNTS},
        'by_level': summary.get('by_level') or {},
        'tests': compile_tests(report),
    }


def build_run(reports: list[dict]) -> dict:
    """One history entry covering every binding this run exercised.

    ``conformant`` at the top is the conjunction: an SDK that passes over
    JSON-RPC and fails over gRPC is not conformant, and a dashboard should not
    have to compute that from three rows to find out.
    """
    if not reports:
        raise ValueError('build_run needs at least one report')

    first = reports[0]
    results = {r.get('transport', 'unknown'): compile_transport(r) for r in reports}

    totals = {k: 0 for k in _COUNTS}
    for slice_ in results.values():
        for key in _COUNTS:
            totals[key] += slice_['summary'].get(key, 0)

    return {
        'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'commit_sha': os.environ.get('GITHUB_SHA', 'local-dev'),
        'github_run_id': os.environ.get('GITHUB_RUN_ID', '0'),
        'sdk': (first.get('sdk') or {}).get('name', 'unknown'),
        'sdk_version': (first.get('sdk') or {}).get('version', 'unknown'),
        'acts_version': first.get('acts_version', ''),
        'spec_version': first.get('spec_version', ''),
        'transports': sorted(results),
        # Conformant only if every binding was. Precomputed so a dashboard
        # needn't re-derive the one number the whole suite exists to produce.
        'conformant': all(s['conformant'] for s in results.values()),
        # The sum across bindings — a single trend line. Per-binding detail is
        # under `results`.
        'summary': totals,
        'results': results,
    }


def save_history(filepath: str, history: list) -> None:
    path = pathlib.Path(filepath)
    try:
        with path.open('w', encoding='utf-8') as handle:
            json.dump(history, handle, indent=2)
        logger.info('Wrote ACTS conformance history to %s', filepath)
    except (OSError, TypeError):
        logger.exception('Error writing history file')
        sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='ACTS conformance metrics processor.')
    parser.add_argument('--report-file', action='append', dest='report_files',
                        metavar='PATH', required=True,
                        help='A §13 report from POST /run-acts. Repeat once '
                             'per transport; all of them become one entry.')
    parser.add_argument('--history_output_file', required=True,
                        help='Where to write the updated rolling history.')
    parser.add_argument('--history_url', required=True,
                        help='URL of the existing history release asset.')
    args = parser.parse_args(argv)

    reports = [load_report(path) for path in args.report_files]

    for path, report in zip(args.report_files, reports, strict=True):
        if not compile_tests(report):
            # Belt and braces alongside acts_report.validate: publishing a run
            # with no tests would push a real entry off the rolling window.
            logger.error('%s contains no tests; refusing to publish.', path)
            raise SystemExit(1)

    seen = [r.get('transport') for r in reports]
    if len(set(seen)) != len(seen):
        logger.error('Two reports claim the same transport: %s', seen)
        raise SystemExit(1)

    history = fetch_existing_history(args.history_url)
    history.append(build_run(reports))

    limit = int(os.environ.get('ACTS_HISTORY_LIMIT', str(DEFAULT_HISTORY_LIMIT)))
    if len(history) > limit:
        history = history[-limit:]
        logger.info('Pruned history to the last %d entries.', limit)

    save_history(args.history_output_file, history)
    return 0


if __name__ == '__main__':
    sys.exit(main())
