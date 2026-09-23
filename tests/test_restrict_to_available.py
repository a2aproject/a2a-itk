"""test_suite.scenarios.topology.restrict_to_available — the shared trim/skip.

One rule, used by both the resolver (a known_failures.yaml drop) and the runner
(a peer that fails to start): given the agents still available, rebuild the
scenario for the survivors, or skip it when what's left can't run. The key
property this pins is that an explicit author edge list is never re-indexed —
it's skipped, the same as the resolver has always done — while a named topology
is rebuilt for the smaller set.
"""

from __future__ import annotations

from test_suite.scenarios.topology import (
    Topology,
    restrict_to_available,
    topology_to_edges,
)


def _usable(*names: str) -> set[str]:
    return set(names)


class TestNothingMissing:
    def test_author_edges_pass_through_unchanged(self):
        sdks = ['current', 'python_v10']
        edges = ['0->1', '1->0']
        got = restrict_to_available(sdks, None, edges, _usable('current', 'python_v10'))
        assert got == (sdks, edges)

    def test_topology_rebuilt_for_the_full_set(self):
        sdks = ['current', 'a', 'b']
        got = restrict_to_available(sdks, Topology.STAR, None, _usable('current', 'a', 'b'))
        assert got == (sdks, topology_to_edges(Topology.STAR, 3))


class TestTrim:
    def test_star_drops_a_leaf_and_rebuilds(self):
        sdks = ['current', 'python_v10', 'go_v10']
        kept, edges = restrict_to_available(
            sdks, Topology.STAR, None, _usable('current', 'go_v10'),
        )
        assert kept == ['current', 'go_v10']
        assert edges == topology_to_edges(Topology.STAR, 2)

    def test_chain_drops_middle_and_rebuilds(self):
        # This is the case the previous induced-subgraph path *skipped*
        # (remapping a chain unbalances it); rebuilding from the topology
        # trims it, matching the resolver.
        sdks = ['current', 'mid', 'end']
        kept, edges = restrict_to_available(
            sdks, Topology.CHAIN, None, _usable('current', 'end'),
        )
        assert kept == ['current', 'end']
        assert edges == topology_to_edges(Topology.CHAIN, 2)

    def test_complete_digraph_stays_complete(self):
        sdks = ['current', 'a', 'b']
        # euler topology and legacy edges=None both mean "complete digraph".
        assert restrict_to_available(
            sdks, Topology.EULER, None, _usable('current', 'a'),
        ) == (['current', 'a'], None)
        assert restrict_to_available(
            sdks, None, None, _usable('current', 'a'),
        ) == (['current', 'a'], None)


class TestSkip:
    def test_explicit_author_edges_are_never_reindexed(self):
        # topology None + an explicit edge list + a drop -> skip, exactly as
        # resolver._expand does. Positional indices must not be rewired.
        sdks = ['current', 'a', 'b']
        edges = ['0->1', '0->2', '1->0', '2->0']
        assert restrict_to_available(sdks, None, edges, _usable('current', 'a')) is None

    def test_two_agent_scenario_cannot_lose_one(self):
        assert restrict_to_available(
            ['current', 'python_v10'], Topology.STAR, None, _usable('current'),
        ) is None

    def test_below_min_agents(self):
        assert restrict_to_available(
            ['a', 'b', 'c'], Topology.STAR, None, _usable('a'), min_agents=2,
        ) is None
