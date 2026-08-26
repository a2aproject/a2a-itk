"""Scenario execution pipeline, shared by the service and the CLI.

Given a list of :class:`Scenario` objects this module does the whole job:
resolve each agent identifier to a :class:`TargetSpec`, start one shared
cluster, run every scenario against it, and tear the cluster down.

Two front ends call it and neither owns any pipeline logic of its own:

  * ``itk_service_v2.py`` — the HTTP ``/run`` handler every SDK's
    ``run_itk.sh`` POSTs to. Maps the errors raised here onto status codes.
  * ``run_tests.py`` — the local CLI, for running scenarios on your own
    machine without standing up a container or checking out an SDK.

Keeping the pipeline in one place is the point: a second copy would drift,
and "the local runner and CI disagree" is exactly the class of bug this
whole consolidation exists to remove.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from test_suite.agent_table import AgentTable
from test_suite.launcher import Cluster, TargetSpec
from test_suite.launcher.fetch import resolve_ref
from test_suite.launcher.matrix import Matrix
from test_suite.scenarios.loader import load_file, parse_tests
from test_suite.scenarios.resolver import (
    ResolutionReport,
    ResolvedScenario,
    resolve_all,
)
from test_suite.launcher.spec import Kind
from test_suite import induced_runnable_scenario
from testlib import execute_itk_test


logger = logging.getLogger(__name__)


# The SUT identifier. Unlike every other id it doesn't map through
# matrix.yaml — it's whatever is sitting at config.mount_dir().
SUT_ID = 'current'


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


# Everything below this point sees only this shape, never which schema it
# came from.
Scenario = ResolvedScenario


@dataclass(frozen=True)
class ScenarioResult:
    """One scenario's outcome, plus enough of its definition to record it.

    The metadata rides along so the nightly metrics processor needn't recover
    it by matching the result name back to a scenario file — a lookup that
    silently dropped anything it couldn't match.
    """

    passed: bool
    sdks: list[str]
    edges: list[str] | None = None
    protocols: list[str] | None = None
    behavior: str | None = None
    streaming: bool = False
    tier: str | None = None


@dataclass(frozen=True)
class RunReport:
    """Everything a run produced: the results, and what a down peer cost.

    ``results`` holds only scenarios that actually ran (as authored, or with
    a failed peer trimmed out). The other three record coverage lost to a
    peer that didn't start, so it can be surfaced rather than silently
    dropped — a run that quietly tests less than the file describes and still
    goes green is the failure mode this pipeline exists to prevent.
    """

    results: dict[str, ScenarioResult]
    # agent id -> "<stage>: <error>" for each peer that failed to start.
    dropped_peers: dict[str, str] = field(default_factory=dict)
    # (scenario name, agents removed) for scenarios that ran a peer short.
    trimmed: list[tuple[str, list[str]]] = field(default_factory=list)
    # (scenario name, agents missing) for scenarios that couldn't run at all.
    skipped: list[tuple[str, list[str]]] = field(default_factory=list)


class ClusterStartupError(RuntimeError):
    """The run cannot proceed because a required agent didn't start.

    Raised only when a *peer* being down cannot be tolerated: the SUT
    (``current``) itself failed to start, or every scenario needed a peer
    that failed. A peer failure that leaves at least one scenario runnable
    does **not** raise — the peer is dropped and the run continues (see
    :func:`_select_runnable`).

    Carries per-agent detail so the caller can say *which* agent died and at
    which stage, plus an optional headline naming why it was fatal.
    """

    def __init__(
        self, failures: list[tuple[str, str]], *, summary: str | None = None,
    ) -> None:
        self.failures = failures
        self.summary = summary
        headline = f' ({summary})' if summary else ''
        super().__init__(
            f'Cluster startup failed{headline}: '
            + '; '.join(f'{sid}: {detail}' for sid, detail in failures)
        )


@dataclass
class _Plan:
    """Agent identifiers resolved to launch targets, in a stable order."""

    ids: list[str] = field(default_factory=list)
    specs: list[TargetSpec] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Serialisation of runs
# ---------------------------------------------------------------------------

# A run owns a whole cluster: host ports, the launcher's build cache, and the
# fixed container port the notification server binds. Two at once would
# contend on all three.
_execution_lock = asyncio.Lock()

_matrix: Matrix | None = None


def get_matrix() -> Matrix:
    """Load ``matrix.yaml`` once per process.

    Lazy so a malformed matrix surfaces on the first run rather than
    preventing the service from starting at all (``/health`` stays green
    either way, which is useful when debugging).
    """
    global _matrix
    if _matrix is None:
        _matrix = Matrix.from_default()
    return _matrix


def set_matrix(matrix: Matrix | None) -> None:
    """Override the cached matrix. For tests and for ``--matrix``."""
    global _matrix
    _matrix = matrix


# ---------------------------------------------------------------------------
# Input: either scenario schema -> executable scenarios
# ---------------------------------------------------------------------------


def prepare(raw_tests: object, *, sut_sdk: str | None = None) -> list[Scenario]:
    """Resolve an already-parsed scenario document into runnable scenarios.

    Accepts a ``{"tests": [...]}`` mapping or a bare list, holding legacy
    entries, ``traversal/v1`` entries, or a mixture.

    Args:
        raw_tests: The parsed scenario document.
        sut_sdk: SDK under test, for ``test_when`` and ``include_own_lines``.

    Raises:
        test_suite.scenarios.loader.ScenarioFileError: Malformed input.
        test_suite.scenarios.resolver.ResolutionError: A scenario names a
            peer the matrix doesn't have, or can't be bound.
    """
    return _report(
        resolve_all(parse_tests(raw_tests), get_matrix(), sut_sdk=sut_sdk)
    )


def prepare_file(path: Path, *, sut_sdk: str | None = None) -> list[Scenario]:
    """Same as :func:`prepare`, reading the document from a file.

    Both front ends go through one of these two so a scenario behaves the
    same over HTTP and on the CLI — including what gets reported about the
    scenarios that won't run.
    """
    return _report(
        resolve_all(load_file(path), get_matrix(), sut_sdk=sut_sdk)
    )


def _report(report: ResolutionReport) -> list[Scenario]:
    """Log what won't run, and what will run short-handed.

    At warning level, because a skip or a trim nobody notices is
    indistinguishable from coverage that quietly vanished. Grouped by cause:
    one exclusion typically hits dozens of scenarios, and repeating its
    rationale per scenario buries the run's actual output.
    """
    if report.skipped:
        by_reason: dict[str, int] = defaultdict(int)
        for _, why in report.skipped:
            by_reason[why] += 1
        logger.warning('%d scenario(s) SKIPPED:', len(report.skipped))
        for why, n in sorted(by_reason.items()):
            logger.warning('  [%d] %s', n, why)

    if report.trimmed:
        by_peer: dict[tuple[str, str], int] = defaultdict(int)
        for _, agent, why in report.trimmed:
            by_peer[(agent, why)] += 1
        logger.warning(
            '%d scenario(s) running with a peer removed:',
            len({name for name, _, _ in report.trimmed}),
        )
        for (agent, why), n in sorted(by_peer.items()):
            logger.warning('  [%d] %s dropped — %s', n, agent, why)

    logger.info('%d scenario(s) to run', len(report.scenarios))
    return report.scenarios


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def build_specs(sdk_ids: list[str]) -> dict[str, TargetSpec]:
    """Turn scenario-level agent IDs into TargetSpecs.

    Blocks on ``git ls-remote`` per unique (repo, ref), so callers on an
    event loop should run it via :func:`asyncio.to_thread`.

    Refs resolve to SHAs once, here at plan time, so a branch tip that moves
    mid-run cannot mix versions across peers — that's the rule
    ``TargetSpec`` enforces by rejecting anything but a 40-hex SHA.

    Each unique (repo, ref) resolves once even when several identifiers map
    to it (``python_v10`` and ``python_v10_2`` share one entry).

    Raises:
        test_suite.launcher.matrix.MatrixError: Unknown agent id, or a
            malformed matrix.
        test_suite.launcher.errors.PermanentError: The ref does not exist.
        test_suite.launcher.errors.InfraFailure: ls-remote kept timing out.
    """
    matrix = get_matrix()
    sha_cache: dict[tuple[str, str], str] = {}
    specs: dict[str, TargetSpec] = {}

    for sdk_id in sdk_ids:
        if sdk_id == SUT_ID:
            specs[sdk_id] = TargetSpec(kind=Kind.MOUNT)
            continue

        entry = matrix.resolve(sdk_id)
        cache_key = (entry.repo, entry.ref)
        if cache_key not in sha_cache:
            sha_cache[cache_key] = resolve_ref(entry.repo, entry.ref)
        specs[sdk_id] = TargetSpec(
            kind=Kind.CHECKOUT,
            repo=entry.repo,
            sha=sha_cache[cache_key],
        )
    return specs


def _plan(scenarios: list[Scenario]) -> _Plan:
    """Union every agent the batch needs, so each is spawned exactly once."""
    all_ids = sorted({sdk for s in scenarios for sdk in s.sdks})
    specs_by_id = build_specs(all_ids)
    ids = list(specs_by_id.keys())
    return _Plan(ids=ids, specs=[specs_by_id[i] for i in ids])


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


async def run_scenarios(
    scenarios: list[Scenario],
    *,
    log_dir: Path | None = None,
) -> RunReport:
    """Start one cluster and run every scenario against it.

    A peer that fails to build or start is dropped rather than sinking the
    run: scenarios that can lose it run with it trimmed out, scenarios that
    can't are skipped, and both are recorded on the returned report so the
    lost coverage is visible instead of a quietly smaller green run. The SUT
    (``current``) is the exception — it is the code under test, so if it
    can't start there is nothing to test and the run fails.

    Args:
        scenarios: What to run. Must be non-empty.
        log_dir: If set, each agent's stdout/stderr is captured to
            ``<log_dir>/agent_<id>.log``.

    Returns:
        A :class:`RunReport`: results keyed by scenario name (a scenario with
        ``build_subtests`` contributes one entry per expanded subgraph), plus
        the peers dropped and the scenarios trimmed or skipped as a result.

    Raises:
        ValueError: ``scenarios`` is empty.
        ClusterStartupError: The SUT failed to start, or every scenario
            needed a peer that did — nothing runnable is left.
        test_suite.launcher.matrix.MatrixError: Unknown agent id.
        test_suite.launcher.errors.PermanentError: Unresolvable ref.
        test_suite.launcher.errors.InfraFailure: Transient fetch failure.
    """
    if not scenarios:
        raise ValueError('No scenarios provided')

    async with _execution_lock:
        return await _run_locked(scenarios, log_dir=log_dir)


def _select_runnable(
    scenarios: list[Scenario],
    agents: AgentTable,
) -> tuple[
    list[tuple[Scenario, list[str], list[str] | None]],
    list[tuple[str, list[str]]],
    list[tuple[str, list[str]]],
]:
    """Decide what each scenario can still run without the missing peers.

    ``agents`` holds only the agents that came up. For each scenario this
    keeps it as authored when nothing is missing, trims a downed peer when
    the remainder still traverses, or gives up on it when it doesn't.

    Returns ``(runnable, trimmed, skipped)`` where ``runnable`` is
    ``(scenario, sdks, edges)`` ready to execute (sdks/edges are the induced
    subgraph for a trimmed scenario), ``trimmed`` is ``(name, dropped)`` and
    ``skipped`` is ``(name, missing)``.
    """
    started = set(agents)
    runnable: list[tuple[Scenario, list[str], list[str] | None]] = []
    trimmed: list[tuple[str, list[str]]] = []
    skipped: list[tuple[str, list[str]]] = []

    for s in scenarios:
        missing = [sdk for sdk in s.sdks if sdk not in started]
        if not missing:
            runnable.append((s, s.sdks, s.edges))
            continue
        induced = induced_runnable_scenario(
            s.sdks, s.edges, started, agents,
            behavior=s.behavior, protocols=s.protocols, streaming=s.streaming,
        )
        if induced is None:
            skipped.append((s.name, missing))
        else:
            kept, kept_edges = induced
            runnable.append((s, kept, kept_edges))
            trimmed.append((s.name, missing))
    return runnable, trimmed, skipped


def _report_startup(
    dropped_peers: dict[str, str],
    trimmed: list[tuple[str, list[str]]],
    skipped: list[tuple[str, list[str]]],
) -> None:
    """Log peers that didn't start and the coverage that cost.

    At warning level and naming names: silently dropping a peer and going
    green on a smaller run is the exact failure mode this pipeline guards
    against. Reported the way resolution-time skips are (see :func:`_report`)
    so both kinds of lost coverage read alike in the log. In CI this reaches
    the job output two ways — the container log on a failing run, and the
    ``/run`` response (hence :class:`RunReport`) on a passing one, where the
    container log is not dumped.
    """
    logger.warning(
        '%d peer(s) failed to start and were dropped:', len(dropped_peers),
    )
    for agent, detail in sorted(dropped_peers.items()):
        logger.warning('  %s — %s', agent, detail)
    if trimmed:
        logger.warning(
            '%d scenario(s) running with a peer removed:', len(trimmed),
        )
        for name, dropped in trimmed:
            logger.warning('  %s — without %s', name, ', '.join(sorted(dropped)))
    if skipped:
        logger.warning(
            '%d scenario(s) SKIPPED — a required peer did not start:',
            len(skipped),
        )
        for name, missing in skipped:
            logger.warning('  %s — needs %s', name, ', '.join(sorted(missing)))


async def _run_locked(
    scenarios: list[Scenario],
    *,
    log_dir: Path | None,
) -> RunReport:
    logger.info(
        'Planning cluster for %d scenario(s): %s',
        len(scenarios),
        sorted({sdk for s in scenarios for sdk in s.sdks}),
    )
    # ls-remote per peer blocks; keep it off the event loop.
    plan = await asyncio.to_thread(_plan, scenarios)

    # Readable per-agent log filenames so whoever is tailing them can tell
    # which peer is which. Positional, so `python_v10` and `python_v10_2`
    # get separate files even though they resolve to the same spec.
    log_names = [f'agent_{sid}' for sid in plan.ids]

    with Cluster(log_dir=log_dir) as cluster:
        outcomes = await asyncio.to_thread(
            cluster.start_all, plan.specs, log_names=log_names,
        )

        started_handles = {
            plan.ids[i]: o.handle for i, o in enumerate(outcomes) if o.ok()
        }
        dropped_peers = {
            plan.ids[i]: f'{o.error.stage.value}: {o.error}'
            for i, o in enumerate(outcomes)
            if not o.ok()
        }

        # The SUT is the code under test; a peer being down is tolerable, the
        # SUT being down is not — there would be nothing to test, and a green
        # empty run is worse than a red one.
        if SUT_ID in dropped_peers:
            raise ClusterStartupError(
                sorted(dropped_peers.items()),
                summary=f'the code under test ({SUT_ID!r}) failed to start',
            )

        # Where the agents we started are listening. Passed down the call
        # chain rather than published to a global, so nothing can leak into
        # the next run. Holds only the agents that actually came up.
        agents = AgentTable.from_handles(started_handles)
        logger.info('Cluster up: %r', agents)

        runnable, trimmed, skipped = _select_runnable(scenarios, agents)
        if dropped_peers:
            _report_startup(dropped_peers, trimmed, skipped)

        if not runnable:
            # Every scenario needed a peer that didn't come up. Refuse rather
            # than return an empty result set that reads as a green run which
            # tested nothing.
            raise ClusterStartupError(
                sorted(dropped_peers.items()),
                summary='every scenario needed a peer that failed to start',
            )

        # Sequential on purpose — the cluster is shared, and running
        # scenarios concurrently against it overloads the agents.
        results: dict[str, ScenarioResult] = {}
        for scenario, run_sdks, run_edges in runnable:
            logger.info("Executing scenario '%s'", scenario.name)
            raw = await execute_itk_test(
                sdks=run_sdks,
                behavior=scenario.behavior,
                agents=agents,
                edges=run_edges,
                scenario_name=scenario.name,
                protocols=scenario.protocols,
                streaming=scenario.streaming,
                build_subtests=scenario.build_subtests,
            )
            for name, details in raw.items():
                # sdks/edges come from the execution because a subtest (or a
                # trimmed scenario) runs a smaller graph than the scenario
                # declares; everything else is a property of the scenario and
                # is copied across.
                results[name] = ScenarioResult(
                    passed=bool(details['passed']),
                    sdks=list(details['sdks']),
                    edges=details.get('edges'),
                    protocols=scenario.protocols,
                    behavior=scenario.behavior,
                    streaming=scenario.streaming,
                    tier=scenario.tier,
                )

    return RunReport(
        results=results,
        dropped_peers=dropped_peers,
        trimmed=trimmed,
        skipped=skipped,
    )



