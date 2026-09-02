"""The HTTP+JSON/REST binding (A2A §11).

The operation picks a method and a path template; params split between the
path, the query string and the body according to the method. Errors are a
``google.rpc.Status`` document, and the abstract error name is recovered from
the ``ErrorInfo.reason`` inside it rather than from the HTTP status, which is
not injective.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Mapping

import httpx

from test_suite.acts.dispatcher.base import StreamEvent, WireError, WireResponse
from test_suite.acts.dispatcher.http_base import HttpDispatcher
from test_suite.acts.dispatcher.params import adapt
from test_suite.acts.schema import HttpMethod, Operation, TransportBinding
from test_suite.acts.wire_map import (
    REST_CONTENT_TYPE,
    binding_for_operation,
    error_for_reason,
)


#: Methods that carry their remaining params as a query string rather than a
#: body. ``DELETE`` is here because A2A gives it no request body.
_QUERY_METHODS = frozenset({HttpMethod.GET, HttpMethod.DELETE})


class RestDispatcher(HttpDispatcher):
    """Dispatches ACTS operations over the HTTP+JSON/REST binding."""

    binding = TransportBinding.REST
    content_type = REST_CONTENT_TYPE

    @staticmethod
    def _split_params(
        operation: Operation, params: Mapping[str, Any] | None
    ) -> tuple[str, dict[str, Any]]:
        """Fill the path template, returning the path and the leftover params.

        Adaptation runs first: ``create_push_config`` takes ``taskId`` from
        the path *and* flattens the nested config into the body, so splitting
        before reshaping would look for ``taskId`` in the wrong place.
        """
        return binding_for_operation(operation).format_path(
            adapt(operation, params)
        )

    async def dispatch(
        self,
        operation: Operation,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> WireResponse:
        binding = binding_for_operation(operation)
        if binding.http_only:
            return await self._get_agent_card(headers)

        path, rest = self._split_params(operation, params)
        query = rest if binding.rest_method in _QUERY_METHODS else None
        body = None if binding.rest_method in _QUERY_METHODS else rest

        response = await self._request(
            binding.rest_method.value,
            self._url(path),
            headers=self._headers(headers, content_type=self.content_type),
            params=_stringify(query) if query else None,
            json=body if body else None,
        )
        parsed, text = self._parse(response)
        return WireResponse(
            status=response.status_code,
            # The body *is* the response message on this binding — there is no
            # envelope to unwrap, which is why `expect.body` reads the same
            # here as it does over JSON-RPC.
            payload=parsed,
            error=self._error_from(response, parsed),
            headers=dict(response.headers),
            raw_body=text,
        )

    def _error_from(self, response: httpx.Response, parsed: Any) -> WireError | None:
        """Read a ``google.rpc.Status`` error document (A2A §11.6).

        Keyed off the HTTP status, not the presence of an ``error`` member: on
        this binding a failure is a non-2xx, and a SUT that returns 500 with
        an empty or non-JSON body must still be reported rather than read as
        success.
        """
        if response.status_code < 400:
            return None

        body = parsed.get('error') if isinstance(parsed, dict) else None
        if not isinstance(body, dict):
            return WireError(
                message=f'HTTP {response.status_code} with no google.rpc.Status body',
                raw=parsed,
            )

        reason, details = self._error_info(body.get('details'))
        status = body.get('status')
        return WireError(
            message=str(body.get('message', '')),
            error_type=error_for_reason(reason) if reason else None,
            status=status if isinstance(status, str) else None,
            reason=reason,
            details=details,
            raw=body,
        )

    async def stream(
        self,
        operation: Operation,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self._streaming_binding(operation)
        binding = binding_for_operation(operation)
        path, rest = self._split_params(operation, params)
        async for event in self._stream_sse(
            binding.rest_method.value,
            path,
            payload=rest,
            headers=headers,
        ):
            yield event


def _stringify(params: Mapping[str, Any]) -> dict[str, str]:
    """Render query params the way HTTP wants them.

    ``httpx`` would send a bool as ``True``; the wire spelling is ``true``.
    """
    out: dict[str, str] = {}
    for key, value in params.items():
        if isinstance(value, bool):
            out[key] = 'true' if value else 'false'
        else:
            out[key] = str(value)
    return out
