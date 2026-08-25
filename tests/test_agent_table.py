"""AgentTable — where this run's agents are listening.

Replaced the ``_AGENT_DEFS`` global. The tests that matter are the ones
pinning why it was replaced: no shared mutable state, and a missing agent
raising something ``_get_valid_subgraphs`` won't swallow.
"""

from __future__ import annotations

import pytest

from test_suite.agent_table import EMPTY, AgentEndpoint, AgentTable


class _Handle:
    """Duck-types launcher.AgentHandle for from_handles()."""

    def __init__(self, http_port, grpc_port):
        self.http_port = http_port
        self.grpc_port = grpc_port


class TestEndpoint:
    def test_card_uri_is_built_from_the_http_port(self):
        assert AgentEndpoint(8080, 9090).card_uri == 'http://127.0.0.1:8080'

    def test_is_frozen(self):
        with pytest.raises(Exception):  # noqa: B017, PT011
            AgentEndpoint(1, 2).http_port = 3


class TestFromHandles:
    def test_reads_both_ports(self):
        table = AgentTable.from_handles({'current': _Handle(101, 102)})
        assert table['current'] == AgentEndpoint(101, 102)

    def test_empty_cluster(self):
        assert len(AgentTable.from_handles({})) == 0


class TestCardUri:
    def test_returns_the_uri(self):
        table = AgentTable({'go_v10': AgentEndpoint(5000, 5001)})
        assert table.card_uri('go_v10') == 'http://127.0.0.1:5000'

    def test_missing_agent_raises_runtime_error(self):
        """Deliberately not ValueError: _get_valid_subgraphs catches those to
        skip untraversable subgraphs, so a never-started peer would vanish
        from the expansion instead of failing the run."""
        table = AgentTable({'current': AgentEndpoint(1, 2)})
        with pytest.raises(RuntimeError) as e:
            table.card_uri('ghost_v10')
        assert not isinstance(e.value, ValueError)

    def test_error_lists_what_did_start(self):
        table = AgentTable({
            'current': AgentEndpoint(1, 2), 'go_v10': AgentEndpoint(3, 4),
        })
        with pytest.raises(RuntimeError, match='current, go_v10'):
            table.card_uri('ghost_v10')

    def test_error_is_readable_for_an_empty_table(self):
        with pytest.raises(RuntimeError, match=r'\(none\)'):
            EMPTY.card_uri('anything')


class TestMappingBehaviour:
    def test_iterates_and_sizes(self):
        table = AgentTable({
            'a': AgentEndpoint(1, 2), 'b': AgentEndpoint(3, 4),
        })
        assert sorted(table) == ['a', 'b']
        assert len(table) == 2
        assert 'a' in table

    def test_snapshot_is_decoupled_from_the_source_dict(self):
        """A later mutation of the caller's dict must not retarget a live run."""
        source = {'a': AgentEndpoint(1, 2)}
        table = AgentTable(source)
        source['b'] = AgentEndpoint(3, 4)
        assert 'b' not in table

    def test_has_no_public_setter(self):
        table = AgentTable({'a': AgentEndpoint(1, 2)})
        with pytest.raises(TypeError):
            table['b'] = AgentEndpoint(3, 4)

    def test_repr_shows_ports(self):
        assert 'a=:1' in repr(AgentTable({'a': AgentEndpoint(1, 2)}))


class TestRegistryIsGone:
    """The old global must not come back by accident."""

    def test_test_suite_exports_no_agent_defs(self):
        import test_suite

        for name in ('_AGENT_DEFS', 'get_agent_def', '_http_port',
                     'get_agent_card_uri'):
            assert not hasattr(test_suite, name), (
                f'test_suite.{name} is back; agent addressing must go through '
                f'the AgentTable passed into create_test_suite'
            )

    def test_itk_runner_no_longer_wires_ports(self):
        import itk_runner

        assert not hasattr(itk_runner, 'wire_ports')
