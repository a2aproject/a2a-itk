"""Agent registry accessors in :mod:`test_suite`.

The registry no longer allocates ports — ``itk_service_v2`` injects them from
the launcher's handles for the duration of a run and removes them afterwards.
That leaves entries empty between runs, so "unknown agent" and "known agent,
not started" are distinct states and must not be conflated.
"""

from __future__ import annotations

import pytest

import test_suite


@pytest.fixture
def wired():
    """Inject a port pair the way itk_service_v2's adapter does, then undo it."""
    test_suite._AGENT_DEFS['python_v10']['httpPort'] = 9101  # noqa: SLF001
    test_suite._AGENT_DEFS['python_v10']['grpcPort'] = 9102  # noqa: SLF001
    yield
    test_suite._AGENT_DEFS['python_v10'].pop('httpPort', None)  # noqa: SLF001
    test_suite._AGENT_DEFS['python_v10'].pop('grpcPort', None)  # noqa: SLF001


class TestGetAgentDef:
    def test_known_agent_with_no_ports_is_not_an_error(self):
        # Empty dict is the resting state between runs — it must not read as
        # "unknown SDK" (it did when the guard was a truthiness check).
        assert test_suite.get_agent_def('python_v10') == {}

    def test_unknown_agent_raises(self):
        with pytest.raises(ValueError, match='Unknown SDK: nope_v99'):
            test_suite.get_agent_def('nope_v99')


class TestGetAgentCardUri:
    def test_uses_the_injected_port(self, wired):
        assert test_suite.get_agent_card_uri('python_v10') == 'http://127.0.0.1:9101'

    def test_unstarted_agent_raises_instead_of_yielding_a_none_port(self):
        # RuntimeError, not ValueError — _get_valid_subgraphs swallows
        # ValueError to skip untraversable subgraphs, and a peer the cluster
        # never started must not disappear down that path.
        with pytest.raises(RuntimeError, match='No port assigned'):
            test_suite.get_agent_card_uri('python_v10')

    def test_unknown_agent_raises(self):
        with pytest.raises(ValueError, match='Unknown SDK'):
            test_suite.get_agent_card_uri('nope_v99')


class TestSubgraphExpansionDoesNotHideMissingPeers:
    """`build_subtests` filters subgraphs by catching ValueError. A peer the
    cluster never started raises RuntimeError so it can't be filtered away.
    """

    def test_missing_peer_propagates_out_of_subgraph_expansion(self):
        with pytest.raises(RuntimeError, match='No port assigned'):
            test_suite._get_valid_subgraphs(  # noqa: SLF001
                sdks=['current', 'python_v10'],
                edges=None,
                behavior='send_message',
                protocols=['jsonrpc'],
            )


class TestTraversalUsesRegistryPorts:
    def test_instruction_targets_the_injected_port(self, wired):
        test_suite._AGENT_DEFS['current']['httpPort'] = 9201  # noqa: SLF001
        try:
            instruction, tokens = test_suite.create_test_suite(
                sdks=['current', 'python_v10'],
                protocols=['jsonrpc'],
                edges=['0->1', '1->0'],
            )
            rendered = str(instruction)
            assert 'http://127.0.0.1:9101' in rendered
            assert 'http://127.0.0.1:9201' in rendered
            assert tokens
        finally:
            test_suite._AGENT_DEFS['current'].pop('httpPort', None)  # noqa: SLF001

    def test_unstarted_peer_fails_loudly(self):
        with pytest.raises(RuntimeError, match='No port assigned'):
            test_suite.create_test_suite(
                sdks=['current', 'python_v10'],
                protocols=['jsonrpc'],
                edges=['0->1', '1->0'],
            )
