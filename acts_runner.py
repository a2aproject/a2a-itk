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
import contextlib
import dataclasses
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from test_suite.acts import behaviors as sut_behaviors
from test_suite.acts import report as report_writer
from test_suite.acts.dispatcher import Dispatcher, for_binding
from test_suite.acts.dispatcher.http_base import FOLLOW_REDIRECTS
from test_suite.acts.loader import LoadedSuite, load_suite
from test_suite.acts.runner import VERSION_HEADER, Runner, TestResult
from test_suite.acts.schema import RunnerRequirement, TransportBinding
from test_suite.acts.wire_map import WELL_KNOWN_AGENT_CARD_PATH
from test_suite.launcher import Cluster, TargetSpec
from test_suite.launcher.config import mount_dir
from test_suite.launcher.spec import Kind


logger = logging.getLogger(__name__)

#: Default corpus, shipped in the image under `scenarios/acts/`.
DEFAULT_SUITE = Path(__file__).resolve().parent / 'scenarios' / 'acts' / 'suite.acts.yaml'

#: The identifier the SUT goes by, matching `itk_runner.SUT_ID`.
SUT_ID = 'current'

#: The protocol version this runner speaks, sent on every request including
#: the bootstrap card fetch. Matches `Runner`'s own default.
SPEC_VERSION = '1.0'

#: Bearer token the runner presents on every abstract operation, so an
#: operation the spec puts behind authentication can still be exercised. A2A
#: §13.3 requires `Get Extended Agent Card` to be authenticated, and
#: `CARD-EXT-001` fetches it expecting success — which only a runner holding a
#: credential can ask for.
#:
#: Raw steps never carry it: `dispatch_raw` drops the dispatcher's defaults, so
#: `SEC-EXTCARD-001` still sends nothing and `SEC-EXTCARD-002` still sends the
#: insufficient token. That is the whole reason those two mean anything, and
#: why the abstract and raw step kinds must not share a header set.
#:
#: Not a secret — a SUT's `itk/` fixture has to recognise it to answer 200
#: rather than 401. ACTS declares the *capability* a runner needs
#: (`runner_requirements: [auth_credentials]`) but never says what the
#: credential is, so this pairing is ours and is documented in `acts/`.
ACTS_AUTH_TOKEN = 'itk-valid-token'

#: Offered by `SEC-EXTCARD-002` as a token that authenticates but does not
#: authorize; a fixture should answer 403 rather than 401.
ACTS_INSUFFICIENT_TOKEN = 'itk-insufficient-token'

#: Set in the SUT's environment for the second pass. An agent that honours it
#: advertises none of the optional capabilities *and* refuses the operations
#: they gate.
#:
#: Four tests — `CORE-CAP-001`, `CORE-CAP-002`, `SEC-EXTCARD-003`,
#: `PUSH-CFG-004` — assert that an agent lacking a capability answers
#: `UnsupportedOperationError`. Their preconditions therefore require the card
#: *not* to advertise it, so against a fully capable SUT they skip, and the
#: "unsupported" branch of the protocol goes untested by anybody. It cannot be
#: fixed by relaxing the gate: an agent that advertises streaming is obliged to
#: stream, so the assertion would simply be wrong.
#:
#: One agent cannot both stream and not stream, hence a second pass against a
#: second, deliberately diminished instance. Every SDK already gates these
#: operations on its own card, so honouring this usually means nothing more
#: than publishing a smaller `capabilities` block.
REDUCED_CAPABILITIES_ENV = 'ITK_ACTS_REDUCED_CAPABILITIES'

#: The capabilities the reduced pass expects the SUT to drop.
REDUCIBLE_CAPABILITIES = ('streaming', 'pushNotifications', 'extendedAgentCard')

#: How `Runner._unmet_precondition` words a skip the reduced pass can clear:
#: the card advertises something the test needs absent.
_CAPABILITY_SKIP = re.compile(r'agent card capability (\w+)=True, needs False')

#: Set in the SUT's environment for the auth pass. An agent that honours it
#: declares a security scheme on its card *and* enforces it.
#:
#: `SEC-AUTH-001/002/003/004/006` assert that an agent requiring a credential
#: rejects a request that lacks one. A2A conditions that obligation on the
#: agent's own declared requirements, so against an agent declaring none the
#: tests are correctly not applicable — and every ITK fixture declares none by
#: default, because the traversal suite dials it with no credential at all.
#:
#: One agent cannot both require and not require a credential, which is the
#: same shape as the reduced pass and the reason this is a separate SUT rather
#: than a flag on the first one. Enforcing during the main pass instead would
#: cost fourteen raw steps that must be *served*: an absent `Authorization`
#: header means "reject me" in `SEC-AUTH-001` and "serve me" in
#: `JSONRPC-ENV-001`, and no server can tell those two requests apart.
AUTH_ENFORCED_ENV = 'ITK_ACTS_AUTH'

#: How `Runner._unmet_precondition` words a skip the auth pass can clear: the
#: card declares no security requirement and the test needs one. Deliberately
#: distinct from `_CAPABILITY_SKIP` — the two passes move the SUT along
#: different axes and neither may claim the other's skips.
_SECURITY_SKIP = re.compile(r'agent card authentication=False, needs True')

#: Variables the corpus names that no document defines (spec §12.2).
RUNNER_VARIABLES: dict[str, Any] = {
    'insufficientAuthToken': ACTS_INSUFFICIENT_TOKEN,
    'otherUserTaskId': '00000000-0000-0000-0000-0000000000ff',
}

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
    """Read the well-known agent card off a running agent.

    Sent with `A2A-Version: 1.0`, like every other request the run makes.
    §3.6.2 makes an absent header mean **0.3**, so an SDK with a compat layer
    answers a bare GET with its v0.3 card — on which `extendedAgentCard` is not
    a capability at all but a top-level `supportsAuthenticatedExtendedCard`.
    Preconditions are evaluated against this card, so reading the wrong dialect
    silently skips whole groups of tests while the operations they cover work
    perfectly well.

    Redirects are not followed here either: the card decides what the whole
    run tests, so an agent that does not serve it where §8.6 says should fail
    the run loudly rather than have the harness go looking elsewhere.
    """
    url = f'{base_url.rstrip("/")}{WELL_KNOWN_AGENT_CARD_PATH}'
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=FOLLOW_REDIRECTS
    ) as client:
        try:
            response = await client.get(url, headers={VERSION_HEADER: SPEC_VERSION})
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ActsRunError(f'cannot read the agent card at {url}: {exc}') from exc


def interface_for(
    card: dict[str, Any], binding: TransportBinding
) -> tuple[str, str | None]:
    """Where ``binding`` is served, per the card.

    Returns the interface's URL and the protocol version it advertises.

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
    # Presented on abstract operations only; `dispatch_raw` drops it, which is
    # what keeps the unauthenticated `SEC-EXTCARD-*` probes honest.
    auth = {'Authorization': f'Bearer {ACTS_AUTH_TOKEN}'}

    if binding is TransportBinding.GRPC:
        # The card gives `host:port` for gRPC, sometimes with a scheme.
        target = url.removeprefix('http://').removeprefix('https://').rstrip('/')
        return for_binding(
            binding, target, agent_card_url=base_url, default_headers=auth
        )

    # For both HTTP bindings the *mount point* is the base, not the host root.
    # A raw step writes an absolute path — `POST /` for JSON-RPC,
    # `GET /tasks/x` for REST — and means it relative to where the binding
    # lives. An agent mounting JSON-RPC at `/jsonrpc/` would otherwise get
    # every raw step 404'd at the host root, which reads as a conformance
    # failure and is nothing of the kind.
    #
    # Passed exactly as advertised: the SDKs disagree about the trailing slash
    # and each serves only its own spelling, so `HttpDispatcher._url` keeps it
    # and trims only when joining a deeper path onto it.
    if binding is TransportBinding.JSONRPC:
        # With the mount as the base, the endpoint itself is just `/`.
        return for_binding(
            binding,
            url,
            rpc_path='/',
            agent_card_url=base_url,
            default_headers=auth,
        )

    return for_binding(
        binding, url, agent_card_url=base_url, default_headers=auth
    )


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
    # Merged here rather than by the caller, so every front end gets them. The
    # CLI used to pass them and `POST /run-acts` did not, which left
    # `{{insufficientAuthToken}}` unresolved on the service path and turned
    # `SEC-EXTCARD-002` into an error about the harness. A caller may still
    # override any of them.
    variables = {**RUNNER_VARIABLES, **(variables or {})}

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
    results, card = await _run_pass(
        suite,
        transport=transport,
        variables=variables,
        capabilities=capabilities,
        declared=declared,
        log_dir=log_dir,
        log_name='acts_sut',
    )

    for deviation in DEVIATIONS:
        results = await _rerun_deviation(
            results,
            suite,
            deviation=deviation,
            transport=transport,
            variables=variables,
            capabilities=capabilities,
            declared=declared,
            log_dir=log_dir,
        )

    return ActsRun(
        results=results,
        suite=suite,
        transport=transport,
        duration_ms=int((time.monotonic() - started) * 1000),
        agent_card=card,
        declared_behaviors=declared,
    )


async def _run_pass(
    suite: LoadedSuite,
    *,
    transport: TransportBinding,
    variables: dict[str, Any] | None,
    capabilities: list[RunnerRequirement] | None,
    declared: frozenset[str] | None,
    log_dir: Path | None,
    log_name: str,
) -> tuple[list[TestResult], dict[str, Any]]:
    """Start one SUT, run ``suite`` against it, and tear it down."""
    with Cluster(log_dir=log_dir) as cluster:
        outcomes = await asyncio.to_thread(
            cluster.start_all, [TargetSpec(kind=Kind.MOUNT)], log_names=[log_name],
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
            return await runner.run_suite(suite), card


@contextlib.contextmanager
def _sut_env(name: str) -> Iterator[None]:
    """Ask the next SUT this process spawns to deviate from its defaults.

    The launcher gives the child no explicit environment, so it inherits this
    one — which is why setting a variable here reaches the agent without any
    plumbing through `Cluster`. The corollary is that two SUTs alive at once
    cannot be told apart this way, so the deviations are serial passes.
    """
    previous = os.environ.get(name)
    os.environ[name] = '1'
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


@dataclass(frozen=True)
class Deviation:
    """A SUT started differently, to reach tests the default one cannot run.

    Some preconditions are mutually exclusive with the deployment the rest of
    the corpus needs: an agent cannot both advertise streaming and refuse it,
    nor both require a credential and serve the raw steps that must go
    unauthenticated. Rather than leave those branches of the protocol untested
    by anybody, the run starts a second agent configured to meet them and
    splices its verdicts in.

    Each deviation is keyed on the *skip reason* rather than on a list of test
    ids: the corpus is authored upstream and copied here, so a hard-coded list
    would silently stop matching when a test is added or renamed, whereas a
    derived selector leaves an uncleared skip visible in the report.
    """

    #: Environment variable the SUT must honour.
    env_var: str
    #: Which skips this pass is allowed to claim.
    skip_pattern: re.Pattern[str]
    #: Distinguishes this pass's agent log from the others'.
    log_name: str
    #: Human-readable, for the log line naming what is being re-run.
    description: str
    #: Why the SUT did not enter the mode, read off its card — or None if it
    #: did. Takes the card and the set of regex captures that caused the skips.
    took_effect: Callable[[dict[str, Any], set[str]], str | None]


def _capabilities_dropped(card: dict[str, Any], names: set[str]) -> str | None:
    """Whether the reduced SUT stopped advertising what it was asked to."""
    still = sorted(n for n in names if (card.get('capabilities') or {}).get(n))
    if not still:
        return None
    return (
        f'the card still advertises {", ".join(still)}; an agent honouring '
        f'this must publish a smaller `capabilities` block'
    )


def _security_declared(card: dict[str, Any], names: set[str]) -> str | None:
    """Whether the auth SUT came up declaring a credential requirement.

    Only that the card *claims* one. Whether the claim is true is what
    `SEC-AUTH-001` and the rest are for, and checking it here as well would
    duplicate the very tests this pass exists to run.
    """
    absent = [
        k for k in ('securitySchemes', 'securityRequirements') if not card.get(k)
    ]
    if not absent:
        return None
    return (
        f'the card declares no {", ".join(absent)}; an agent honouring this '
        f'must publish at least one security scheme and one security '
        f'requirement (A2A §7.3)'
    )


#: The deviations, in the order they run. Both are serial: each is a whole SUT
#: start, and `_sut_env` cannot differentiate two children alive at once.
DEVIATIONS: tuple[Deviation, ...] = (
    Deviation(
        env_var=REDUCED_CAPABILITIES_ENV,
        skip_pattern=_CAPABILITY_SKIP,
        log_name='acts_sut_reduced',
        description='capability-gated',
        took_effect=_capabilities_dropped,
    ),
    Deviation(
        env_var=AUTH_ENFORCED_ENV,
        skip_pattern=_SECURITY_SKIP,
        log_name='acts_sut_auth',
        description='authentication-gated',
        took_effect=_security_declared,
    ),
)


def _skips_matching(
    results: list[TestResult], pattern: re.Pattern[str]
) -> dict[str, str]:
    """Tests skipped for a reason ``pattern`` matches, and what it captured.

    The capture is the empty string for a pattern with no group, which is what
    the auth deviation wants: the skip names no particular thing to restore.
    """
    found = {}
    for result in results:
        match = pattern.search(result.skip_reason or '')
        if match is not None:
            found[result.id] = match.group(1) if match.groups() else ''
    return found


def _capability_skips(results: list[TestResult]) -> dict[str, str]:
    """Tests skipped only because the SUT advertises what they need absent."""
    return _skips_matching(results, _CAPABILITY_SKIP)


async def _rerun_deviation(
    results: list[TestResult],
    suite: LoadedSuite,
    *,
    deviation: Deviation,
    transport: TransportBinding,
    variables: dict[str, Any] | None,
    capabilities: list[RunnerRequirement] | None,
    declared: frozenset[str] | None,
    log_dir: Path | None,
) -> list[TestResult]:
    """Re-run the tests ``deviation`` can unblock against a second SUT.

    Returns ``results`` with those tests' verdicts replaced. A SUT that does
    not honour the environment variable keeps its original skips — the point
    is to run the tests, not to report a verdict nobody produced. So does a SUT
    that fails to start: an agent that will not come up in a deviated mode is
    worth a warning, not the loss of the whole binding's report.
    """
    blocked = _skips_matching(results, deviation.skip_pattern)
    if not blocked:
        return results

    logger.info(
        'Re-running %d %s test(s) against a %s SUT: %s',
        len(blocked), deviation.description, deviation.env_var,
        ', '.join(sorted(blocked)),
    )
    narrowed = LoadedSuite(
        tests=[t for t in suite.tests if t.id in blocked],
        variables=suite.variables,
        sources=suite.sources,
    )

    try:
        with _sut_env(deviation.env_var):
            rerun, card = await _run_pass(
                narrowed,
                transport=transport,
                variables=variables,
                capabilities=capabilities,
                declared=declared,
                log_name=deviation.log_name,
                log_dir=log_dir,
            )
    except ActsRunError as exc:
        logger.warning(
            'The %s pass could not run (%s), so %s stay skipped.',
            deviation.env_var, exc, ', '.join(sorted(blocked)),
        )
        return results

    refused = deviation.took_effect(card, set(blocked.values()))
    if refused is not None:
        logger.warning(
            'With %s set, %s, so %s stay skipped.',
            deviation.env_var, refused, ', '.join(sorted(blocked)),
        )
        return results

    # Stamped here rather than inside `Runner`, which has no idea it is being
    # run against anything unusual. Spec §12.8 requires it: a verdict from a
    # differently-configured instance is not interchangeable with one from the
    # agent the rest of the run tested.
    replacement = {
        r.id: dataclasses.replace(r, configuration=deviation.env_var)
        for r in rerun
    }
    return [replacement.get(r.id, r) for r in results]


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
    'AUTH_ENFORCED_ENV',
    'DEFAULT_SUITE',
    'DEVIATIONS',
    'REDUCED_CAPABILITIES_ENV',
    'SUT_ID',
    'ActsRun',
    'ActsRunError',
    'Deviation',
    'build_dispatcher',
    'fetch_agent_card',
    'interface_for',
    'run',
    'sut_repo_root',
    'to_report',
]
