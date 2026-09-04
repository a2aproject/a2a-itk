"""The gRPC binding (A2A §10).

Built on the generated stubs over ``grpc.aio``, so that the status and
trailing metadata a typed client would discard stay visible to the assertions.

Two structural differences from the HTTP bindings:

- **There is no raw request.** ``dispatch_raw`` raises
  :class:`~test_suite.acts.dispatcher.base.UnsupportedByBinding`. Below
  protobuf there is nothing an ACTS ``raw`` block could describe, which is why
  §4.4 requires an all-raw test to name its transport.
- **There is no HTTP status.** ``WireResponse.status`` is derived from the
  gRPC status by the canonical transcoding, so that the 86 binding-agnostic
  corpus tests asserting ``status: 200`` mean "it succeeded" here too.

The agent card is still fetched over plain HTTP — the generated service has no
``GetAgentCard`` RPC, because the card is what tells a client that gRPC is on
offer in the first place.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Mapping

import grpc
import httpx
from google.protobuf import empty_pb2
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from google.rpc import error_details_pb2, status_pb2
from grpc.aio import AioRpcError

from pyproto import a2a_pb2

from test_suite.acts.dispatcher.base import (
    DispatchError,
    Dispatcher,
    StreamEvent,
    UnsupportedByBinding,
    WireError,
    WireResponse,
)
from test_suite.acts.dispatcher.params import adapt
from test_suite.acts.schema import Operation, RawBlock, TransportBinding
from test_suite.acts.wire_map import (
    GRPC_SERVICE,
    WELL_KNOWN_AGENT_CARD_PATH,
    binding_for_operation,
    error_for_reason,
    http_status_for_grpc,
)


#: Trailing-metadata key carrying a serialized ``google.rpc.Status``. This is
#: where an ``ErrorInfo`` — and so the abstract error name — actually travels
#: on gRPC; ``details()`` is only the human-readable message.
STATUS_DETAILS_KEY = 'grpc-status-details-bin'


def _message_type(name: str) -> type:
    """Resolve a protobuf message type named in the wire map."""
    if name == 'google.protobuf.Empty':
        return empty_pb2.Empty
    try:
        return getattr(a2a_pb2, name)
    except AttributeError as exc:  # pragma: no cover - a wire_map typo
        raise DispatchError(f'unknown protobuf message {name!r}') from exc


def _to_dict(message: Any) -> Any:
    """ProtoJSON-encode a response message.

    ``preserving_proto_field_name=False`` gives camelCase and
    ``use_integers_for_enums=False`` gives enum *names*, which is what the
    corpus asserts (``state: TASK_STATE_COMPLETED``) and what the two HTTP
    bindings put on the wire. Defaults are dropped, matching the HTTP
    bindings' habit of omitting empty fields.
    """
    return MessageToDict(message)


class GrpcDispatcher(Dispatcher):
    """Dispatches ACTS operations over the gRPC binding."""

    binding = TransportBinding.GRPC

    def __init__(
        self,
        target: str,
        *,
        channel: grpc.aio.Channel | None = None,
        agent_card_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
        default_headers: Mapping[str, str] | None = None,
    ) -> None:
        """
        Args:
          target: ``host:port`` for the gRPC channel.
          channel: An existing channel to use. Injected by the tests so they
            can run against an in-process fake servicer; when given, this
            dispatcher does not close it.
          agent_card_url: Base URL for the well-known agent card, which is
            served over HTTP rather than gRPC. Defaults to ``http://{target}``,
            which is right when both are on one port and wrong otherwise —
            pass it explicitly when they are split.
        """
        self.target = target
        self.timeout = timeout
        self._default_headers = dict(default_headers or {})
        self._owns_channel = channel is None
        self._channel = channel or grpc.aio.insecure_channel(target)
        self._agent_card_url = (agent_card_url or f'http://{target}').rstrip('/')
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=timeout, follow_redirects=True
        )

    async def aclose(self) -> None:
        if self._owns_channel:
            await self._channel.close()
        if self._owns_http:
            await self._http.aclose()

    # -- request construction ----------------------------------------------

    def _metadata(
        self, headers: Mapping[str, str] | None
    ) -> list[tuple[str, str]]:
        """A2A service parameters ride as gRPC metadata (§10.2).

        Keys are lowercased because gRPC rejects uppercase metadata keys,
        while ACTS writes headers in HTTP's case-insensitive style.
        """
        merged = {**self._default_headers, **(headers or {})}
        return [(key.lower(), value) for key, value in merged.items()]

    def _build_request(
        self, operation: Operation, params: Mapping[str, Any] | None
    ) -> Any:
        binding = binding_for_operation(operation)
        message_type = _message_type(binding.grpc_request)
        payload = adapt(operation, params)
        try:
            return ParseDict(payload, message_type())
        except ParseError as exc:
            raise DispatchError(
                f'{operation.value}: params do not fit '
                f'{binding.grpc_request}: {exc}'
            ) from exc

    def _callable(self, operation: Operation) -> Any:
        """The channel callable for an operation, typed by the wire map."""
        binding = binding_for_operation(operation)
        method = f'/{GRPC_SERVICE}/{binding.grpc_method}'
        factory = (
            self._channel.unary_stream if binding.streaming
            else self._channel.unary_unary
        )
        return factory(
            method,
            request_serializer=_message_type(binding.grpc_request).SerializeToString,
            response_deserializer=_message_type(binding.grpc_response).FromString,
        )

    # -- the Dispatcher contract -------------------------------------------

    async def dispatch(
        self,
        operation: Operation,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> WireResponse:
        binding = binding_for_operation(operation)
        if binding.http_only:
            return await self._get_agent_card(headers)
        if binding.streaming:
            raise DispatchError(
                f'{operation.value} is a streaming operation; use stream()'
            )

        request = self._build_request(operation, params)
        call = self._callable(operation)
        try:
            response = await call(
                request,
                metadata=self._metadata(headers),
                timeout=self.timeout,
            )
        except AioRpcError as exc:
            return _response_from_rpc_error(exc)
        except grpc.RpcError as exc:  # pragma: no cover - sync-style failure
            raise DispatchError(f'gRPC call failed: {exc}') from exc

        return WireResponse(
            status=200,
            payload=_to_dict(response),
            headers={},
        )

    async def dispatch_raw(
        self,
        raw: RawBlock,
        headers: Mapping[str, str] | None = None,
    ) -> WireResponse:
        raise UnsupportedByBinding(
            'gRPC has no raw-request form; a test built from `raw` steps must '
            'declare `transport: [jsonrpc]` or `[rest]` (ACTS §4.4)'
        )

    async def stream_raw(
        self,
        raw: RawBlock,
        headers: Mapping[str, str] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        raise UnsupportedByBinding(
            'gRPC has no raw-request form, streaming or otherwise (ACTS §4.4)'
        )
        yield  # pragma: no cover - unreachable, but makes this a generator

    async def stream(
        self,
        operation: Operation,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if not binding_for_operation(operation).streaming:
            raise DispatchError(
                f'{operation.value} is not a streaming operation; use dispatch()'
            )

        request = self._build_request(operation, params)
        call = self._callable(operation)(
            request,
            metadata=self._metadata(headers),
            timeout=self.timeout,
        )
        index = 0
        try:
            async for message in call:
                yield StreamEvent(index=index, data=_to_dict(message))
                index += 1
        except AioRpcError as exc:
            # A stream that dies partway is a result, not a harness failure,
            # but there is no WireResponse to carry it here — the runner sees
            # the events yielded so far and this error.
            raise DispatchError(
                f'stream failed after {index} event(s): '
                f'{exc.code().name}: {exc.details()}'
            ) from exc

    # -- the agent card, over HTTP -----------------------------------------

    async def _get_agent_card(
        self, headers: Mapping[str, str] | None
    ) -> WireResponse:
        url = f'{self._agent_card_url}{WELL_KNOWN_AGENT_CARD_PATH}'
        try:
            response = await self._http.get(
                url, headers={**self._default_headers, **(headers or {})}
            )
        except httpx.HTTPError as exc:
            raise DispatchError(f'{type(exc).__name__}: {exc}') from exc

        text = response.text
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return WireResponse(
            status=response.status_code,
            payload=payload,
            error=(
                None if response.status_code < 400
                else WireError(message=f'HTTP {response.status_code}', raw=payload)
            ),
            headers=dict(response.headers),
            raw_body=text,
        )


def _response_from_rpc_error(exc: AioRpcError) -> WireResponse:
    """Turn a failed RPC into a :class:`WireResponse` carrying a WireError."""
    status_name = exc.code().name
    reason, details = _reason_from_trailers(exc)
    return WireResponse(
        status=http_status_for_grpc(status_name),
        payload=None,
        error=WireError(
            message=exc.details() or '',
            error_type=error_for_reason(reason) if reason else None,
            status=status_name,
            reason=reason,
            details=details,
            raw=exc,
        ),
        headers={
            key: value
            for key, value in (exc.trailing_metadata() or ())
            if isinstance(value, str)
        },
    )


def _reason_from_trailers(exc: AioRpcError) -> tuple[str | None, tuple[Any, ...]]:
    """Recover ``ErrorInfo.reason`` from ``grpc-status-details-bin``.

    The abstract error name cannot come from the gRPC status: six A2A errors
    share ``FAILED_PRECONDITION``. It comes from the ``ErrorInfo`` that A2A
    §11.6 requires, which on gRPC travels as a serialized ``google.rpc.Status``
    in the trailers. A SUT that omits it leaves the error unnamed, and that is
    reported rather than guessed at.
    """
    blob = None
    for key, value in exc.trailing_metadata() or ():
        if key == STATUS_DETAILS_KEY and isinstance(value, bytes):
            blob = value
            break
    if blob is None:
        return None, ()

    try:
        status = status_pb2.Status()
        status.ParseFromString(blob)
        collected: list[Any] = []
        reason: str | None = None
        for detail in status.details:
            if detail.Is(error_details_pb2.ErrorInfo.DESCRIPTOR):
                info = error_details_pb2.ErrorInfo()
                detail.Unpack(info)
                reason = reason or info.reason
                collected.append(MessageToDict(info))
            else:
                collected.append({'@type': detail.type_url})
        return reason or None, tuple(collected)
    except Exception:  # noqa: BLE001 - malformed trailers are the SUT's bug
        # A trailer we cannot decode is a finding about the SUT, not a reason
        # to abort the run; the error is still reported, just unnamed.
        return None, ()
