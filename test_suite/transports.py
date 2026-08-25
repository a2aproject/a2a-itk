"""The wire protocols a hop can use.

Canonical definition, imported by everything that needs it: the traversal
engine, the matrix parser, the scenario schema and the coverage diff. Kept in
a leaf module with no package imports of its own so any of them can depend on
it without creating a cycle.
"""

from __future__ import annotations

import enum


class Transport(str, enum.Enum):
    """Wire protocol for a hop."""

    JSONRPC = 'jsonrpc'
    GRPC = 'grpc'
    HTTP_JSON = 'http_json'


ALL_TRANSPORTS: frozenset[str] = frozenset(t.value for t in Transport)

# Declaration order, for places that need a stable sequence rather than a set.
TRANSPORT_ORDER: tuple[str, ...] = tuple(t.value for t in Transport)
