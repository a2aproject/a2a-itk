#!/usr/bin/env python3
"""ITK Compatibility Metrics Processor.

Compiles test outcomes from raw JSON results, retrieves and aggregates historical
runs from GitHub Release assets, and outputs the updated historical metrics log.
"""

import argparse
import datetime
import json
import logging
import os
import pathlib
import sys
import urllib.error
import urllib.request


# --- CONSTANTS ---
RESULTS_FILE = 'raw_results.json'
DEFAULT_HISTORY_LIMIT = 50

HTTP_STATUS_OK = 200
HTTP_STATUS_NOT_FOUND = 404

# Configure logging to match standard ITK formatting
logging.basicConfig(
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def load_raw_results(filepath: str) -> dict:
    """Loads the raw compatibility results from raw_results.json."""
    path = pathlib.Path(filepath)
    if not path.exists():
        logger.error('Results file %s not found.', filepath)
        raise SystemExit(1)

    try:
        with path.open() as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.exception('Error loading results JSON')
        raise SystemExit(1) from None


def fetch_existing_history(url: str) -> list:
    """Fetches the existing compatibility history from the GitHub release asset.

    If the asset does not exist (HTTP 404), a fresh empty history list is returned.
    For all other network or server errors, the script exits with a non-zero status
    to prevent overwriting and losing historical metrics.
    """
    try:
        req = urllib.request.Request(  # noqa: S310
            url, headers={'User-Agent': 'Mozilla/5.0'}
        )
        with urllib.request.urlopen(req, timeout=15) as response:  # noqa: S310
            if response.status == HTTP_STATUS_OK:
                history = json.loads(response.read().decode('utf-8'))
                logger.info(
                    'Successfully retrieved history. Current entries: %d',
                    len(history),
                )
                return history
            logger.error(
                'Unexpected HTTP status when downloading existing history: %d',
                response.status,
            )
            raise SystemExit(1)  # noqa: TRY301
    except urllib.error.HTTPError as e:
        if e.code == HTTP_STATUS_NOT_FOUND:
            logger.warning(
                'No existing history found (HTTP %d). Initializing fresh history.',
                e.code,
            )
            return []
        logger.exception(
            'HTTP error downloading existing history: %d. Aborting to preserve metrics.',
            e.code,
        )
        raise SystemExit(1) from None
    except Exception:
        logger.exception(
            'Failed to download existing history. Aborting to preserve metrics.'
        )
        raise SystemExit(1) from None


def load_scenarios(filepath: str, required: bool = True) -> list:
    """Loads the list of tests from the scenarios.json definitions.

    Only needed as a fallback now: a result carrying its own metadata (see
    :func:`build_record`) doesn't need looking up here at all. When every
    result is self-describing the file may legitimately be absent — a run
    driven by a shared scenario set has no local ``scenarios.json`` — so
    ``required=False`` degrades to an empty list instead of exiting.
    """
    path = pathlib.Path(filepath)
    if not path.exists():
        if not required:
            logger.info(
                'Scenarios file %s not found; relying on result metadata.',
                filepath,
            )
            return []
        logger.error('Scenarios file %s not found.', filepath)
        raise SystemExit(1)

    try:
        with path.open() as f:
            data = json.load(f)
        return data['tests']
    except (OSError, json.JSONDecodeError, KeyError):
        logger.exception('Failed to load scenarios.json definitions')
        raise SystemExit(1) from None


# Metadata the service now returns per result. Its presence is what lets a
# record be built without matching the result name back to a scenario file.
_SELF_DESCRIBING_KEYS = ('protocols', 'behavior')


def is_self_describing(details: object) -> bool:
    """Does this result carry its own scenario metadata?"""
    return isinstance(details, dict) and any(
        details.get(k) is not None for k in _SELF_DESCRIBING_KEYS
    )


def build_record(name: str, details: dict, base: dict | None) -> dict:
    """Compile one history record from a result and, if needed, its scenario.

    Prefers metadata carried on the result. ``base`` is the scenario file
    entry, used only for results produced before the service returned
    metadata.

    The dashboard's record shape is unchanged — an explicit non-goal of this
    work is altering what it ingests.
    """
    base = base or {}

    def pick(key: str, default=None):
        value = details.get(key)
        return base.get(key, default) if value is None else value

    record = {
        'name': name,
        'sdks': details.get('sdks') or base.get('sdks', []),
        'edges': pick('edges'),
        'protocols': pick('protocols'),
        'behavior': pick('behavior'),
        'traversal': base.get('traversal', 'euler'),
        'passed': bool(details.get('passed', False)),
    }
    if 'streaming' in details or 'streaming' in base:
        record['streaming'] = details.get('streaming', base.get('streaming'))
    if 'build_subtests' in base:
        record['build_subtests'] = base['build_subtests']
    return record


def save_history(filepath: str, history: list) -> None:
    """Saves the updated history back to disk as a release asset candidate."""
    path = pathlib.Path(filepath)
    try:
        with path.open('w') as f:
            json.dump(history, f, indent=2)
        logger.info(
            'Successfully compiled and wrote nightly history to: %s',
            filepath,
        )
    except (OSError, TypeError):
        logger.exception('Error writing history file')
        sys.exit(1)


def main() -> None:
    """Orchestrates nightly ITK metrics processing and compiles rolling history."""
    parser = argparse.ArgumentParser(description='ITK Compatibility Metrics Processor.')
    parser.add_argument('--history_output_file', required=True, help='Path to the output JSON file for historical metrics.')
    parser.add_argument('--history_url', required=True, help='URL to fetch the existing historical metrics JSON.')
    parser.add_argument(
        '--scenarios',
        help='Scenario definitions used for this run. Only consulted for '
             'results that lack their own metadata; defaults to '
             'scenarios{,_full}.json in the working directory.',
    )
    args = parser.parse_args()

    history_output_file = args.history_output_file
    history_url = args.history_url

    # 1. Load raw compatibility results
    data = load_raw_results(RESULTS_FILE)
    all_passed = data.get('all_passed', False)
    results = data.get('results', {})

    # 2. Fetch existing history from rolling release
    history = fetch_existing_history(history_url)

    # 3. Load scenarios only as a fallback. Results from a current service
    # carry their own metadata; the file is needed just for older ones.
    self_describing = all(is_self_describing(d) for d in results.values())
    scenarios_file = args.scenarios or (
        'scenarios_full.json'
        if os.environ.get('ITK_NIGHTLY_RUN', '').lower() == 'true'
        else 'scenarios.json'
    )
    base_scenarios = load_scenarios(scenarios_file, required=not self_describing)
    by_name = {b['name']: b for b in base_scenarios if 'name' in b}

    if not results:
        # Belt and braces alongside itk_report.validate: publishing a run with
        # no scenarios can push a real entry off the rolling window.
        logger.error('No results to record; refusing to publish an empty run.')
        raise SystemExit(1)

    compiled_scenarios = []
    dropped = []
    for name, details in results.items():
        # A bare bool predates the structured result shape.
        if isinstance(details, bool):
            details = {'passed': details}
        elif not isinstance(details, dict):
            dropped.append((name, f'unusable result type {type(details).__name__}'))
            continue

        # Subtests are named "<parent>-sub-<agents>" and share the parent's
        # definition.
        base = by_name.get(name) or by_name.get(name.split('-sub-')[0])

        if base is None and not is_self_describing(details):
            dropped.append((name, 'no metadata on the result and no matching scenario'))
            continue

        compiled_scenarios.append(build_record(name, details, base))

    # Dropping a scenario silently is how a renamed or generated scenario
    # used to vanish from the published history while the run stayed green.
    # Refuse to publish a partial history instead.
    if dropped:
        for name, why in dropped:
            logger.error('Cannot record result %r: %s', name, why)
        logger.error(
            'Refusing to publish history missing %d of %d result(s). '
            'Re-run against a service that returns scenario metadata, or '
            'pass --scenarios pointing at the definitions used for this run.',
            len(dropped), len(results),
        )
        raise SystemExit(1)

    # 4. Compile new run metadata
    new_run = {
        'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'commit_sha': os.environ.get('GITHUB_SHA', 'local-dev'),
        'github_run_id': os.environ.get('GITHUB_RUN_ID', '0'),
        'all_passed': all_passed,
        'scenarios': compiled_scenarios,
    }

    # 5. Merge and Prune rolling window
    history.append(new_run)
    history_limit = int(
        os.environ.get('ITK_HISTORY_LIMIT', str(DEFAULT_HISTORY_LIMIT))
    )
    if len(history) > history_limit:
        history = history[-history_limit:]
        logger.info('Pruned history to last %d entries.', history_limit)

    # 6. Save candidates back to disk
    save_history(history_output_file, history)
    sys.exit(0)


if __name__ == '__main__':
    main()
