"""Named traversal topologies, and their expansion to edge lists.

Authors pick a topology and the edge list follows from the agent count.
Index 0 is always the SUT; peers follow in scenario order.

An explicit ``edges:`` still wins, for graphs none of these names describe.
"""

from __future__ import annotations

import enum


class Topology(str, enum.Enum):
    """How agents are wired to each other within one scenario."""

    STAR = 'star'
    CHAIN = 'chain'
    EULER = 'euler'


# Two agents is the smallest traversable graph: one hop out, one back.
_MIN_NODES = 2


def topology_to_edges(topology: Topology, node_count: int) -> list[str] | None:
    """Expand a topology into the edge list for ``node_count`` agents.

    Args:
        topology: Which shape to build.
        node_count: Total agents, SUT included. Must be >= 2.

    Returns:
        Edge strings in ``"<from>-><to>"`` index form, or ``None`` for
        :attr:`Topology.EULER` — the traversal engine builds the complete
        digraph itself when handed no edges, and reproducing it here would
        only risk the two definitions drifting.

    Raises:
        ValueError: Fewer than two agents; nothing to traverse.
    """
    if node_count < _MIN_NODES:
        raise ValueError(
            f'{topology.value} topology needs at least {_MIN_NODES} agents, '
            f'got {node_count}'
        )

    if topology is Topology.STAR:
        # The SUT talks to every peer and every peer talks back; peers never
        # talk to each other. Out-and-back keeps every node balanced, which
        # is what makes the graph Eulerian.
        return (
            [f'0->{i}' for i in range(1, node_count)]
            + [f'{i}->0' for i in range(1, node_count)]
        )

    if topology is Topology.CHAIN:
        # One loop threading through every agent in order and back to the
        # start: 0->1, 1->2, ..., (n-1)->0.
        return [f'{i}->{(i + 1) % node_count}' for i in range(node_count)]

    return None


def normalize_edges(edges: list[str] | None, node_count: int) -> frozenset[str]:
    """Canonical form of an edge list, for comparing two scenarios.

    Edge *order* changes which Eulerian circuit the engine happens to walk
    but not which graph is traversed, so equivalence is set-based. ``None``
    is expanded to the complete digraph the engine would generate, so a
    scenario written as ``topology: euler`` compares equal to one that spelt
    the mesh out by hand.
    """
    if edges is None:
        return frozenset(
            f'{u}->{v}'
            for u in range(node_count)
            for v in range(node_count)
            if u != v
        )
    return frozenset(e.replace(' ', '') for e in edges)


def restrict_to_available(
    sdks: list[str],
    topology: Topology | None,
    edges: list[str] | None,
    usable: set[str] | frozenset[str],
    *,
    min_agents: int = _MIN_NODES,
) -> tuple[list[str], list[str] | None] | None:
    """Restrict a scenario's graph to the agents in ``usable``.

    One answer to "can what's left still run?", shared by both callers that
    ask it: the resolver, when a ``known_failures.yaml`` rule removes a peer,
    and the runner, when a peer fails to build/start. Keeping it in one place
    is the point — the two used to disagree on explicit-edge scenarios.

    Args:
        sdks: The scenario's agents.
        topology: The shape to rebuild from when agents are missing, or
            ``None`` when ``edges`` was an author-given explicit list.
        edges: The scenario's edges, or ``None`` for the complete digraph.
        usable: The agents actually available (peers a rule kept / that came
            up).
        min_agents: Smallest traversable graph; below it there's nothing to
            walk.

    Returns:
        The ``(sdks, edges)`` to run — unchanged when nothing is missing, or
        rebuilt for the smaller set — or ``None`` when the scenario can't
        survive the loss:

          * fewer than ``min_agents`` remain; or
          * an agent is missing and the edges are an explicit author list
            (``topology is None``): those indices are positional, so dropping
            a node would silently rewire the graph. A named topology instead
            carries its shape and is simply rebuilt for the smaller set, and
            the complete digraph stays complete.
    """
    kept = [s for s in sdks if s in usable]
    if len(kept) < min_agents:
        return None
    if len(kept) < len(sdks) and edges is not None and topology is None:
        return None
    if topology is not None:
        return kept, topology_to_edges(topology, len(kept))
    return kept, edges
