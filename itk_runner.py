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
import contextlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from test_suite.launcher import Cluster, TargetSpec
from test_suite.launcher.fetch import resolve_ref
from test_suite.launcher.matrix import Matrix
from test_suite.launcher.spec import Kind
from testlib import execute_itk_test


logger = logging.getLogger(__name__)


# The SUT identifier. Unlike every other id it doesn't map through
# matrix.yaml — it's whatever is sitting at config.mount_dir().
SUT_ID = 'current'


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """One scenario to execute.

    Field-for-field the schema of an entry in an SDK's ``scenarios.json``,
    so a file written for CI runs unchanged through the CLI.
    """

    name: str
    sdks: list[str]
    behavior: str
    edges: list[str] | None = None
    protocols: list[str] | None = None
    streaming: bool = False
    build_subtests: bool = False


@dataclass(frozen=True)
class ScenarioResult:
    passed: bool
    sdks: list[str]
    edges: list[str] | None = None


class ClusterStartupError(RuntimeError):
    """One or more agents failed to start.

    Carries per-agent detail so the caller can say *which* peer died and at
    which stage, rather than reporting a blanket failure.
    """

    def __init__(self, failures: list[tuple[str, str]]) -> None:
        self.failures = failures
        super().__init__(
            'Cluster startup failed: '
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

# Scenario execution reads agent ports from the process-global
# ``test_suite._AGENT_DEFS``, so two concurrent runs in one process would
# race on it. Both front ends go through here, so one lock covers both.
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
) -> dict[str, ScenarioResult]:
    """Start one cluster and run every scenario against it.

    Args:
        scenarios: What to run. Must be non-empty.
        log_dir: If set, each agent's stdout/stderr is captured to
            ``<log_dir>/agent_<id>.log``.

    Returns:
        Results keyed by scenario name. A scenario with ``build_subtests``
        contributes one entry per expanded subgraph, not just one for itself.

    Raises:
        ValueError: ``scenarios`` is empty.
        ClusterStartupError: At least one agent didn't come up.
        test_suite.launcher.matrix.MatrixError: Unknown agent id.
        test_suite.launcher.errors.PermanentError: Unresolvable ref.
        test_suite.launcher.errors.InfraFailure: Transient fetch failure.
    """
    if not scenarios:
        raise ValueError('No scenarios provided')

    async with _execution_lock:
        return await _run_locked(scenarios, log_dir=log_dir)


async def _run_locked(
    scenarios: list[Scenario],
    *,
    log_dir: Path | None,
) -> dict[str, ScenarioResult]:
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

        failures = [
            (plan.ids[i], f'{o.error.stage.value}: {o.error}')
            for i, o in enumerate(outcomes)
            if not o.ok()
        ]
        if failures:
            raise ClusterStartupError(failures)

        id_to_handle = {plan.ids[i]: o.handle for i, o in enumerate(outcomes)}
        with wire_ports(id_to_handle):
            # Sequential on purpose — the cluster is shared, and running
            # scenarios concurrently against it overloads the agents.
            results: dict[str, ScenarioResult] = {}
            for scenario in scenarios:
                logger.info("Executing scenario '%s'", scenario.name)
                raw = await execute_itk_test(
                    sdks=scenario.sdks,
                    behavior=scenario.behavior,
                    edges=scenario.edges,
                    scenario_name=scenario.name,
                    protocols=scenario.protocols,
                    streaming=scenario.streaming,
                    build_subtests=scenario.build_subtests,
                )
                for name, details in raw.items():
                    results[name] = ScenarioResult(
                        passed=bool(details['passed']),
                        sdks=list(details['sdks']),
                        edges=details.get('edges'),
                    )
    return results


# ---------------------------------------------------------------------------
# Adapter: launcher handles -> test_suite._AGENT_DEFS[sdk][httpPort/grpcPort]
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def wire_ports(id_to_handle: dict[str, object]) -> Iterator[None]:
    """Publish launcher-owned ports to the agent registry, then withdraw them.

    ``testlib.execute_itk_test`` resolves peer addresses through
    ``test_suite.get_agent_card_uri`` / ``get_agent_def``, which read
    ``test_suite._AGENT_DEFS[sdk]['httpPort'/'grpcPort']``. The registry
    doesn't allocate ports — the launcher owns them — so this is where they
    get handed over.

    On exit only the keys we wrote are removed, so a second run in the same
    process starts clean (the service is long-lived).

    Unknown identifiers are skipped with a warning instead of raising: the
    scenario will fail anyway, with a message from ``get_agent_card_uri``
    that names the id.
    """
    from test_suite import _AGENT_DEFS  # noqa: PLC0415 — access is intentional

    written: list[str] = []
    try:
        for sdk_id, handle in id_to_handle.items():
            if sdk_id not in _AGENT_DEFS:
                logger.warning(
                    'Launcher handle for %r has no entry in _AGENT_DEFS; '
                    'execute_itk_test will not see it', sdk_id,
                )
                continue
            _AGENT_DEFS[sdk_id]['httpPort'] = handle.http_port  # type: ignore[attr-defined]
            _AGENT_DEFS[sdk_id]['grpcPort'] = handle.grpc_port  # type: ignore[attr-defined]
            written.append(sdk_id)
        yield
    finally:
        for sdk_id in written:
            _AGENT_DEFS[sdk_id].pop('httpPort', None)
            _AGENT_DEFS[sdk_id].pop('grpcPort', None)
