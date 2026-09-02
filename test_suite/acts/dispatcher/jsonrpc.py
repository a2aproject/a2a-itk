"""The JSON-RPC 2.0 binding (A2A §9).

Every call is a POST of a JSON-RPC envelope to a single endpoint; the
operation is the envelope's ``method``, in PascalCase. Streaming is SSE, each
event carrying a JSON-RPC response envelope of its own.
"""

from __future__ import annotations

import itertools
from typing import Any, AsyncIterator, Mapping

import httpx

from test_suite.acts.dispatcher.base import StreamEvent, WireError, WireResponse
from test_suite.acts.dispatcher.http_base import HttpDispatcher
from test_suite.acts.dispatcher.params import adapt
from test_suite.acts.schema import Operation, TransportBinding
from test_suite.acts.wire_map import (
    JSONRPC_CONTENT_TYPE,
    binding_for_operation,
    error_for_jsonrpc_code,
)


#: Where the JSON-RPC endpoint sits by default. The agent card's ``url``
#: names the real one; the corpus's raw steps all post to ``/``.
DEFAULT_RPC_PATH = '/'


class JsonRpcDispatcher(HttpDispatcher):
    """Dispatches ACTS operations over the JSON-RPC binding."""

    binding = TransportBinding.JSONRPC
    content_type = JSONRPC_CONTENT_TYPE

    def __init__(
        self,
        base_url: str,
        *,
        rpc_path: str = DEFAULT_RPC_PATH,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
        default_headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(
            base_url,
            client=client,
            timeout=timeout,
            default_headers=default_headers,
        )
        self.rpc_path = rpc_path
        # Monotonic per dispatcher. JSON-RPC only requires that a response can
        # be matched to its request; reusing 1 everywhere would still work but
        # makes a captured transcript unreadable.
        self._ids = itertools.count(1)

    def _envelope(
        self,
        operation: Operation,
        params: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            'jsonrpc': '2.0',
            'id': next(self._ids),
            'method': binding_for_operation(operation).jsonrpc_method,
            'params': adapt(operation, params),
        }

    async def dispatch(
        self,
        operation: Operation,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> WireResponse:
        binding = binding_for_operation(operation)
        if binding.http_only:
            return await self._get_agent_card(headers)

        response = await self._request(
            'POST',
            self._url(self.rpc_path),
            headers=self._headers(headers, content_type=self.content_type),
            json=self._envelope(operation, params),
        )
        parsed, text = self._parse(response)
        error = self._error_from(response, parsed)
        return WireResponse(
            status=response.status_code,
            # The envelope's `result` is the operation's response message, and
            # that is what a test's `expect.body` mirrors. Handing back the
            # whole envelope would make every assertion start with `result:`.
            payload=parsed.get('result') if isinstance(parsed, dict) else None,
            error=error,
            headers=dict(response.headers),
            raw_body=text,
        )

    def _error_from(self, response: httpx.Response, parsed: Any) -> WireError | None:
        """Read the envelope's ``error`` member.

        A JSON-RPC error rides HTTP 200, so the status says nothing here; the
        presence of the member is the signal. A non-2xx status with no
        envelope is still reported, because a SUT that answers 500 with an
        HTML error page has failed in a way a test should see.
        """
        if isinstance(parsed, dict) and isinstance(parsed.get('error'), dict):
            err = parsed['error']
            code = err.get('code')
            reason, details = self._error_info(err.get('data'))
            return WireError(
                message=str(err.get('message', '')),
                error_type=(
                    error_for_jsonrpc_code(code) if isinstance(code, int) else None
                ),
                code=code if isinstance(code, int) else None,
                reason=reason,
                details=details,
                raw=err,
            )
        if response.status_code >= 400:
            return WireError(
                message=(
                    f'HTTP {response.status_code} with no JSON-RPC error '
                    f'envelope'
                ),
                raw=parsed,
            )
        return None

    def _unwrap_stream_event(self, data: Any) -> Any:
        """Each SSE event is a JSON-RPC envelope; the event is its ``result``.

        Left wrapped, every streaming assertion in the corpus would have to
        say ``result:`` on the JSON-RPC binding and not on the others, which
        is exactly the transport leak the abstract format exists to avoid.
        """
        if isinstance(data, dict) and 'result' in data:
            return data['result']
        return data

    async def stream(
        self,
        operation: Operation,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self._streaming_binding(operation)
        async for event in self._stream_sse(
            'POST',
            self.rpc_path,
            payload=self._envelope(operation, params),
            headers=headers,
        ):
            yield event
