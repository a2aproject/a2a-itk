"""test_suite.induced_runnable_scenario — trim-where-safe / skip-otherwise.

The rule this pins: when a peer fails to start, a scenario keeps running with
that peer removed if the remaining graph is still a traversable circuit, and
is dropped whole when it isn't. It reuses the traversal engine's own edge
mapping and Eulerian check, so "safe to trim" here means exactly what it does
for build_subtests.
"""

from __future__ import annotations

from test_suite import induced_runnable_scenario
from test_suite.agent_table import AgentEndpoint, AgentTable


# Every agent any test might keep, at distinct ports. The function only ever
# addresses the agents it keeps, so an over-broad table is harmless.
_TABLE = AgentTable({
    name: AgentEndpoint(http_port=9000 + i, grpc_port=9500 + i)
    for i, name in enumerate(
        ['current', 'python_v10', 'go_v10', 'mid', 'end']
    )
})


def _available(*names: str) -> set[str]:
    return set(names)


class TestNothingMissing:
    def test_returns_originals_untouched(self):
        sdks = ['current', 'python_v10']
        edges = ['0->1', '1->0']
        got = induced_runnable_scenario(
            sdks, edges, _available('current', 'python_v10'), _TABLE,
        )
        assert got == (sdks, edges)


class TestTrimWhereSafe:
    def test_star_drops_a_leaf_and_reindexes_edges(self):
        # current<->python, current<->go; drop python -> current<->go.
        sdks = ['current', 'python_v10', 'go_v10']
        edges = ['0->1', '0->2', '1->0', '2->0']
        kept, kept_edges = induced_runnable_scenario(
            sdks, edges, _available('current', 'go_v10'), _TABLE,
        )
        assert kept == ['current', 'go_v10']
        assert kept_edges == ['0->1', '1->0']

    def test_complete_digraph_stays_none_edged(self):
        # edges=None means "the engine builds the mesh"; a smaller mesh is
        # still balanced, so it trims and stays None-edged.
        kept, kept_edges = induced_runnable_scenario(
            ['current', 'python_v10', 'go_v10'], None,
            _available('current', 'go_v10'), _TABLE,
        )
        assert kept == ['current', 'go_v10']
        assert kept_edges is None


class TestSkipOtherwise:
    def test_two_agent_scenario_cannot_lose_one(self):
        assert induced_runnable_scenario(
            ['current', 'python_v10'], ['0->1', '1->0'],
            _available('current'), _TABLE,
        ) is None

    def test_entry_point_down_is_unrunnable(self):
        # Entry point (index 0) is the peer that's down: nothing to POST to,
        # even though two other agents survive.
        assert induced_runnable_scenario(
            ['python_v10', 'go_v10', 'current'], ['0->1', '1->2', '2->0'],
            _available('go_v10', 'current'), _TABLE,
        ) is None

    def test_unbalanced_remainder_is_rejected(self):
        # A chain current->mid->end->current; drop the middle and what's left
        # (end->current only) is no longer Eulerian, so it's skipped, not
        # silently run as something else.
        assert induced_runnable_scenario(
            ['current', 'mid', 'end'], ['0->1', '1->2', '2->0'],
            _available('current', 'end'), _TABLE,
        ) is None
