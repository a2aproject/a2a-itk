"""ITK integration test orchestrator.

Loads test scenarios from a YAML file and runs them sequentially against a
freshly started cluster of SDK agents. The default scenarios file is
``scenarios.yaml`` next to this script. Consuming SDK repositories can point
the runner at their own scenarios file via ``--scenarios``.
"""

import argparse
import asyncio
import collections
import logging
import pathlib
import sys

import yaml
from itk_service import TestCase, _run_test_case
from testlib import start_itk_cluster, stop_itk_cluster


logging.basicConfig(
    level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

DEFAULT_SCENARIOS_PATH = (
    pathlib.Path(__file__).parent / 'scenarios.yaml'
)


def load_scenarios(path: pathlib.Path) -> list[TestCase]:
    """Load and validate a scenarios file.

    The file is expected to be a YAML mapping with a top-level
    ``tests:`` list, each entry of which conforms to :class:`TestCase`
    (either the multi-step or legacy flat form). On any failure
    (missing file, YAML parse error, wrong shape, schema violation, or
    duplicate test names) the runner logs the cause and exits with
    status 1 instead of raising an unhandled exception.
    """
    if not path.exists():
        logger.error('Scenarios file not found: %s', path)
        sys.exit(1)
    try:
        with path.open() as f:
            data = yaml.safe_load(f)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            logger.error(
                'Scenarios file %s must contain a YAML mapping at the '
                'top level; got %s.',
                path,
                type(data).__name__,
            )
            sys.exit(1)
        raw_tests = data.get('tests')
        if not isinstance(raw_tests, list) or not raw_tests:
            logger.error(
                "Scenarios file %s must contain a non-empty 'tests' list.",
                path,
            )
            sys.exit(1)
        tests = [TestCase.model_validate(entry) for entry in raw_tests]
        name_counts = collections.Counter(t.name for t in tests)
        duplicates = sorted(n for n, c in name_counts.items() if c > 1)
        if duplicates:
            logger.error(
                'Duplicate test case names in %s: %s',
                path,
                ', '.join(duplicates),
            )
            sys.exit(1)
        return tests
    except SystemExit:
        raise
    except Exception:
        logger.exception('Failed to parse scenarios from %s', path)
        sys.exit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Run ITK integration test scenarios.'
    )
    parser.add_argument(
        '--scenarios',
        type=pathlib.Path,
        default=DEFAULT_SCENARIOS_PATH,
        help=(
            'Path to the scenarios YAML file '
            '(default: scenarios.yaml next to this script).'
        ),
    )
    return parser.parse_args()


async def main_async(scenarios_path: pathlib.Path) -> int:
    """Execute scenarios sequentially; returns a shell-style exit code."""
    test_cases = load_scenarios(scenarios_path)
    logger.info(
        'Loaded %d scenario(s) from %s', len(test_cases), scenarios_path
    )

    # 1. Identify all unique SDKs needed across all steps of all tests.
    sdk_set: set[str] = set()
    for case in test_cases:
        sdk_set.update(case.all_sdks())
    sdk_list = sorted(sdk_set)

    # 2. Start the shared cluster.
    procs, _uris, ports = await start_itk_cluster(sdk_list)

    try:
        # 3. Run all scenarios sequentially to prevent overwhelming the
        # shared cluster.
        logger.info('Starting sequential scenario execution...')
        merged_results: dict[str, dict] = {}
        for case in test_cases:
            res_dict = await _run_test_case(case)
            merged_results.update(res_dict)

        # 4. Report results.
        all_passed = True
        for idx, (name, details) in enumerate(merged_results.items()):
            passed = details['passed']
            status = 'PASSED' if passed else 'FAILED'
            note = ''
            if details.get('expected') == 'fail':
                note = ' (expected to fail)'
            logger.info(
                "Scenario %s/%s '%s': %s%s",
                idx + 1,
                len(merged_results),
                name,
                status,
                note,
            )
            if not passed:
                all_passed = False

        if not all_passed:
            logger.error('One or more test scenarios failed.')
            return 1
        logger.info('All test scenarios passed.')
        return 0
    except Exception:
        logger.exception('Concurrent test execution encountered an error.')
        return 1
    finally:
        logger.info('Decommissioning shared agent cluster...')
        stop_itk_cluster(procs, ports)


def main() -> None:
    """Entry point for the integration test orchestrator."""
    args = parse_args()
    sys.exit(asyncio.run(main_async(args.scenarios)))


if __name__ == '__main__':
    main()
