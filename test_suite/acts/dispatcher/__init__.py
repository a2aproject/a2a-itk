"""Transport adapters: one abstract operation, three wire protocols.

The runner asks for ``get_task`` and gets a :class:`WireResponse`; which of
JSON-RPC, REST or gRPC carried it is the dispatcher's business. Method names,
paths, status codes and error codes all come from
:mod:`test_suite.acts.wire_map`, so the three adapters cannot drift apart.

    from test_suite.acts.dispatcher import for_binding
    from test_suite.acts.schema import Operation, TransportBinding

    async with for_binding(TransportBinding.REST, 'http://localhost:9999') as d:
        response = await d.dispatch(Operation.GET_TASK, {'id': task_id})
        if response.ok:
            print(response.payload['status']['state'])
"""

from __future__ import annotations

from typing import Any

from test_suite.acts.dispatcher.base import (
    DispatchError,
    Dispatcher,
    StreamEvent,
    UnsupportedByBinding,
    WireError,
    WireResponse,
)
from test_suite.acts.dispatcher.grpc import GrpcDispatcher
from test_suite.acts.dispatcher.http_base import HttpDispatcher
from test_suite.acts.dispatcher.jsonrpc import JsonRpcDispatcher
from test_suite.acts.dispatcher.params import adapt
from test_suite.acts.dispatcher.rest import RestDispatcher
from test_suite.acts.schema import TransportBinding


_BY_BINDING: dict[TransportBinding, type[Dispatcher]] = {
    TransportBinding.JSONRPC: JsonRpcDispatcher,
    TransportBinding.REST: RestDispatcher,
    TransportBinding.GRPC: GrpcDispatcher,
}


def for_binding(
    binding: TransportBinding,
    target: str,
    **kwargs: Any,
) -> Dispatcher:
    """Build the dispatcher for ``binding``.

    ``target`` is a base URL for the HTTP bindings and a ``host:port`` for
    gRPC. Remaining keyword arguments go to the concrete dispatcher.
    """
    return _BY_BINDING[binding](target, **kwargs)


__all__ = [
    'DispatchError',
    'Dispatcher',
    'GrpcDispatcher',
    'HttpDispatcher',
    'JsonRpcDispatcher',
    'RestDispatcher',
    'StreamEvent',
    'UnsupportedByBinding',
    'WireError',
    'WireResponse',
    'adapt',
    'for_binding',
]
