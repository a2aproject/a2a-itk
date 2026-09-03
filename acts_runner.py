"""ACTS conformance pipeline: start the SUT, run the corpus, build a report.

The counterpart of :mod:`itk_runner`, and deliberately a separate module for
the same reason ACTS and traversal are separate suites: a traversal starts N
agents and walks a circuit between them, while a conformance run starts *one*
agent and interrogates it. Forcing both through one pipeline would mean a
plan step that is a no-op for half its callers.

Two front ends drive it, neither owning pipeline logic: ``itk_service_v2.py``
exposes it as ``POST /run-acts``, and ``run_acts.py`` runs it locally.

**Binding URLs come from the agent card, never from a convention.** An agent
may mount JSON-RPC at ``/jsonrpc/`` and REST at ``/rest/`` — the python one
does — so the card's ``supportedInterfaces`` is the only reliable source. The
card itself is always at the host root: it is what tells a client which
bindings exist, so it cannot sit behind one of them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from test_suite.acts import behaviors as sut_behaviors
from test_suite.acts import report as report_writer
from test_suite.acts.dispatcher import Dispatcher, for_binding
from test_suite.acts.loader import LoadedSuite, load_suite
from test_suite.acts.runner import Runner, TestResult
from test_suite.acts.schema import RunnerRequirement, TransportBinding
from test_suite.launcher import Cluster, TargetSpec
from test_suite.launcher.config import mount_dir
from test_suite.launcher.spec import Kind


logger = logging.getLogger(__name__)

#: Default corpus, shipped in the image under `scenarios/acts/`.
DEFAULT_SUITE = Path(__file__).resolve().parent / 'scenarios' / 'acts' / 'suite.acts.yaml'

#: The identifier the SUT goes by, matching `itk_runner.SUT_ID`.
SUT_ID = 'current'

#: Protocol bindings as the agent card spells them, mapped to ACTS's names.
#: The card says `JSONRPC` / `GRPC` / `HTTP_JSON`; ACTS says `rest` for the
#: last one. The two vocabularies are separate on purpose, so the translation
#: lives here rather than either enum growing the other's spelling.
_CARD_BINDING = {
    'JSONRPC': TransportBinding.JSONRPC,
    'GRPC': TransportBinding.GRPC,
    'HTTP_JSON': TransportBinding.REST,
    'HTTP+JSON': TransportBinding.REST,
    'REST': TransportBinding.REST,
}


class ActsRunError(RuntimeError):
    """The conformance run could not be set up or completed."""


@dataclass(frozen=True)
class ActsRun:
    """One conformance run's outcome."""

    results: list[TestResult]
    suite: LoadedSuite
    transport: TransportBinding
    duration_ms: int
    agent_card: dict[str, Any] = field(default_factory=dict)
    declared_behaviors: frozenset[str] | None = None


async def fetch_agent_card(base_url: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """Read the well-known agent card off a running agent."""
    url = f'{base_url.rstrip("/")}/.well-known/agent-card.json'
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ActsRunError(f'cannot read the agent card at {url}: {exc}') from exc


def interface_for(
    card: dict[str, Any], binding: TransportBinding
) -> tuple[str, str | None]:
    """Where ``binding`` is served, per the card.

    Returns the URL and, for JSON-RPC, the path component to use as the
    dispatcher's ``rpc_path``.

    Prefers protocol version 1.0 when the card advertises several: the ITK
    agents publish a 0.3 interface at the same URL for traversal compat, and
    picking it for a conformance run would test the wrong protocol.
    """
    candidates = [
        i for i in (card.get('supportedInterfaces') or [])
        if _CARD_BINDING.get(str(i.get('protocolBinding', '')).upper()) is binding
    ]
    if not candidates:
        advertised = sorted({
            str(i.get('protocolBinding')) for i in (card.get('supportedInterfaces') or [])
        })
        raise ActsRunError(
            f'the agent card advertises no {binding.value} interface; '
            f'it lists {advertised or "nothing"}'
        )

    chosen = next(
        (i for i in candidates if str(i.get('protocolVersion', '')) == '1.0'),
        candidates[0],
    )
    url = str(chosen.get('url') or '')
    if not url:
        raise ActsRunError(f'the {binding.value} interface on the card has no url')
    return url, chosen.get('protocolVersion')


def build_dispatcher(
    card: dict[str, Any], binding: TransportBinding, base_url: str
) -> Dispatcher:
    """Construct the dispatcher for ``binding`` against a running agent.

    ``base_url`` is the agent's HTTP root — where the card lives — which is
    not necessarily where the binding is mounted.
    """
    url, _ = interface_for(card, binding)

    if binding is TransportBinding.GRPC:
        # The card gives `host:port` for gRPC, sometimes with a scheme.
        target = url.removeprefix('http://').removeprefix('https://').rstrip('/')
        return for_binding(binding, target, agent_card_url=base_url)

    # For both HTTP bindings the *mount point* is the base, not the host root.
    # A raw step writes an absolute path — `POST /` for JSON-RPC,
    # `GET /tasks/x` for REST — and means it relative to where the binding
    # lives. An agent mounting JSON-RPC at `/jsonrpc/` would otherwise get
    # every raw step 404'd at the host root, which reads as a conformance
    # failure and is nothing of the kind.
    mount = url.rstrip('/')

    if binding is TransportBinding.JSONRPC:
        # With the mount as the base, the endpoint itself is just `/`.
        return for_binding(binding, mount, rpc_path='/', agent_card_url=base_url)

    return for_binding(binding, mount, agent_card_url=base_url)


def sut_repo_root() -> Path:
    """The SDK checkout the SUT was mounted from.

    ``mount_dir()`` points at the SDK's ``itk/``; the contract file sits at
    ``acts/sut-behaviors.yaml`` beside it, in the repo root.
    """
    return mount_dir().parent


async def run(
    *,
    transport: TransportBinding,
    suite_path: Path | None = None,
    test_ids: list[str] | None = None,
    variables: dict[str, Any] | None = None,
    capabilities: list[RunnerRequirement] | None = None,
    gate_on_behaviors: bool = True,
    log_dir: Path | None = None,
) -> ActsRun:
    """Start the SUT, run the corpus against it, and collect the results."""
    suite = load_suite(suite_path or DEFAULT_SUITE)
    if test_ids:
        selected = [t for t in suite.tests if t.id in set(test_ids)]
        missing = set(test_ids) - {t.id for t in selected}
        if missing:
            raise ActsRunError(f'no such test(s) in the corpus: {sorted(missing)}')
        suite = LoadedSuite(
            tests=selected,
            variables=suite.variables,
            sources=suite.sources,
            rewrites=suite.rewrites,
        )

    declared = None
    if gate_on_behaviors:
        try:
            declared = sut_behaviors.declared_by(sut_repo_root())
        except sut_behaviors.BehaviorsFileError as exc:
            raise ActsRunError(str(exc)) from exc
        if declared is None:
            logger.warning(
                'No %s in the SUT checkout — behaviour gating is off. Tests '
                'needing a tck-* prefix will run and probably fail.',
                sut_behaviors.CONTRACT_PATH,
            )

    started = time.monotonic()
    with Cluster(log_dir=log_dir) as cluster:
        outcomes = await asyncio.to_thread(
            cluster.start_all, [TargetSpec(kind=Kind.MOUNT)], log_names=['acts_sut'],
        )
        outcome = outcomes[0]
        if not outcome.ok():
            raise ActsRunError(
                f'the code under test failed to start: '
                f'{outcome.error.stage.value}: {outcome.error}'
            )

        handle = outcome.handle
        base_url = f'http://127.0.0.1:{handle.http_port}'
        logger.info('SUT up at %s (grpc :%s)', base_url, handle.grpc_port)

        card = await fetch_agent_card(base_url)
        dispatcher = build_dispatcher(card, transport, base_url)

        async with dispatcher:
            runner = Runner(
                dispatcher,
                variables=variables or {},
                agent_card=card,
                sut_behaviors=declared,
                capabilities=capabilities or (),
            )
            results = await runner.run_suite(suite)

    return ActsRun(
        results=results,
        suite=suite,
        transport=transport,
        duration_ms=int((time.monotonic() - started) * 1000),
        agent_card=card,
        declared_behaviors=declared,
    )


def to_report(
    run_result: ActsRun,
    *,
    sdk_name: str,
    sdk_version: str = 'unknown',
    language: str = 'unknown',
    repository: str | None = None,
) -> dict[str, Any]:
    """Render a completed run as a §13 report document."""
    sdk: dict[str, str] = {
        'name': sdk_name,
        'version': sdk_version,
        'language': language,
    }
    if repository:
        sdk['repository'] = repository
    return report_writer.build(
        run_result.results,
        run_result.suite,
        sdk=sdk,
        transport=run_result.transport,
        duration_ms=run_result.duration_ms,
    )


__all__ = [
    'DEFAULT_SUITE',
    'SUT_ID',
    'ActsRun',
    'ActsRunError',
    'build_dispatcher',
    'fetch_agent_card',
    'interface_for',
    'run',
    'sut_repo_root',
    'to_report',
]
