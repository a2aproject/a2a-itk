"""ITK service — launcher-based /run handler for the strangler shadow.

Same wire contract as ``itk_service.py`` (frozen — request/response
schema, ``/health``, port 8000). The only observable difference is
under the hood:

  * Cluster startup goes through :class:`test_suite.launcher.Cluster`
    (dynamic ports, parallel readiness gate, process-group teardown)
    instead of the legacy ``testlib.start_itk_cluster`` registry-based
    launcher.

  * Peer versions come from ``matrix.yaml`` (fetched via
    :class:`test_suite.launcher.Cluster` on CHECKOUT specs) instead of
    the baked ``agents/<sdk>/<line>`` directories.

  * The SUT (``current``) still comes from the container's bind mount
    at ``/app/agents/repo/itk`` — unchanged from legacy.

Scenario execution reuses ``testlib.execute_itk_test`` unchanged. The
adapter (:func:`_wire_launcher_ports_into_registry`) writes the
launcher's dynamically-allocated ports into ``test_suite._AGENT_DEFS``
so ``execute_itk_test``'s port lookups find them, then clears them on
exit. Migrating scenario execution to consume launcher handles
directly is a follow-up refactor (Phase 2).

Selection between v1 and v2 is via the ``ITK_ENTRYPOINT`` env var read
by the container's CMD (see Dockerfile). Default is legacy; set
``ITK_ENTRYPOINT=itk_service_v2.py`` to route to this service.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import test_suite
from test_suite.launcher import Cluster, InfraFailure, PermanentError, TargetSpec
from test_suite.launcher.errors import LauncherError
from test_suite.launcher.fetch import resolve_ref
from test_suite.launcher.matrix import Matrix, MatrixError
from test_suite.launcher.spec import Kind
from testlib import execute_itk_test


logging.basicConfig(
    level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Frozen wire schema — MUST stay in lock-step with itk_service.py
# ---------------------------------------------------------------------------


class TestCase(BaseModel):
    """One scenario the caller wants executed."""

    name: str
    sdks: list[str]
    behavior: str
    edges: list[str] | None = None
    protocols: list[str] | None = None
    streaming: bool = False
    build_subtests: bool = False


class RunTestsRequest(BaseModel):
    tests: list[TestCase]


class TestResultDetails(BaseModel):
    passed: bool
    sdks: list[str]
    edges: list[str] | None = None


class RunTestsResponse(BaseModel):
    results: dict[str, TestResultDetails]
    all_passed: bool


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


app = FastAPI(title='ITK v2 Service (launcher-based)')

# Serialise /run — every launcher spawn allocates dynamic ports and
# writes them into test_suite._AGENT_DEFS via the adapter. Two concurrent
# /run calls would race on that registry. Legacy has the same lock.
_execution_lock = asyncio.Lock()

# Lazy-loaded so tests can inject a fake Matrix without needing a real
# matrix.yaml on disk. Also lets a bad matrix.yaml surface as a 500 on
# the first /run instead of preventing the whole service from starting
# (nice for debugging via /health, which stays green either way).
_matrix: Matrix | None = None


def _get_matrix() -> Matrix:
    global _matrix
    if _matrix is None:
        _matrix = Matrix.from_default()
    return _matrix


@app.get('/health')
async def health() -> dict[str, str]:
    """Frozen — same shape as itk_service.py."""
    return {'status': 'ok'}


@app.post('/run', response_model=RunTestsResponse)
async def run_tests(request: RunTestsRequest) -> RunTestsResponse:
    """Frozen — same request/response schema as itk_service.py."""
    async with _execution_lock:
        try:
            return await _run(request)
        except HTTPException:
            raise
        except Exception as e:
            logger.exception('Test execution failed')
            raise HTTPException(status_code=500, detail=f'Execution error: {e!s}') from e


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


async def _run(request: RunTestsRequest) -> RunTestsResponse:
    if not request.tests:
        raise HTTPException(status_code=400, detail='No tests provided')

    # 1. Union all peer IDs the batch needs — cluster is shared across
    #    scenarios so we spawn each SDK once, not once per scenario.
    all_sdk_ids = sorted({sdk for case in request.tests for sdk in case.sdks})
    logger.info(
        'Planning cluster for %d scenarios: %s',
        len(request.tests), all_sdk_ids,
    )

    # 2. Resolve each ID -> TargetSpec.
    #    'current' -> MOUNT (SUT). Everything else -> matrix + resolve_ref.
    #    A ref resolve blocks briefly on git ls-remote; keep it on a thread
    #    so /run stays cooperative with the event loop.
    try:
        specs_by_id = await asyncio.to_thread(_build_specs, all_sdk_ids)
    except (MatrixError, PermanentError) as e:
        # Bad matrix entry, bogus agent id, unknown ref — none of these
        # are retryable. Surface as 400 (client sent a spec we can't fulfil).
        raise HTTPException(status_code=400, detail=str(e)) from e
    except InfraFailure as e:
        # Transient (ls-remote timed out after retries). Surface as 502
        # so the caller can retry.
        raise HTTPException(status_code=502, detail=str(e)) from e

    ordered_ids = list(specs_by_id.keys())
    specs = [specs_by_id[i] for i in ordered_ids]
    # Give each agent a readable log filename so operators tailing
    # /app/logs know what's what.
    log_names = {spec: f'agent_{sid}' for sid, spec in specs_by_id.items()}

    # 3. Start cluster (dynamic ports + parallel readiness + pgroup teardown).
    with Cluster() as cluster:
        outcomes = await asyncio.to_thread(
            cluster.start_all, specs, log_names=log_names,
        )

        # Per-target startup outcomes — one bad peer shouldn't produce a
        # blanket 500. Report which specific peer failed and where.
        failed = [(ordered_ids[i], o) for i, o in enumerate(outcomes) if not o.ok()]
        if failed:
            detail = '; '.join(
                f'{sid} ({o.error.stage.value}): {o.error}'
                for sid, o in failed
            )
            # 502 because at least one peer's startup was transient-class
            # (build hiccup, git flake); a caller retry can recover.
            raise HTTPException(status_code=502, detail=f'Cluster startup failed: {detail}')

        # 4. Wire launcher handles into legacy registry so execute_itk_test
        #    finds ports when it looks them up.
        id_to_handle = {ordered_ids[i]: o.handle for i, o in enumerate(outcomes)}
        with _wire_launcher_ports_into_registry(id_to_handle):

            # 5. Run scenarios sequentially against the shared cluster
            #    (matches legacy — protects the cluster from concurrency).
            results_map: dict[str, dict] = {}
            for case in request.tests:
                logger.info("Executing scenario '%s'", case.name)
                res_dict = await execute_itk_test(
                    sdks=case.sdks,
                    behavior=case.behavior,
                    edges=case.edges,
                    scenario_name=case.name,
                    protocols=case.protocols,
                    streaming=case.streaming,
                    build_subtests=case.build_subtests,
                )
                results_map.update(res_dict)

    # 6. Frozen response format.
    typed_results = {
        name: TestResultDetails(
            passed=bool(details['passed']),
            sdks=list(details['sdks']),
            edges=details.get('edges'),
        )
        for name, details in results_map.items()
    }
    all_passed = all(d.passed for d in typed_results.values())
    return RunTestsResponse(results=typed_results, all_passed=all_passed)


# ---------------------------------------------------------------------------
# Spec building — matrix + ls-remote per unique ID
# ---------------------------------------------------------------------------


def _build_specs(sdk_ids: list[str]) -> dict[str, TargetSpec]:
    """Turn scenario-level agent IDs into TargetSpecs.

    Runs on a worker thread so the ``git ls-remote`` per peer doesn't
    block the event loop. Refs are resolved to SHAs once here (plan time)
    so a moving branch tip can't shift version across peers during the
    batch — matches the design's ``TargetSpec.sha must be 40-hex`` rule.

    Cache: resolves each unique (repo, ref) once even if two agent IDs
    map to it (e.g. ``python_v10`` + ``python_v10_2`` share one entry;
    ``python_v10`` + ``go_v10`` don't but same repo could reappear).
    """
    matrix = _get_matrix()
    sha_cache: dict[tuple[str, str], str] = {}
    specs: dict[str, TargetSpec] = {}

    for sdk_id in sdk_ids:
        if sdk_id == 'current':
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


# ---------------------------------------------------------------------------
# Adapter: launcher handles -> test_suite._AGENT_DEFS[sdk][httpPort/grpcPort]
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _wire_launcher_ports_into_registry(
    id_to_handle: dict[str, object],
) -> Iterator[None]:
    """Poke launcher-owned ports into the legacy registry, then unpoke.

    ``testlib.execute_itk_test`` looks up ports via
    ``test_suite.get_agent_card_uri(sdk_name)`` and
    ``test_suite.get_agent_def(sdk_name)``, both of which read from
    ``test_suite._AGENT_DEFS[sdk_name]['httpPort'/'grpcPort']``. Legacy
    populates those via ``allocate_agent_ports`` before spawning; we
    skip that path entirely and inject the launcher's dynamic ports
    here instead.

    On exit we remove only the entries we wrote — never touch keys that
    were already there. That keeps subsequent /run calls clean even
    under FastAPI's persistent process model.

    Agent IDs unknown to the legacy registry (say a new v2-only SDK id)
    are silently skipped: ``execute_itk_test`` won't try to look up
    ports for something the caller didn't ask for, and skipping avoids
    KeyError.
    """
    from test_suite import _AGENT_DEFS  # noqa: PLC0415 — access is intentional

    written: list[str] = []
    try:
        for sdk_id, handle in id_to_handle.items():
            if sdk_id not in _AGENT_DEFS:
                logger.warning(
                    'Launcher handle for %r has no legacy registry entry; '
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


# ---------------------------------------------------------------------------
# Suppress unused-import warnings for LauncherError re-export
# ---------------------------------------------------------------------------


_ = LauncherError, test_suite  # explicit "we depend on these modules"


if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=8000)  # noqa: S104
