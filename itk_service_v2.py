"""ITK service — the HTTP ``/run`` handler.

``/health``, port 8000 and the legacy request/response schema are unchanged.
``/run`` additionally accepts ``traversal/v1`` scenarios, and a batch may mix
both, so each SDK can migrate on its own schedule.

Thin by design: parsing, role binding, cluster lifecycle and execution all
live in :mod:`itk_runner`, which ``run_tests.py`` also drives. Everything
here is HTTP concerns.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import acts_runner
import itk_runner
from acts_runner import ActsRunError
from itk_runner import ClusterStartupError
from test_suite.acts.schema import RunnerRequirement, TransportBinding
from test_suite.launcher import InfraFailure, PermanentError
from test_suite.launcher.matrix import MatrixError
from test_suite.scenarios.loader import ScenarioFileError
from test_suite.scenarios.resolver import ResolutionError


logging.basicConfig(
    level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class RunTestsRequest(BaseModel):
    # Untyped on purpose: both scenario schemas are live, and
    # test_suite.scenarios owns their validation. A pydantic copy here would
    # only give the definitions somewhere to drift apart.
    tests: list[dict[str, Any]]
    # SDK under test, for `test_when` and `include_own_lines`. Absent means
    # nothing is filtered, which is what a legacy scenarios.json wants.
    sut_sdk: str | None = None


class TestResultDetails(BaseModel):
    """One scenario's outcome.

    ``passed``/``sdks``/``edges`` are unchanged. The rest are optional
    additions, so a consumer reading only those three is unaffected.
    """

    passed: bool
    sdks: list[str]
    edges: list[str] | None = None
    protocols: list[str] | None = None
    behavior: str | None = None
    streaming: bool = False
    tier: str | None = None


class StartupReport(BaseModel):
    """What a peer failing to start cost this run.

    Populated only when the cluster didn't come up clean (``null`` otherwise,
    so the common case adds a single null key and nothing more). Lets
    ``scripts/itk_report.py`` print the lost coverage: on a passing run the
    container log isn't dumped, so this is the only place the operator sees
    what went missing.

    Two shapes: a *partial* failure (some peers dropped, run still ran) fills
    ``dropped_peers``/``trimmed``/``skipped``; a *total* failure (the SUT
    didn't start, or nothing was runnable) sets ``cluster_error`` and every
    scenario is reported failed — see :func:`_cluster_failure_response`.
    """

    dropped_peers: dict[str, str]
    trimmed: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    # Set only on a total cluster-startup failure. Its presence is what tells
    # a reader the run didn't test anything — the scenarios are all failed
    # placeholders recorded so the nightly history gets an entry, not a gap.
    cluster_error: str | None = None


class RunTestsResponse(BaseModel):
    results: dict[str, TestResultDetails]
    all_passed: bool
    # ``null`` on a clean run; an object when a peer failed to start. Additive
    # and optional — the three fields every SDK already reads are untouched.
    startup: StartupReport | None = None


class RunActsRequest(BaseModel):
    """Ask for one ACTS conformance run against the mounted SUT.

    Separate from ``RunTestsRequest`` because the two suites answer different
    questions: a traversal names agents and edges, a conformance run names one
    binding and a corpus.
    """

    transport: TransportBinding
    #: SDK identity for the report's `sdk-info` (spec §13.1).
    sdk: str
    sdk_version: str = 'unknown'
    language: str = 'unknown'
    repository: str | None = None
    #: Restrict the run to these test ids. For iterating on one failure
    #: without paying for the whole corpus.
    tests: list[str] | None = None
    #: Variables the corpus references but no document defines —
    #: `insufficientAuthToken` and `otherUserTaskId`.
    variables: dict[str, Any] = {}
    #: What this runner can do beyond speaking the protocol (spec §12.1).
    #: Anything not listed makes the tests needing it skip honestly.
    capabilities: list[RunnerRequirement] = []
    #: Fail tests whose `tck-*` behaviours the SUT does not declare. Off means
    #: run them anyway, which is only useful before a repo adopts §11.
    gate_on_behaviors: bool = True


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


app = FastAPI(title='ITK Service')


@app.get('/health')
async def health() -> dict[str, str]:
    """Frozen — SDK run_itk.sh scripts poll this before POSTing /run."""
    return {'status': 'ok'}


@app.post('/run', response_model=RunTestsResponse)
async def run_tests(request: RunTestsRequest) -> RunTestsResponse:
    """Run a batch of scenarios in either schema.

    The legacy request shape every SDK's ``scenarios.json`` targets is
    unchanged. A ``traversal/v1`` entry is recognised by its ``schema`` key
    and resolved against ``matrix.yaml`` first; a batch may mix the two.
    """
    if not request.tests:
        raise HTTPException(status_code=400, detail='No tests provided')

    try:
        scenarios = itk_runner.prepare(
            {'tests': request.tests}, sut_sdk=request.sut_sdk,
        )
    except (ScenarioFileError, ResolutionError) as e:
        # Malformed or unbindable scenario — the caller sent something we
        # can't run. 400 rather than 500: nothing here is retryable.
        raise HTTPException(status_code=400, detail=str(e)) from e

    if not scenarios:
        # Refused rather than returning an empty pass, which would read as a
        # green run that tested nothing. The cause is in the service log:
        # itk_runner reports every skip with its reason.
        raise HTTPException(
            status_code=400,
            detail=(
                f'All {len(request.tests)} scenario declaration(s) resolved to '
                f'nothing runnable for sut_sdk={request.sut_sdk!r} — filtered '
                f'by test_when, or excluded as known failures. See the service '
                f'log for the per-scenario reasons.'
            ),
        )

    try:
        report = await itk_runner.run_scenarios(
            scenarios, log_dir=_agent_log_dir(),
        )
    except (MatrixError, PermanentError) as e:
        # Bad matrix entry, bogus agent id, unknown ref — none retryable.
        # 400: the caller sent a spec we can't fulfil.
        raise HTTPException(status_code=400, detail=str(e)) from e
    except InfraFailure as e:
        # Transient (ls-remote timed out after retries) — caller can retry.
        raise HTTPException(status_code=502, detail=str(e)) from e
    except ClusterStartupError as e:
        # Reached only when a peer being down could not be tolerated: the SUT
        # itself failed to start, or every scenario needed a peer that did.
        # A lone peer build hiccup no longer lands here — it is dropped and
        # the run continues.
        #
        # Deliberately NOT a 502: a bare error envelope is discarded by the
        # nightly metrics step (itk_report rejects it, so process_results
        # never runs), leaving a *gap* in the rolling history — the failure
        # looks like "no run happened". Instead return a recordable run with
        # every scenario failed and the reason attached, so the nightly
        # appends a red entry and the PR job still fails (all_passed=false).
        # Genuinely transient infra (fetch/ls-remote) is InfraFailure above
        # and still 502s — that is not a test outcome to record.
        return _cluster_failure_response(scenarios, e)
    except Exception as e:
        logger.exception('Test execution failed')
        raise HTTPException(status_code=500, detail=f'Execution error: {e!s}') from e

    typed = {
        name: TestResultDetails(
            passed=r.passed,
            sdks=r.sdks,
            edges=r.edges,
            protocols=r.protocols,
            behavior=r.behavior,
            streaming=r.streaming,
            tier=r.tier,
        )
        for name, r in report.results.items()
    }
    return RunTestsResponse(
        results=typed,
        all_passed=all(r.passed for r in typed.values()),
        startup=_startup_report(report),
    )


@app.post('/run-acts')
async def run_acts(request: RunActsRequest) -> dict[str, Any]:
    """Run the ACTS conformance corpus against the mounted SUT.

    Returns a spec §13 report document as-is. Unlike ``/run`` there is no
    ITK-shaped envelope around it: the report format is standardized so that
    dashboards can read a run from any ACTS runner, and wrapping it would
    defeat that.
    """
    try:
        result = await acts_runner.run(
            transport=request.transport,
            test_ids=request.tests,
            variables=request.variables,
            capabilities=request.capabilities,
            gate_on_behaviors=request.gate_on_behaviors,
            log_dir=_agent_log_dir(),
        )
    except ActsRunError as e:
        # The SUT would not start, its card is unreadable, or the request
        # names tests that do not exist. None of it is retryable.
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception('ACTS run failed')
        raise HTTPException(status_code=500, detail=f'ACTS error: {e!s}') from e

    return acts_runner.to_report(
        result,
        sdk_name=request.sdk,
        sdk_version=request.sdk_version,
        language=request.language,
        repository=request.repository,
    )


def _startup_report(report: itk_runner.RunReport) -> StartupReport | None:
    """Render a peer-drop summary for the response, or ``None`` if clean.

    ``trimmed``/``skipped`` become lists of objects rather than tuples so the
    JSON is self-describing for whoever reads it.
    """
    if not report.dropped_peers:
        return None
    return StartupReport(
        dropped_peers=report.dropped_peers,
        trimmed=[{'name': n, 'dropped': d} for n, d in report.trimmed],
        skipped=[{'name': n, 'missing': m} for n, m in report.skipped],
    )


def _cluster_failure_response(
    scenarios: list[itk_runner.Scenario], err: ClusterStartupError,
) -> RunTestsResponse:
    """Represent a total cluster-startup failure as a recordable, failed run.

    The cluster couldn't provide anything to test, so no scenario produced a
    result. Rather than let that surface as a bare error the nightly metrics
    step discards — a gap in the rolling history — mark every resolved
    scenario failed and attach the reason. The nightly then records a red run
    (dashboard shows the night), and the PR job still fails on
    ``all_passed=false``. Each placeholder keeps its scenario metadata so the
    metrics processor records it the same way it records a real result.
    """
    typed = {
        s.name: TestResultDetails(
            passed=False,
            sdks=s.sdks,
            edges=s.edges,
            protocols=s.protocols,
            behavior=s.behavior,
            streaming=s.streaming,
            tier=s.tier,
        )
        for s in scenarios
    }
    return RunTestsResponse(
        results=typed,
        all_passed=False,
        startup=StartupReport(
            dropped_peers=dict(err.failures),
            cluster_error=str(err),
        ),
    )


def _agent_log_dir() -> Path | None:
    """``/app/logs`` when the operator bind-mounted it, else no capture.

    ``run_itk.sh`` mounts it when ``ITK_LOG_LEVEL=DEBUG``; with it present,
    agent stdout/stderr lands in per-agent files so a readiness or spawn
    failure can be diagnosed after the container is gone.
    """
    d = Path('/app/logs')
    return d if d.is_dir() else None


if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=8000)  # noqa: S104
