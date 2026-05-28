import asyncio  # noqa: I001
import logging
from typing import Any, Literal

import uvicorn

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator

from testlib import execute_itk_test, start_itk_cluster, stop_itk_cluster


# Configure logging to match run_tests.py style
logging.basicConfig(
    level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title='ITK Test Orchestration Service')
_execution_lock = asyncio.Lock()


class TestStep(BaseModel):
    """A single traversal execution within a test case.

    One step corresponds to one call into ``execute_itk_test``: a cluster
    of SDK agents is exercised over the given protocols and behavior.
    """

    sdks: list[str]
    behavior: str
    edges: list[str] | None = None
    protocols: list[str] | None = None
    streaming: bool = False
    build_subtests: bool = False


class TestCase(BaseModel):
    """A named ITK test case.

    A test case is composed of one or more :class:`TestStep` executions
    run sequentially. The legacy flat form (sdks/behavior/... at the
    top level) is still accepted for backwards compatibility and is
    automatically normalised into a single-step list.
    """

    name: str
    expected: Literal['pass', 'fail'] = 'pass'
    steps: list[TestStep] = Field(default_factory=list)

    # --- Legacy fields (single-step, flat form). Kept optional and only
    # used to construct ``steps`` when the caller did not provide it. ---
    sdks: list[str] | None = None
    behavior: str | None = None
    edges: list[str] | None = None
    protocols: list[str] | None = None
    streaming: bool | None = None
    build_subtests: bool | None = None

    @model_validator(mode='after')
    def _coalesce_legacy_flat_form(self) -> 'TestCase':
        """Promote legacy top-level fields into a single ``TestStep``.

        This keeps existing scenarios.json files and ``/run`` callers
        working without modification while we transition to the
        explicit multi-step schema.
        """
        if self.steps:
            return self
        if self.sdks is None or self.behavior is None:
            raise ValueError(
                f"TestCase '{self.name}' must define either 'steps' or both "
                "'sdks' and 'behavior' at the top level."
            )
        self.steps = [
            TestStep(
                sdks=self.sdks,
                behavior=self.behavior,
                edges=self.edges,
                protocols=self.protocols,
                streaming=self.streaming or False,
                build_subtests=self.build_subtests or False,
            )
        ]
        return self

    def all_sdks(self) -> list[str]:
        """All SDKs referenced across all steps (deduplicated)."""
        seen: set[str] = set()
        for step in self.steps:
            seen.update(step.sdks)
        return sorted(seen)


class RunTestsRequest(BaseModel):
    """Request model for the /run endpoint."""

    tests: list[TestCase]


class TestResultDetails(BaseModel):
    """Details representing the outcome and topology of a single test run."""

    passed: bool
    sdks: list[str]
    edges: list[str] | None = None
    # Set to "fail" when the surrounding TestCase declared ``expected: fail``,
    # in which case ``passed`` has already been inverted (i.e. an
    # actually-failing run is reported as ``passed: true``). Omitted for the
    # default expected-pass case so that legacy consumers see no schema change.
    expected: Literal['fail'] | None = None


class RunTestsResponse(BaseModel):
    """Response model for the /run endpoint."""

    results: dict[str, TestResultDetails]
    all_passed: bool


@app.get('/health')
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {'status': 'ok'}


@app.post('/run', response_model=RunTestsResponse)
async def run_tests(request: RunTestsRequest) -> RunTestsResponse:
    """FastAPI endpoint to execute ITK tests with concurrency control."""
    # We allow only single collection of test cases to be executed at a time
    # to avoid wipeing out the ports that are utilized at the moment.
    async with _execution_lock:
        try:
            response = await _test(request)
        except HTTPException:
            raise
        except Exception as e:
            logger.exception('Concurrent test execution encountered an error.')
            raise HTTPException(
                status_code=500, detail=f'Execution error: {e!s}'
            ) from e
    return response


async def _run_test_case(case: TestCase) -> dict[str, dict[str, Any]]:
    """Execute every step of a test case sequentially against a running cluster.

    Each step's traversal results are merged into one dict keyed by the
    runtime scenario name produced by ``execute_itk_test`` (which may
    differ from ``case.name`` when sub-tests are expanded). When a case
    has more than one step, each step's results are prefixed with
    ``"{case.name}/step-{n}/"`` so they remain distinguishable.

    If ``case.expected == "fail"``, every result's ``passed`` flag is
    inverted before returning, so an expected-failing case that does
    fail is reported as ``passed: true``.
    """
    multi_step = len(case.steps) > 1
    merged: dict[str, dict[str, Any]] = {}
    for idx, step in enumerate(case.steps, start=1):
        label = (
            f'{case.name}/step-{idx}' if multi_step else case.name
        )
        logger.info("Executing scenario '%s'...", label)
        step_results = await execute_itk_test(
            sdks=step.sdks,
            behavior=step.behavior,
            edges=step.edges,
            scenario_name=label,
            protocols=step.protocols,
            streaming=step.streaming,
            build_subtests=step.build_subtests,
        )
        merged.update(step_results)

    if case.expected == 'fail':
        for entry in merged.values():
            entry['passed'] = not entry['passed']
            entry['expected'] = 'fail'

    return merged


async def _test(request: RunTestsRequest) -> RunTestsResponse:
    """Internal logic to execute a batch of ITK test scenarios."""
    if not request.tests:
        raise HTTPException(status_code=400, detail='No tests provided')

    # 1. Identify all unique SDKs needed across all steps of all test cases
    all_required_sdks: set[str] = set()
    for case in request.tests:
        all_required_sdks.update(case.all_sdks())

    sdk_list = sorted(all_required_sdks)

    logger.info(
        'Starting test execution for %d scenarios using SDKs: %s',
        len(request.tests),
        sdk_list,
    )

    # 2. Start the shared cluster
    procs = []
    ports = []
    try:
        procs, _uris, ports = await start_itk_cluster(sdk_list)
    except Exception as e:
        logger.exception('Failed to start ITK cluster')
        raise HTTPException(
            status_code=500, detail=f'Failed to start ITK cluster: {e!s}'
        ) from e

    try:
        # 3. Run all scenarios sequentially to prevent overwhelming the shared cluster
        logger.info('Starting sequential scenario execution...')
        results_list = []
        for case in request.tests:
            res_dict = await _run_test_case(case)
            results_list.append(res_dict)

        # 5. Prepare results
        results_map: dict[str, Any] = {}
        all_passed = True
        for res_dict in results_list:
            results_map.update(res_dict)

        for name, details in results_map.items():
            passed = details['passed']
            if not passed:
                all_passed = False
            status = 'PASSED' if passed else 'FAILED'
            logger.info("Scenario '%s': %s", name, status)

        return RunTestsResponse(results=results_map, all_passed=all_passed)

    except Exception as e:
        logger.exception('Concurrent test execution encountered an error.')
        raise HTTPException(
            status_code=500, detail=f'Execution error: {e!s}'
        ) from e
    finally:
        logger.info('Decommissioning shared agent cluster...')
        stop_itk_cluster(procs, ports)


if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=8000)  # noqa: S104
