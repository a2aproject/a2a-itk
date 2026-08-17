"""ITK service — the HTTP ``/run`` handler.

The wire contract is frozen: request/response schema, ``/health``, port
8000. Every SDK's ``run_itk.sh`` POSTs its ``scenarios.json`` here.

This module is deliberately thin. All the actual work — resolving agent
identifiers against ``matrix.yaml``, starting the cluster, executing
scenarios — lives in :mod:`itk_runner`, which ``run_tests.py`` also
drives. Everything here is HTTP concerns: schema validation and mapping
runner errors onto status codes.
"""

from __future__ import annotations

import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import itk_runner
from itk_runner import ClusterStartupError, Scenario
from test_suite.launcher import InfraFailure, PermanentError
from test_suite.launcher.matrix import MatrixError


logging.basicConfig(
    level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Frozen wire schema — every SDK's scenarios.json is written against it
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


app = FastAPI(title='ITK Service')


@app.get('/health')
async def health() -> dict[str, str]:
    """Frozen — SDK run_itk.sh scripts poll this before POSTing /run."""
    return {'status': 'ok'}


@app.post('/run', response_model=RunTestsResponse)
async def run_tests(request: RunTestsRequest) -> RunTestsResponse:
    """Frozen — the request/response schema every SDK's scenarios.json targets."""
    if not request.tests:
        raise HTTPException(status_code=400, detail='No tests provided')

    scenarios = [
        Scenario(
            name=c.name,
            sdks=c.sdks,
            behavior=c.behavior,
            edges=c.edges,
            protocols=c.protocols,
            streaming=c.streaming,
            build_subtests=c.build_subtests,
        )
        for c in request.tests
    ]

    try:
        results = await itk_runner.run_scenarios(
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
        # At least one peer's startup was transient-class (build hiccup,
        # git flake); a retry can recover. Detail names which peer.
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        logger.exception('Test execution failed')
        raise HTTPException(status_code=500, detail=f'Execution error: {e!s}') from e

    typed = {
        name: TestResultDetails(passed=r.passed, sdks=r.sdks, edges=r.edges)
        for name, r in results.items()
    }
    return RunTestsResponse(
        results=typed, all_passed=all(r.passed for r in typed.values()),
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
