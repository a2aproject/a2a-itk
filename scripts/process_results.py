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


def load_scenarios(filepath: str) -> list:
    """Loads the list of tests from the scenarios.json definitions."""
    path = pathlib.Path(filepath)
    if not path.exists():
        logger.error('Scenarios file %s not found.', filepath)
        raise SystemExit(1)

    try:
        with path.open() as f:
            data = json.load(f)
        return data['tests']
    except (OSError, json.JSONDecodeError, KeyError):
        logger.exception('Failed to load scenarios.json definitions')
        raise SystemExit(1) from None


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
    args = parser.parse_args()

    history_output_file = args.history_output_file
    history_url = args.history_url

    # 1. Load raw compatibility results
    data = load_raw_results(RESULTS_FILE)
    all_passed = data.get('all_passed', False)
    results = data.get('results', {})

    # 2. Fetch existing history from rolling release
    history = fetch_existing_history(history_url)

    # 3. Load scenarios list for base metadata
    scenarios_file = (
        'scenarios_full.json'
        if os.environ.get('ITK_NIGHTLY_RUN', '').lower() == 'true'
        else 'scenarios.json'
    )
    base_scenarios = load_scenarios(scenarios_file)
    # Merge definitions with current outcomes dynamically
    compiled_scenarios = []
    for name, details in results.items():
        # Extract the parent scenario name cleanly by splitting on the subtest suffix
        parent_name = name.split('-sub-')[0]

        # Find the matching base scenario with an EXACT match!
        matched_base = None
        for base in base_scenarios:
            if parent_name == base['name']:
                matched_base = base
                break

        if not matched_base:
            logger.warning(
                'No matching base scenario found for result key: %s', name
            )
            continue

        # Build the metadata-rich scenario record
        passed = False
        sdks = matched_base.get('sdks', [])
        edges = matched_base.get('edges')

        if isinstance(details, dict):
            passed = details.get('passed', False)
            sdks = details.get('sdks', sdks)
            edges = details.get('edges', edges)
        elif isinstance(details, bool):
            passed = details

        record = {
            'name': name,
            'sdks': sdks,
            'edges': edges,
            'protocols': matched_base.get('protocols'),
            'behavior': matched_base.get('behavior'),
            'traversal': matched_base.get('traversal', 'euler'),
            'passed': passed,
        }
        if 'streaming' in matched_base:
            record['streaming'] = matched_base['streaming']
        if 'build_subtests' in matched_base:
            record['build_subtests'] = matched_base['build_subtests']

        compiled_scenarios.append(record)

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
