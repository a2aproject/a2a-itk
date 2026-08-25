"""Where each agent in the current run is listening.

``matrix.yaml`` says which agents exist; this says where the ones started for
*this run* are. Passed down the call chain rather than held in a module
global, so one run cannot leak ports into the next.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass


_HOST = '127.0.0.1'


@dataclass(frozen=True)
class AgentEndpoint:
    """One running agent's ports."""

    http_port: int
    grpc_port: int

    @property
    def card_uri(self) -> str:
        """Base URI the agent card and JSON-RPC endpoint hang off."""
        return f'http://{_HOST}:{self.http_port}'


class AgentTable(Mapping[str, AgentEndpoint]):
    """Immutable map of agent identifier → endpoint, for one run.

    A ``Mapping`` so it can be inspected and iterated, but with no setter:
    the launcher builds it once from the handles it just started, and every
    consumer receives the same snapshot.
    """

    def __init__(self, endpoints: Mapping[str, AgentEndpoint]) -> None:
        self._endpoints = dict(endpoints)

    @classmethod
    def from_handles(cls, handles: Mapping[str, object]) -> 'AgentTable':
        """Build from the launcher's ``AgentHandle`` objects.

        Takes anything exposing ``http_port`` / ``grpc_port`` so this module
        needn't import the launcher, keeping the traversal engine free of a
        dependency on how agents get started.
        """
        return cls({
            agent_id: AgentEndpoint(
                http_port=handle.http_port,  # type: ignore[attr-defined]
                grpc_port=handle.grpc_port,  # type: ignore[attr-defined]
            )
            for agent_id, handle in handles.items()
        })

    def card_uri(self, agent_id: str) -> str:
        """Base URI for ``agent_id``.

        Raises:
            RuntimeError: The agent is not in this run's table. Deliberately
                not a ``ValueError``: ``_get_valid_subgraphs`` swallows those
                to skip untraversable subgraphs, and a peer that was never
                started must not be mistaken for one — it would vanish from
                the expansion instead of failing.
        """
        try:
            return self._endpoints[agent_id].card_uri
        except KeyError:
            known = ', '.join(sorted(self._endpoints)) or '(none)'
            raise RuntimeError(
                f'No running agent {agent_id!r} in this run; '
                f'started agents are: {known}'
            ) from None

    def __getitem__(self, agent_id: str) -> AgentEndpoint:
        return self._endpoints[agent_id]

    def __iter__(self) -> Iterator[str]:
        return iter(self._endpoints)

    def __len__(self) -> int:
        return len(self._endpoints)

    def __repr__(self) -> str:
        inner = ', '.join(
            f'{k}=:{v.http_port}' for k, v in sorted(self._endpoints.items())
        )
        return f'AgentTable({inner})'


EMPTY = AgentTable({})
