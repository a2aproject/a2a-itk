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

import importlib
from typing import Any, Final

from test_suite.acts.dispatcher.base import (
    DispatchError,
    Dispatcher,
    StreamEvent,
    UnsupportedByBinding,
    WireError,
    WireResponse,
)
from test_suite.acts.dispatcher.params import adapt
from test_suite.acts.schema import TransportBinding


#: Concrete dispatcher -> the module it lives in. Loaded on first use rather
#: than up front: `grpc` is a C extension that costs a noticeable slice of the
#: package's import time, and most callers — anything reading or validating the
#: corpus — never open a connection at all.
_IMPLEMENTATIONS: Final[dict[str, str]] = {
    'JsonRpcDispatcher': 'jsonrpc',
    'RestDispatcher': 'rest',
    'GrpcDispatcher': 'grpc',
    'HttpDispatcher': 'http_base',
}

_FOR_BINDING: Final[dict[TransportBinding, str]] = {
    TransportBinding.JSONRPC: 'JsonRpcDispatcher',
    TransportBinding.REST: 'RestDispatcher',
    TransportBinding.GRPC: 'GrpcDispatcher',
}


def _load(name: str) -> type[Dispatcher]:
    module = importlib.import_module(f'{__name__}.{_IMPLEMENTATIONS[name]}')
    loaded = getattr(module, name)
    globals()[name] = loaded  # so the next lookup skips this entirely
    return loaded


def __getattr__(name: str) -> Any:
    """Resolve a dispatcher class on first reference (PEP 562)."""
    if name in _IMPLEMENTATIONS:
        return _load(name)
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


def for_binding(
    binding: TransportBinding,
    target: str,
    **kwargs: Any,
) -> Dispatcher:
    """Build the dispatcher for ``binding``.

    ``target`` is a base URL for the HTTP bindings and a ``host:port`` for
    gRPC. Remaining keyword arguments go to the concrete dispatcher.
    """
    return _load(_FOR_BINDING[binding])(target, **kwargs)


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
