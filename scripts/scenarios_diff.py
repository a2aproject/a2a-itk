#!/usr/bin/env python3
"""Check the shared scenario set still covers each SDK's legacy set.

The acceptance condition is that migrating a repo onto the shared
scenarios doesn't quietly test less than its own file did. Comparing the two
files scenario-by-scenario doesn't answer that, because the merge deliberately
reshapes them: five repos wrote the same test five different ways, several
scenarios collapse into one declaration, and one scenario may now carry three
transports where a repo's version carried one.

So the comparison is at the level of what actually gets exercised. Running a
scenario walks an Eulerian circuit over its edges, once per transport, so the
unit of coverage is::

    (caller, callee, transport, behavior, streaming)

Every such tuple in the legacy set must appear in the new one. This gets the
reshaping right by construction:

  * a scenario over ``[jsonrpc, grpc, http_json]`` covers the repo that only
    listed ``[jsonrpc]``, because it walks the circuit for each;
  * a star over nine agents covers a star over five of them, since a star's
    edges are all SUT-to-peer;
  * a pairwise scenario covers the same pair inside someone else's star.

Coverage may grow — that is the point of merging five sets — so extra tuples
are reported and never fail. Coverage may only shrink for a stated reason:
either the peer cannot speak that transport (``matrix.yaml``) or the
combination is a recorded known failure (``known_failures.yaml``). A hop that
disappears for neither reason fails the check.

Usage::

    scripts/scenarios_diff.py --old a2a-python/itk/scenarios.json \\
                              --new scenarios/traversal/pr.yaml
    scripts/scenarios_diff.py --old a2a-go/itk/scenarios_full.json \\
                              --new scenarios/traversal/nightly.yaml

Exit codes: 0 when nothing is lost, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from test_suite.launcher.matrix import Matrix, MatrixError
from test_suite.scenarios.exclusions import KnownFailures
from test_suite.transports import TRANSPORT_ORDER
from test_suite.scenarios.loader import ScenarioFileError, load_file
from test_suite.scenarios.resolver import ResolutionError, ResolvedScenario, resolve

# (caller, callee, transport, behavior, streaming)
Atom = tuple[str, str, str, str, bool]


def atoms(s: ResolvedScenario) -> set[Atom]:
    """Every hop this scenario exercises, once per transport."""
    pairs = _edge_pairs(s)
    transports = s.protocols or list(TRANSPORT_ORDER)
    return {
        (caller, callee, t, s.behavior, bool(s.streaming))
        for caller, callee in pairs
        for t in transports
    }


def _edge_pairs(s: ResolvedScenario) -> set[tuple[str, str]]:
    """Resolve index edges to agent pairs; no edges means a complete digraph."""
    if s.edges is None:
        return {(u, v) for u in s.sdks for v in s.sdks if u != v}

    pairs = set()
    for edge in s.edges:
        raw_u, _, raw_v = edge.partition('->')
        u, v = int(raw_u.strip()), int(raw_v.strip())
        pairs.add((s.sdks[u], s.sdks[v]))
    return pairs


def describe(a: Atom) -> str:
    caller, callee, transport, behavior, streaming = a
    return (
        f'{caller} -> {callee}  {transport}  {behavior}'
        f'{" streaming" if streaming else ""}'
    )


def load(
    paths: list[Path],
    matrix: Matrix,
    sut_sdk: str | None,
    known_failures: KnownFailures | None = None,
) -> list[ResolvedScenario]:
    scenarios: list = []
    for p in paths:
        scenarios.extend(load_file(p))
    return resolve(
        scenarios, matrix, sut_sdk=sut_sdk, known_failures=known_failures,
    )


def explain_drop(
    a: Atom, matrix: Matrix, known: KnownFailures, sut_sdk: str | None = None
) -> str | None:
    """Why this hop is legitimately no longer tested, or None if it isn't.

    Two acceptable reasons, both written down somewhere a reader can check:
    the peer has no such transport, or the combination is a known failure.
    """
    caller, callee, transport, behavior, streaming = a

    for agent in (caller, callee):
        try:
            entry = matrix.resolve(agent)
        except MatrixError:
            continue  # 'current' and friends aren't matrix lines
        if transport not in entry.transports:
            return (
                f'matrix.yaml: {agent} does not speak {transport} '
                f'(has {sorted(entry.transports)})'
            )

    hit = known.find(
        sdks=[caller, callee], protocols=[transport],
        behavior=behavior, streaming=streaming, sut_sdk=sut_sdk,
    )
    if hit is not None:
        return f'known_failures.yaml: {hit.describe()}'
    return None


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old', nargs='+', type=Path, required=True,
                        help='The legacy scenario file(s) being replaced.')
    parser.add_argument('--new', nargs='+', type=Path, required=True,
                        help='The shared scenario file(s) replacing them.')
    parser.add_argument('--sut-sdk',
                        help='SUT, for evaluating test_when on the new set.')
    parser.add_argument('--show-extra', action='store_true',
                        help='List the added coverage as well as summarising it.')
    parser.add_argument('--ignore-known-failures', action='store_true',
                        help='Resolve the new set as if known_failures.yaml '
                             'were empty, to see its full potential coverage.')
    args = parser.parse_args(argv)

    matrix = Matrix.from_default()
    known = KnownFailures() if args.ignore_known_failures else KnownFailures.from_default()
    try:
        # The legacy side is resolved with exclusions off: it is the baseline
        # of what used to run, and applying today's exclusions to it would
        # hide exactly the drops this tool exists to surface.
        old = load(args.old, matrix, None, KnownFailures())
        new = load(args.new, matrix, args.sut_sdk, known)
    except (ScenarioFileError, ResolutionError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 1

    old_atoms: set[Atom] = set().union(*(atoms(s) for s in old)) if old else set()
    new_atoms: set[Atom] = set().union(*(atoms(s) for s in new)) if new else set()

    missing = old_atoms - new_atoms
    extra = new_atoms - old_atoms

    print(f'old: {len(old):>3} scenarios -> {len(old_atoms):>4} hops covered')
    print(f'new: {len(new):>3} scenarios -> {len(new_atoms):>4} hops covered')
    print(f'retained: {len(old_atoms) - len(missing)}/{len(old_atoms)}')

    explained: list[tuple[Atom, str]] = []
    unexplained: list[Atom] = []
    for a in sorted(missing):
        why = explain_drop(a, matrix, known, args.sut_sdk)
        (explained.append((a, why)) if why else unexplained.append(a))

    if explained:
        by_reason: dict[str, int] = defaultdict(int)
        for _, why in explained:
            by_reason[why] += 1
        print(f'\nDROPPED for a stated reason ({len(explained)} hops):')
        for why, n in sorted(by_reason.items()):
            print(f'  ~ {why}  ({n} hops)')

    if unexplained:
        print(f'\nMISSING — exercised by the legacy set, not by the new one, '
              f'and nothing says why ({len(unexplained)}):')
        for a in unexplained:
            print(f'  - {describe(a)}')

    if extra:
        by_pair: dict[tuple[str, str], int] = defaultdict(int)
        for caller, callee, *_ in extra:
            by_pair[(caller, callee)] += 1
        print(f'\nEXTRA — added coverage, not an error ({len(extra)} hops):')
        for (caller, callee), n in sorted(by_pair.items()):
            print(f'  + {caller} -> {callee}  ({n} hops)')
        if args.show_extra:
            for a in sorted(extra):
                print(f'      {describe(a)}')

    if unexplained:
        print(f'\nFAIL: {len(unexplained)} hop(s) would stop being tested with '
              f'no reason recorded. Either restore them, or record why in '
              f'matrix.yaml (capability) or known_failures.yaml (defect).')
        return 1

    summary = 'OK: all legacy coverage retained'
    if explained:
        summary = f'OK: {len(explained)} hop(s) dropped for stated reasons'
    print(f'\n{summary}' + (f', plus {len(extra)} new hops.' if extra else '.'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
