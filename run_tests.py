#!/usr/bin/env python3
"""Run ITK scenarios locally, without a container and without an SDK checkout.

Every peer is fetched from its own repository at the ref pinned in
``matrix.yaml``, so a scenario that doesn't name ``current`` needs nothing
but this repo::

    uv run run_tests.py                              # the bundled smoke set
    uv run run_tests.py --scenarios path/to/x.json   # any SDK's scenarios.json
    uv run run_tests.py --scenarios scenarios/traversal/smoke.yaml
    uv run run_tests.py --sdks python_v10,go_v10     # narrow to those peers

To test a local SDK checkout as the code under test, point ``current`` at it::

    uv run run_tests.py --mount ~/Source/a2a-python/itk

This shares its whole pipeline with the ``/run`` HTTP handler (see
:mod:`itk_runner`), so a scenario behaves identically here and in CI.

Caveat: builds happen on your machine with each SDK's native toolchain, so
you need whatever the selected peers require — uv for python, go for go,
cargo for rust, mvn+JDK for java, npm for ts. Builds are cached under
``$ITK_CACHE_DIR`` (default ``~/.cache/a2a-itk``), so the second run of the
same peer is fast.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import itk_runner
from itk_runner import SUT_ID, ClusterStartupError, Scenario
from test_suite.launcher import InfraFailure, PermanentError
from test_suite.launcher.matrix import MatrixError
from test_suite.scenarios.loader import ScenarioFileError
from test_suite.scenarios.resolver import ResolutionError


logging.basicConfig(
    level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

_DEFAULT_SCENARIOS = Path(__file__).parent / 'scenarios' / 'smoke.json'


# ---------------------------------------------------------------------------
# Scenario loading
# ---------------------------------------------------------------------------


def load_scenarios(path: Path, sut_sdk: str | None = None) -> list[Scenario]:
    """Load a scenario file in either schema and bind it to concrete agents.

    Takes an SDK's ``scenarios.json`` unchanged, or a ``traversal/v1``
    YAML/JSON file, or one holding a mixture. Goes through
    :func:`itk_runner.prepare_file`, so the CLI reports skipped and trimmed
    scenarios exactly as the service does.

    Raises:
        SystemExit: The file is missing, malformed, or names a peer the
            matrix doesn't have. These are user input errors, so they exit
            with a readable message rather than a traceback.
    """
    try:
        scenarios = itk_runner.prepare_file(path, sut_sdk=sut_sdk)
    except (ScenarioFileError, ResolutionError) as e:
        sys.exit(str(e))

    if not scenarios:
        sys.exit(
            f'{path}: no scenarios left to run'
            + (f' for --sut-sdk {sut_sdk}' if sut_sdk else '')
        )
    return scenarios


def filter_by_sdks(
    scenarios: list[Scenario], selected: set[str] | None
) -> list[Scenario]:
    """Keep only scenarios whose agents are all in ``selected``.

    All-or-nothing per scenario: a partial cluster would change what the
    scenario actually tests, so a scenario naming an excluded peer is
    dropped rather than trimmed.
    """
    if selected is None:
        return scenarios
    return [s for s in scenarios if all(sdk in selected for sdk in s.sdks)]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog='run_tests.py',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--scenarios', type=Path, default=_DEFAULT_SCENARIOS,
        help=f'Scenario file to run (default: {_DEFAULT_SCENARIOS.name}). '
             "Accepts any SDK's scenarios.json / scenarios_full.json, a "
             'traversal/v1 YAML file, or one holding a mixture.',
    )
    parser.add_argument(
        '--sut-sdk', type=str,
        help="Which SDK the mounted checkout is, e.g. 'python'. Only used to "
             "evaluate a scenario's test_when filter; omit to run everything "
             'in the file.',
    )
    parser.add_argument(
        '--sdks', type=str,
        help='Comma-separated agent identifiers to keep, e.g. '
             '"python_v10,go_v10". Scenarios naming anything outside this '
             'set are skipped. Omit to run everything in the file.',
    )
    parser.add_argument(
        '--mount', type=Path,
        help=f"Directory to serve the '{SUT_ID}' agent from, e.g. "
             '~/Source/a2a-python/itk. Required only if a scenario names '
             f"'{SUT_ID}'.",
    )
    parser.add_argument(
        '--log-dir', type=Path,
        help='Capture each agent\'s stdout/stderr to <dir>/agent_<id>.log.',
    )
    parser.add_argument(
        '--output', type=Path,
        help='Write raw results as JSON here (same shape as the /run '
             'response, which scripts/process_results.py consumes).',
    )
    parser.add_argument(
        '--list-sdks', action='store_true',
        help='List the agent identifiers matrix.yaml can resolve, then exit.',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Show which scenarios and agents would run, then exit. Does not '
             'touch the network.',
    )
    return parser.parse_args(argv)


def _print_available_sdks() -> None:
    matrix = itk_runner.get_matrix()
    ids = [f'{sdk}_{line}' for sdk, line in matrix.keys()]
    width = max((len(i) for i in [*ids, SUT_ID]), default=0)
    print('Agent identifiers available from matrix.yaml:')
    for agent_id in ids:
        entry = matrix.resolve(agent_id)
        print(f'  {agent_id:<{width}}  {entry.repo} @ {entry.ref}')
    print(f'  {SUT_ID:<{width}}  whatever --mount points at (the code under test)')
    print(
        '\nAppend _2 to any identifier to run a second, independently-ported '
        'instance of it (e.g. python_v10_2).'
    )


def _report(results: dict[str, itk_runner.ScenarioResult]) -> bool:
    width = max((len(n) for n in results), default=0)
    passed_count = 0
    for i, (name, r) in enumerate(sorted(results.items()), start=1):
        status = 'PASS' if r.passed else 'FAIL'
        passed_count += r.passed
        logger.info('[%d/%d] %-*s %s', i, len(results), width, name, status)
    all_passed = passed_count == len(results)
    logger.info(
        '%d/%d scenarios passed — %s',
        passed_count, len(results), 'OK' if all_passed else 'FAILURES',
    )
    return all_passed


async def main_async(args: argparse.Namespace) -> int:
    scenarios = load_scenarios(args.scenarios, args.sut_sdk)
    total = len(scenarios)

    selected = None
    if args.sdks:
        selected = {s.strip() for s in args.sdks.split(',') if s.strip()}
        scenarios = filter_by_sdks(scenarios, selected)

    if not scenarios:
        logger.error(
            'No scenarios left after filtering %d by --sdks=%s. '
            'Run --list-sdks to see valid identifiers.',
            total, args.sdks,
        )
        return 1
    if len(scenarios) < total:
        logger.info('Running %d/%d scenarios (filtered by --sdks)', len(scenarios), total)

    needed = sorted({sdk for s in scenarios for sdk in s.sdks})

    # Fail before any network or build work if the SUT is needed but absent.
    if SUT_ID in needed:
        if args.mount is None:
            logger.error(
                "Scenarios reference '%s' (the code under test) but --mount "
                'was not given. Point it at an SDK\'s itk/ directory, or use '
                '--sdks to select scenarios that only use fetched peers.',
                SUT_ID,
            )
            return 1
        mount = args.mount.expanduser().resolve()
        if not mount.is_dir():
            logger.error('--mount path is not a directory: %s', mount)
            return 1
        # How launcher.config.mount_dir() picks up the override.
        os.environ['ITK_MOUNT_DIR'] = str(mount)
        logger.info("Serving '%s' from %s", SUT_ID, mount)
    elif args.mount is not None:
        logger.warning(
            "--mount given but no scenario references '%s'; ignoring it.", SUT_ID,
        )

    if args.dry_run:
        print(f'Would start {len(needed)} agent(s): {", ".join(needed)}')
        print(f'Would run {len(scenarios)} scenario(s):')
        for s in scenarios:
            print(f'  {s.name}  [{", ".join(s.sdks)}]  {s.behavior}')
        return 0

    try:
        report = await itk_runner.run_scenarios(scenarios, log_dir=args.log_dir)
    except (MatrixError, PermanentError) as e:
        logger.error('%s', e)  # noqa: TRY400 — a traceback adds nothing here
        return 1
    except (InfraFailure, ClusterStartupError) as e:
        logger.error('%s', e)  # noqa: TRY400
        if args.log_dir is None:
            logger.error('Re-run with --log-dir to capture agent output.')
        return 1

    # A dropped peer already logged loudly inside run_scenarios; nothing more
    # to do here but run what survived.
    results = report.results

    if args.output:
        payload = {
            'all_passed': all(r.passed for r in results.values()),
            'results': {
                n: {'passed': r.passed, 'sdks': r.sdks, 'edges': r.edges}
                for n, r in results.items()
            },
        }
        if report.dropped_peers:
            payload['startup'] = {
                'dropped_peers': report.dropped_peers,
                'trimmed': [{'name': n, 'dropped': d} for n, d in report.trimmed],
                'skipped': [{'name': n, 'missing': m} for n, m in report.skipped],
            }
        args.output.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        logger.info('Results written to %s', args.output)

    return 0 if _report(results) else 1


def main() -> None:
    args = parse_args()
    if args.list_sdks:
        _print_available_sdks()
        sys.exit(0)
    sys.exit(asyncio.run(main_async(args)))


if __name__ == '__main__':
    main()
