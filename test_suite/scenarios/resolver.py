"""Bind roles to concrete agents: ``traversal/v1`` → executable scenarios.

This is the join between the two halves of the consolidation. A scenario says
*what* it wants — a SUT, some peers described by SDK and version line, a
topology — and ``matrix.yaml`` says *which* repo and ref each of those is
today. Neither knows about the other until here.

The output is :class:`ResolvedScenario`, deliberately field-for-field the
legacy scenario shape, so everything downstream (the traversal engine, the
cluster planner, the results format) is untouched by the new schema.

Resolution does three things:

  * expands roles to agent identifiers, including the ``peers: all`` macro;
  * expands the plural fields to a Cartesian product of executable scenarios;
  * turns ``topology`` into the edge list the engine expects.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from test_suite.launcher.matrix import Matrix, MatrixError
from test_suite.scenarios.exclusions import KnownFailures
from test_suite.scenarios.schema import (
    PEER_PLACEHOLDER,
    SUT_ID,
    Behavior,
    Expand,
    LegacyScenario,
    Tier,
    Transport,
    TraversalScenarioV1,
)
from test_suite.scenarios.topology import topology_to_edges


class ResolutionError(ValueError):
    """A scenario cannot be bound to concrete agents."""


@dataclass(frozen=True)
class ResolvedScenario:
    """One executable scenario.

    Mirrors the legacy scenario fields exactly — ``sdks`` are concrete agent
    identifiers and ``edges`` are index strings — so a resolved traversal/v1
    scenario and a legacy one are indistinguishable to the runner.
    """

    name: str
    sdks: list[str]
    behavior: str
    edges: list[str] | None = None
    protocols: list[str] | None = None
    streaming: bool = False
    build_subtests: bool = False
    # Carried for the nightly metrics record and for diffing; not used to run.
    tier: str = Tier.NIGHTLY.value

    def peer_ids(self) -> list[str]:
        return [s for s in self.sdks if s != SUT_ID]


@dataclass
class ResolutionReport:
    """What resolution produced, and what it deliberately left out.

    ``skipped`` exists because silently dropping a scenario is the failure
    mode this whole story is meant to remove: a run that tests less than the
    file describes, and still goes green. Callers surface these.
    """

    scenarios: list[ResolvedScenario] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    # Scenarios that still run, but with a peer removed by a known failure.
    # Reported separately from `skipped`: the scenario is not lost, but it
    # covers less than the file says, and that must be visible too.
    # (scenario name, agent removed, why). One entry per removed agent, not
    # per scenario, so callers can group by cause instead of printing the
    # same rationale once per scenario.
    trimmed: list[tuple[str, str, str]] = field(default_factory=list)


def resolve(
    scenarios: list[TraversalScenarioV1 | LegacyScenario],
    matrix: Matrix,
    *,
    sut_sdk: str | None = None,
    known_failures: KnownFailures | None = None,
) -> list[ResolvedScenario]:
    """Resolve a mixed batch of scenarios. Convenience over :func:`resolve_all`."""
    return resolve_all(
        scenarios, matrix, sut_sdk=sut_sdk, known_failures=known_failures,
    ).scenarios


def resolve_all(
    scenarios: list[TraversalScenarioV1 | LegacyScenario],
    matrix: Matrix,
    *,
    sut_sdk: str | None = None,
    known_failures: KnownFailures | None = None,
) -> ResolutionReport:
    """Resolve a batch that may mix both schemas.

    Args:
        scenarios: Parsed scenarios, legacy or traversal/v1 in any order.
        matrix: Loaded ``matrix.yaml``.
        sut_sdk: Which SDK is under test, e.g. ``'python'``. Used to evaluate
            ``test_when`` and to expand ``include_own_lines``. When omitted
            nothing is filtered and no own-lines peers are added, which is
            what a local peer-only run wants.
        known_failures: Combinations to leave out. Defaults to the repo's
            ``known_failures.yaml``; pass an empty :class:`KnownFailures` to
            resolve everything, which is what the coverage diff wants.

    Returns:
        A report with the executable scenarios and the reason for any that
        were skipped.

    Raises:
        ResolutionError: A scenario names a peer the matrix doesn't have, or
            resolves to fewer than two agents. Both are authoring errors that
            must fail loudly — the whole point of resolving up front is to
            catch them before CI has built anything.
    """
    if known_failures is None:
        known_failures = KnownFailures.from_default()

    report = ResolutionReport()
    seen: dict[str, str] = {}

    for scenario in scenarios:
        if isinstance(scenario, LegacyScenario):
            report.scenarios.append(_from_legacy(scenario))
            continue

        skip = _skip_reason(scenario, sut_sdk)
        if skip:
            report.skipped.append((scenario.name, skip))
            continue

        report.scenarios.extend(_expand(
            scenario, matrix, sut_sdk,
            known_failures=known_failures,
            skipped=report.skipped,
            trimmed=report.trimmed,
        ))

    for s in report.scenarios:
        if s.name in seen:
            raise ResolutionError(
                f'duplicate scenario name {s.name!r}. Results are keyed by '
                f'name, so one would overwrite the other and the run would '
                f'silently test less than it reports.'
            )
        seen[s.name] = s.name

    return report


# ---------------------------------------------------------------------------
# Legacy passthrough
# ---------------------------------------------------------------------------


def _from_legacy(scenario: LegacyScenario) -> ResolvedScenario:
    """Legacy scenarios are already concrete; nothing to bind."""
    return ResolvedScenario(
        name=scenario.name,
        sdks=list(scenario.sdks),
        behavior=scenario.behavior,
        edges=scenario.edges,
        protocols=scenario.protocols,
        streaming=scenario.streaming,
        build_subtests=scenario.build_subtests,
    )


# ---------------------------------------------------------------------------
# traversal/v1
# ---------------------------------------------------------------------------


_MIN_AGENTS = 2


def _skip_reason(scenario: TraversalScenarioV1, sut_sdk: str | None) -> str | None:
    if scenario.test_when is None or sut_sdk is None:
        return None
    if sut_sdk not in scenario.test_when.sut_sdk:
        return (
            f'test_when.sut_sdk is {scenario.test_when.sut_sdk}, '
            f'SUT is {sut_sdk!r}'
        )
    return None


def _expand(  # noqa: PLR0913
    scenario: TraversalScenarioV1,
    matrix: Matrix,
    sut_sdk: str | None = None,
    known_failures: KnownFailures | None = None,
    skipped: list[tuple[str, str]] | None = None,
    trimmed: list[tuple[str, str, str]] | None = None,
) -> list[ResolvedScenario]:
    """Cartesian product over the plural fields, then over peers if per-peer."""
    known = known_failures if known_failures is not None else KnownFailures()
    variants = list(itertools.product(
        scenario.behavior_variants(),
        scenario.transport_variants(),
        scenario.streaming_options(),
    ))
    multi = _multi_axes(scenario)

    out = []
    for behavior, transports, streaming in variants:
        for sdks, used, peer_label in _groups(
            scenario, matrix, transports, sut_sdk
        ):
            if len(sdks) < _MIN_AGENTS:
                raise ResolutionError(
                    f'{scenario.name!r}: resolves to {len(sdks)} agent(s) '
                    f'({sdks}) for transports {[t.value for t in transports]}; '
                    f'a traversal needs at least {_MIN_AGENTS}. '
                    f'Check the peer list against matrix.yaml transports.'
                )

            name = _name(scenario, behavior, used, streaming, multi, peer_label)
            kept, dropped = _apply_exclusions(
                sdks, known, used, behavior, streaming, sut_sdk,
            )
            if dropped:
                # An explicit edge list is indexed by agent position, so
                # removing one would silently rewire the graph.
                unrunnable = scenario.edges is not None or len(kept) < _MIN_AGENTS
                if unrunnable:
                    why = '; '.join(
                        f'{a} — {e.summary()}' for a, e in dropped  # type: ignore[union-attr]
                    )
                    if skipped is not None:
                        skipped.append((name, f'known failure — {why}'))
                    continue
                if trimmed is not None:
                    trimmed.extend(
                        (name, a, e.summary()) for a, e in dropped  # type: ignore[union-attr]
                    )
                sdks = kept

            edges = (
                scenario.edges
                if scenario.edges is not None
                else topology_to_edges(scenario.topology, len(sdks))
            )
            out.append(ResolvedScenario(
                name=name,
                sdks=sdks,
                behavior=behavior.value,
                edges=edges,
                protocols=[t.value for t in used],
                streaming=streaming,
                build_subtests=scenario.build_subtests,
                tier=scenario.tier.value,
            ))
    return out


def _apply_exclusions(  # noqa: PLR0913
    sdks: list[str],
    known: KnownFailures,
    transports: list[Transport],
    behavior: Behavior,
    streaming: bool,
    sut_sdk: str | None,
) -> tuple[list[str], list[tuple[str, object]]]:
    """Split agents into those that stay and those a known failure removes.

    Evaluated per peer against the SUT, because every rule describes a
    *pair* — "java cannot talk to python_v03", "ts_v03 needs a TypeScript
    counterpart for grpc". A rule naming no agents matches every peer, which
    empties the scenario and skips it; that is right for a rule about a
    transport or behaviour as a whole.

    Assumes the pair is (SUT, peer). In a peer-only scenario
    (``include_sut: false``) ``current`` is not in the graph, so a rule about
    two specific peers cannot be expressed, and one naming ``current`` would
    match every peer. No shipped rule does either.
    """
    protocols = [t.value for t in transports]
    kept: list[str] = []
    dropped: list[tuple[str, object]] = []

    for agent in sdks:
        if agent == SUT_ID:
            kept.append(agent)
            continue
        hit = known.find(
            sdks=[SUT_ID, agent], protocols=protocols,
            behavior=behavior.value, streaming=streaming, sut_sdk=sut_sdk,
        )
        (dropped.append((agent, hit)) if hit else kept.append(agent))
    return kept, dropped


def _groups(
    scenario: TraversalScenarioV1,
    matrix: Matrix,
    transports: list[Transport],
    sut_sdk: str | None = None,
) -> list[tuple[list[str], list[Transport], str | None]]:
    """The agent groups this scenario runs as, with each group's transports.

    Returns one entry for ``expand: together``, and one per peer for
    ``expand: per_peer``. The third element labels the peer for naming, and
    is ``None`` when there is nothing to disambiguate.
    """
    peers = _peer_ids(scenario, matrix, transports, sut_sdk)
    sut = [SUT_ID] if scenario.roles.include_sut else []

    if scenario.expand is Expand.TOGETHER:
        # A peer that can't speak the transport has to leave the graph — the
        # hop to it would fail and take the whole traversal with it. This is
        # what a2a-python's and a2a-java's hand-written "Star Topology (No Go
        # v03) - HTTP_JSON" scenarios encoded by omission; expressing it
        # through matrix.yaml instead means one declaration covers every
        # transport and the capability is stated in exactly one place.
        usable = [p for p in peers if _supports(p, matrix, transports)]
        return [(sut + usable, transports, None)]

    groups = []
    for peer in peers:
        # Per-peer, a partially-capable peer is still worth running over what
        # it does speak — unlike the together case, where it has to leave the
        # graph. This is what reproduces "current vs go_v03" running
        # jsonrpc+grpc alongside "current vs python_v10" running all three.
        usable = _intersect(peer, matrix, transports)
        if not usable:
            continue
        groups.append((sut + [peer], usable, peer))
    return groups


def _peer_ids(
    scenario: TraversalScenarioV1,
    matrix: Matrix,
    transports: list[Transport],
    sut_sdk: str | None = None,
) -> list[str]:
    """Peer identifiers, in scenario order (or matrix order for ``all``)."""
    if scenario.roles.peers == 'all':
        if scenario.expand is Expand.PER_PEER:
            # Capability is applied per group below, so every line is a
            # candidate here; one that shares no transport drops out there.
            return [e.agent_id for e in matrix.entries()]
        wanted = [t.value for t in transports]
        return [e.agent_id for e in matrix.entries() if e.supports(wanted)]

    ids = []
    for peer in scenario.roles.peers:
        agent_id = peer.agent_id()
        try:
            # Validates the (sdk, line) pair exists. Transport filtering is
            # deliberately not done here: _groups drops a peer that can't
            # speak a requested transport, named or selected by `all` alike
            # (see _supports/_intersect), so naming a peer does not exempt it.
            matrix.resolve(agent_id)
        except MatrixError as e:
            raise ResolutionError(f'{scenario.name!r}: {e}') from None
        ids.append(agent_id)

    ids.extend(_own_lines(scenario, matrix, transports, sut_sdk, exclude=ids))
    return ids


def _own_lines(
    scenario: TraversalScenarioV1,
    matrix: Matrix,
    transports: list[Transport],
    sut_sdk: str | None,
    exclude: list[str],
) -> list[str]:
    """The SUT's own SDK's released lines, for ``include_own_lines``.

    Filtered by transport the same way ``peers: all`` is under
    ``expand: together``: a line that can't speak every requested transport
    would break the graph. That reproduces a2a-java's pairing of a
    four-peer jsonrpc+grpc star with a three-peer http_json one.
    """
    if not scenario.roles.include_own_lines or sut_sdk is None:
        return []
    wanted = [t.value for t in transports]
    return [
        e.agent_id
        for e in matrix.entries()
        if e.sdk == sut_sdk
        and e.agent_id not in exclude
        and (scenario.expand is Expand.PER_PEER or e.supports(wanted))
    ]


def _intersect(
    agent_id: str, matrix: Matrix, transports: list[Transport]
) -> list[Transport]:
    """Requested transports this peer can actually speak, in requested order."""
    try:
        capability = matrix.resolve(agent_id).transports
    except MatrixError:
        # 'current' and anything else outside the matrix: assume it can do
        # whatever was asked. The SUT's capability isn't the matrix's to know.
        return transports
    return [t for t in transports if t.value in capability]


def _supports(
    agent_id: str, matrix: Matrix, transports: list[Transport]
) -> bool:
    """Can this peer speak every requested transport?

    Unknown ids pass: the SUT is not in the matrix, and its capability is not
    the matrix's to decide.
    """
    return len(_intersect(agent_id, matrix, transports)) == len(transports)


def _multi_axes(scenario: TraversalScenarioV1) -> set[str]:
    """Which axes actually vary, so single-variant names stay untouched."""
    axes = set()
    if len(scenario.behavior_variants()) > 1:
        axes.add('behavior')
    if len(scenario.transport_variants()) > 1:
        axes.add('transports')
    if len(scenario.streaming_options()) > 1:
        axes.add('streaming')
    return axes


def _name(  # noqa: PLR0913
    scenario: TraversalScenarioV1,
    behavior: Behavior,
    transports: list[Transport],
    streaming: bool,
    multi: set[str],
    peer_label: str | None = None,
) -> str:
    """Build a unique name per expanded variant.

    Only axes that actually vary contribute a suffix, so a scenario written
    with singular fields keeps exactly the name its author gave it. That
    matters: results are keyed by name, and the nightly history is a
    time series per name.
    """
    base = scenario.name
    if peer_label is not None:
        base = (
            base.replace(PEER_PLACEHOLDER, peer_label)
            if PEER_PLACEHOLDER in base
            else f'{base} - {peer_label}'
        )

    parts = [base]
    if 'behavior' in multi:
        parts.append(behavior.value)
    if 'transports' in multi:
        parts.append('+'.join(t.value for t in transports))
    if 'streaming' in multi:
        parts.append('streaming' if streaming else 'non-streaming')
    return ' - '.join(parts)
