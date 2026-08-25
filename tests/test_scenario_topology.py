"""Named topologies expand to the edge lists scenarios used to hand-write.

The corpus test at the bottom is the one that matters: it pins that every
edge list shipping across the five SDK repos is reproduced exactly by a named
topology. That is the evidence behind replacing `edges:` with `topology:` —
without it the migration is a guess.
"""

from __future__ import annotations

import pytest

from test_suite.scenarios.topology import (
    Topology,
    normalize_edges,
    topology_to_edges,
)


# Every distinct (agent count, edge list) pair found across all 154 scenarios
# in a2a-{python,go,js,java,rs}/itk/scenarios{,_full}.json. All four are stars.
CORPUS_SHAPES = [
    (2, ['0->1', '1->0']),
    (3, ['0->1', '0->2', '1->0', '2->0']),
    (4, ['0->1', '0->2', '0->3', '1->0', '2->0', '3->0']),
    (5, ['0->1', '0->2', '0->3', '0->4', '1->0', '2->0', '3->0', '4->0']),
]


class TestStar:
    def test_two_agents(self):
        assert topology_to_edges(Topology.STAR, 2) == ['0->1', '1->0']

    def test_five_agents_is_out_and_back_from_the_sut(self):
        assert topology_to_edges(Topology.STAR, 5) == [
            '0->1', '0->2', '0->3', '0->4',
            '1->0', '2->0', '3->0', '4->0',
        ]

    def test_peers_never_talk_to_each_other(self):
        edges = topology_to_edges(Topology.STAR, 4)
        assert all('0' in e.split('->') for e in edges)

    @pytest.mark.parametrize('n', range(2, 9))
    def test_is_eulerian(self, n):
        """In-degree must equal out-degree at every node or the traversal
        engine rejects the graph."""
        edges = topology_to_edges(Topology.STAR, n)
        _assert_balanced(edges, n)


class TestChain:
    def test_three_agents_is_a_single_loop(self):
        assert topology_to_edges(Topology.CHAIN, 3) == ['0->1', '1->2', '2->0']

    def test_collapses_to_star_at_two_agents(self):
        assert topology_to_edges(Topology.CHAIN, 2) == topology_to_edges(
            Topology.STAR, 2
        )

    @pytest.mark.parametrize('n', range(2, 9))
    def test_is_eulerian(self, n):
        _assert_balanced(topology_to_edges(Topology.CHAIN, n), n)

    @pytest.mark.parametrize('n', range(2, 9))
    def test_visits_every_agent_once(self, n):
        edges = topology_to_edges(Topology.CHAIN, n)
        assert len(edges) == n


class TestEuler:
    def test_defers_to_the_engine(self):
        """None means 'complete digraph'. The traversal engine already builds
        one when given no edges; duplicating that here would risk the two
        definitions drifting."""
        assert topology_to_edges(Topology.EULER, 4) is None

    def test_normalizes_to_the_full_mesh(self):
        assert normalize_edges(None, 3) == frozenset({
            '0->1', '0->2', '1->0', '1->2', '2->0', '2->1',
        })


class TestRejectsTooFewAgents:
    @pytest.mark.parametrize('topology', list(Topology))
    @pytest.mark.parametrize('n', [0, 1])
    def test_raises(self, topology, n):
        with pytest.raises(ValueError, match='at least 2 agents'):
            topology_to_edges(topology, n)


class TestNormalizeEdges:
    def test_order_is_irrelevant(self):
        """Edge order picks which Eulerian circuit gets walked, not which
        graph is traversed, so equivalence is set-based."""
        assert normalize_edges(['0->1', '1->0'], 2) == normalize_edges(
            ['1->0', '0->1'], 2
        )

    def test_whitespace_is_stripped(self):
        assert normalize_edges(['0 -> 1'], 2) == normalize_edges(['0->1'], 2)

    def test_explicit_mesh_equals_implicit(self):
        explicit = ['0->1', '0->2', '1->0', '1->2', '2->0', '2->1']
        assert normalize_edges(explicit, 3) == normalize_edges(None, 3)


class TestShippingCorpus:
    """Every edge list in the five SDK repos is a named topology."""

    @pytest.mark.parametrize(('n', 'edges'), CORPUS_SHAPES)
    def test_shape_is_a_star(self, n, edges):
        assert normalize_edges(topology_to_edges(Topology.STAR, n), n) == (
            normalize_edges(edges, n)
        )

    def test_two_agent_shape_is_degenerate(self):
        """106 of the 154 scenarios are two-agent, where all three topologies
        describe the same graph. Only the 48 larger ones actually discriminate
        — and every one of those is a star."""
        shapes = {
            normalize_edges(topology_to_edges(t, 2), 2) for t in Topology
        }
        assert len(shapes) == 1

    @pytest.mark.parametrize(('n', 'edges'), CORPUS_SHAPES[1:])
    def test_larger_shapes_are_unambiguously_stars(self, n, edges):
        matching = [
            t for t in Topology
            if normalize_edges(topology_to_edges(t, n), n)
            == normalize_edges(edges, n)
        ]
        assert matching == [Topology.STAR]


def _assert_balanced(edges: list[str], n: int) -> None:
    out_deg = dict.fromkeys(range(n), 0)
    in_deg = dict.fromkeys(range(n), 0)
    for e in edges:
        u, v = (int(x) for x in e.split('->'))
        out_deg[u] += 1
        in_deg[v] += 1
    assert out_deg == in_deg, f'unbalanced: out={out_deg} in={in_deg}'
