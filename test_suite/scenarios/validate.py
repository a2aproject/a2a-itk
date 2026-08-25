"""Validate scenario files, before a run rather than during one.

Without this a malformed scenario is discovered when the service rejects the
POST — by which point CI has already built the image and started peers, and
the failure reads as an infrastructure problem rather than a typo. Worse, an
entry that parses but resolves to nothing would drop silently: the run goes
green having tested less than it claimed.

Usage::

    uv run python -m test_suite.scenarios.validate scenarios/
    uv run python -m test_suite.scenarios.validate a2a-python/itk/scenarios.json
    uv run python -m test_suite.scenarios.validate --resolve scenarios/traversal/

``--resolve`` additionally binds roles against ``matrix.yaml`` and reports
how many executable scenarios each file expands to, which is what catches a
peer that no longer exists in the matrix.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from test_suite.scenarios.loader import ScenarioFileError, load_file
from test_suite.scenarios.schema import TraversalScenarioV1


_SUFFIXES = {'.json', '.yaml', '.yml'}


def collect(paths: list[Path]) -> list[Path]:
    """Expand directories to the scenario files inside them, sorted."""
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(
                sorted(f for f in p.rglob('*') if f.suffix in _SUFFIXES)
            )
        else:
            files.append(p)
    return files


def _describe(path: Path, resolve: bool) -> tuple[bool, str]:
    """Validate one file. Returns (ok, one-line report)."""
    try:
        scenarios = load_file(path)
    except ScenarioFileError as e:
        return False, f'FAIL  {path}\n      {e}'

    if not scenarios:
        return False, f'FAIL  {path}\n      contains no scenarios'

    n_new = sum(isinstance(s, TraversalScenarioV1) for s in scenarios)
    kinds = f'{n_new} traversal/v1, {len(scenarios) - n_new} legacy'

    if not resolve:
        return True, f'ok    {path}  ({kinds})'

    # Imported lazily: resolution needs matrix.yaml, and plain validation
    # should stay usable without one.
    from test_suite.launcher.matrix import Matrix, MatrixError  # noqa: PLC0415
    from test_suite.scenarios.resolver import ResolutionError, resolve  # noqa: PLC0415

    try:
        matrix = Matrix.from_default()
        expanded = resolve(scenarios, matrix)
    except (MatrixError, ResolutionError) as e:
        return False, f'FAIL  {path}\n      {e}'

    if not expanded:
        return False, (
            f'FAIL  {path}\n      resolves to 0 executable scenarios '
            f'(every entry filtered out — check test_when and transports)'
        )
    return True, f'ok    {path}  ({kinds}) -> {len(expanded)} executable'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        'paths', nargs='+', type=Path,
        help='Scenario files, or directories to search.',
    )
    parser.add_argument(
        '--resolve', action='store_true',
        help='Also bind roles against matrix.yaml and report the expanded '
             'scenario count.',
    )
    args = parser.parse_args(argv)

    files = collect(args.paths)
    if not files:
        print('No scenario files found.', file=sys.stderr)
        return 1

    failures = 0
    for path in files:
        ok, report = _describe(path, args.resolve)
        failures += not ok
        print(report)

    print(f'\n{len(files) - failures}/{len(files)} file(s) valid.')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
