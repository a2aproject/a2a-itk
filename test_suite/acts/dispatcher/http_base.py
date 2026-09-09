"""Shared HTTP machinery.

JSON-RPC and REST differ in how a call is addressed and how an error is
spelled, and in nothing else: both are HTTP, both stream with Server-Sent
Events, both fetch the agent card from the same well-known path, and both send
raw steps verbatim. That common half lives here so the two adapters are left
stating only what actually distinguishes them.

The module-level helpers go one binding further. Fetching the agent card is
plain HTTP on *all three* bindings — see :func:`fetch_agent_card` — so the gRPC
adapter reaches in here too rather than keeping a second copy of the same GET.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Callable, Mapping

import httpx
from httpx_sse import SSEError, aconnect_sse

from test_suite.acts.dispatcher.base import (
    DispatchError,
    MalformedResponse,
    Dispatcher,
    StreamEvent,
    WireError,
    WireResponse,
)
from test_suite.acts.schema import Operation, RawBlock
from test_suite.acts.wire_map import (
    ERROR_INFO_TYPE,
    SSE_CONTENT_TYPE,
    WELL_KNOWN_AGENT_CARD_PATH,
    binding_for_operation,
)


#: Read timeout for a streaming call. Long, because a streaming test's own
#: ``timeout_ms`` is the assertion — the transport should not pre-empt it.
STREAM_READ_TIMEOUT = 300.0

#: Redirects are never followed. A conformance run asserts on the response the
#: SUT gave to the request as sent, and following a redirect replaces it with
#: the response to a *different* request: httpx rewrites POST to GET and drops
#: the body on a 301/302/303, so a SUT that redirects every call would sail
#: through the suite on the strength of replies it never made. A 3xx is a
#: result like any other and is handed to the assertions as one.
FOLLOW_REDIRECTS = False


def parse_json_body(response: httpx.Response) -> tuple[Any, str]:
    """Return ``(parsed_body_or_None, raw_text)``.

    A body that is not JSON is not an error here: the ParseError tests expect
    the SUT to answer a malformed request, and the answer itself might be
    malformed too. The raw text is always preserved so an assertion can speak
    about it.
    """
    text = response.text
    if not text.strip():
        return None, text
    try:
        return json.loads(text), text
    except ValueError:
        return None, text


def http_status_error(response: httpx.Response, parsed: Any) -> WireError | None:
    """Error reader for an endpoint that is plain HTTP and nothing more.

    Used for the agent card, which carries no binding envelope to read an
    error out of — the status is all there is to go on.
    """
    if response.status_code < 400:
        return None
    return WireError(message=f'HTTP {response.status_code}', raw=parsed)


async def fetch_agent_card(
    client: httpx.AsyncClient,
    url: str,
    headers: Mapping[str, str] | None,
    *,
    error_from: Callable[[httpx.Response, Any], WireError | None],
) -> WireResponse:
    """GET the unauthenticated agent card and normalize the reply.

    Plain HTTP on every binding, gRPC included: the card is what tells a
    client which bindings the agent speaks, so retrieving it cannot presuppose
    one. There is no ``GetAgentCard`` RPC.

    ``error_from`` is the caller's error reader, because the bindings do not
    agree on what a failed fetch looks like — the two HTTP adapters can read
    their own envelope out of the body, while gRPC has only the status.
    """
    try:
        response = await client.get(url, headers=dict(headers or {}))
    except httpx.HTTPError as exc:
        raise DispatchError(f'{type(exc).__name__}: {exc}') from exc

    parsed, text = parse_json_body(response)
    return WireResponse(
        status=response.status_code,
        payload=parsed,
        error=error_from(response, parsed),
        headers=dict(response.headers),
        raw_body=text,
    )


class HttpDispatcher(Dispatcher):
    """A dispatcher that speaks HTTP. Not usable directly.

    Owns an :class:`httpx.AsyncClient` unless one is injected, which is how
    the tests drive it against a :class:`httpx.MockTransport` without a
    server.
    """

    #: Media type this binding sends. Set by the subclass.
    content_type: str

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
        default_headers: Mapping[str, str] | None = None,
        agent_card_url: str | None = None,
    ) -> None:
        """
        Args:
          base_url: Where this binding's operations hang off.
          agent_card_url: Base URL for the well-known agent card. Defaults to
            ``base_url``, which is right when the binding is mounted at the
            root and wrong when it is not — an agent serving REST under
            ``/rest`` still publishes its card at the host root, because the
            card is what tells a client which bindings exist and so cannot
            live behind one of them.
        """
        self.base_url = base_url.rstrip('/')
        self.agent_card_url = (agent_card_url or base_url).rstrip('/')
        self._default_headers = dict(default_headers or {})
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, read=STREAM_READ_TIMEOUT),
            follow_redirects=FOLLOW_REDIRECTS,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- request plumbing --------------------------------------------------

    def _url(self, path: str) -> str:
        return f'{self.base_url}{path if path.startswith("/") else "/" + path}'

    def _headers(
        self,
        headers: Mapping[str, str] | None,
        *,
        content_type: str | None = None,
        accept: str | None = None,
        defaults: bool = True,
    ) -> dict[str, str]:
        """Merge defaults, per-binding media types, and the step's headers.

        The step wins: several tests set a deliberately wrong ``Content-Type``
        and assert the SUT rejects it, so a default must never override one
        that was passed in.

        ``defaults=False`` drops the dispatcher's own headers, for a raw step.
        A raw block is a literal request — the runner already declines to add
        ``A2A-Version`` to one (§12.4), because `VER-NEG-002` omits it on
        purpose and a helpful runner would repair the test away. A dispatcher
        default is the same hazard one layer down, and a sharper one once it
        carries credentials: `SEC-EXTCARD-001` proves an *unauthenticated*
        fetch is refused, so a default ``Authorization`` merged in behind its
        back would make it prove nothing and pass.
        """
        merged = dict(self._default_headers) if defaults else {}
        if content_type:
            merged['Content-Type'] = content_type
        if accept:
            merged['Accept'] = accept
        merged.update(headers or {})
        return merged

    async def _request(self, *args: Any, **kwargs: Any) -> httpx.Response:
        """Send, translating transport failure into :class:`DispatchError`.

        An HTTP error *status* is a result, not a failure, and is returned as
        a response. Only never reaching the SUT raises.
        """
        try:
            return await self._client.request(*args, **kwargs)
        except httpx.HTTPError as exc:
            raise DispatchError(f'{type(exc).__name__}: {exc}') from exc

    # -- pieces the subclass fills in --------------------------------------

    def _error_from(self, response: httpx.Response, parsed: Any) -> WireError | None:
        """Extract the binding's error representation, or ``None``."""
        raise NotImplementedError

    # -- shared operations --------------------------------------------------

    async def _get_agent_card(
        self, headers: Mapping[str, str] | None
    ) -> WireResponse:
        """Fetch the unauthenticated agent card.

        The binding's own ``_error_from`` reads a failure here, unlike on
        gRPC: an agent serving its card from the same app as its RPC endpoint
        will answer a 404 in that app's error dialect, and reading it costs
        nothing when it is absent.
        """
        return await fetch_agent_card(
            self._client,
            f'{self.agent_card_url}{WELL_KNOWN_AGENT_CARD_PATH}',
            self._headers(headers),
            error_from=self._error_from,
        )

    async def dispatch_raw(
        self,
        raw: RawBlock,
        headers: Mapping[str, str] | None = None,
    ) -> WireResponse:
        """Send a hand-built request exactly as written.

        ``payload`` here is the *whole* parsed body, not an unwrapped result:
        a raw test asserts on the envelope it expected to get back, down to
        ``error.code``.
        """
        merged = self._headers(headers, defaults=False)
        merged.update(raw.headers or {})

        content: str | None = None
        if raw.body_raw is not None:
            content = raw.body_raw
        elif raw.body is not None:
            content = json.dumps(raw.body)

        response = await self._request(
            raw.method.value,
            self._url(raw.path),
            headers=merged,
            content=content,
        )
        parsed, text = parse_json_body(response)
        return WireResponse(
            status=response.status_code,
            payload=parsed,
            error=self._error_from(response, parsed),
            headers=dict(response.headers),
            raw_body=text,
        )

    async def stream_raw(
        self,
        raw: RawBlock,
        headers: Mapping[str, str] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream the response to a hand-built request.

        Events are **not** unwrapped, for the same reason `dispatch_raw` does
        not unwrap a reply: a raw streaming test asserts on the transport
        envelope each event arrives in, and removing it would delete the thing
        under test.
        """
        merged = self._headers(headers, defaults=False)
        merged.update(raw.headers or {})

        content: str | None = None
        if raw.body_raw is not None:
            content = raw.body_raw
        elif raw.body is not None:
            content = json.dumps(raw.body)

        async for event in self._stream_sse(
            raw.method.value,
            raw.path,
            payload=None,
            headers=merged,
            content=content,
            unwrap=False,
            defaults=False,
        ):
            yield event

    async def _stream_sse(
        self,
        method: str,
        path: str,
        *,
        payload: Any,
        headers: Mapping[str, str] | None,
        content: str | None = None,
        unwrap: bool = True,
        defaults: bool = True,
    ) -> AsyncIterator[StreamEvent]:
        """Yield parsed SSE events from a streaming endpoint."""
        merged = self._headers(
            headers,
            content_type=self.content_type,
            accept=SSE_CONTENT_TYPE,
            defaults=defaults,
        )
        if content is None and payload is not None:
            content = json.dumps(payload)
        try:
            async with aconnect_sse(
                self._client,
                method,
                self._url(path),
                headers=merged,
                content=content,
            ) as source:
                index = 0
                async for sse in source.aiter_sse():
                    try:
                        data = json.loads(sse.data)
                    except ValueError:
                        data = None
                    yield StreamEvent(
                        index=index,
                        data=self._unwrap_stream_event(data) if unwrap else data,
                        raw=sse.data,
                        event=sse.event or None,
                        status=source.response.status_code,
                    )
                    index += 1
        except SSEError as exc:
            # httpx-sse files this under TransportError, but it means the SUT
            # replied with the wrong Content-Type — an answer, not a failure
            # to get one.
            raise MalformedResponse(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise DispatchError(f'{type(exc).__name__}: {exc}') from exc

    def _unwrap_stream_event(self, data: Any) -> Any:
        """Per-binding unwrapping of one event's payload. Identity by default."""
        return data

    # -- helpers for the subclasses ----------------------------------------

    @staticmethod
    def _error_info(details: Any) -> tuple[str | None, tuple[Any, ...]]:
        """Pull ``ErrorInfo.reason`` out of a ``details``/``data`` array.

        A2A mandates this object precisely because status codes are not
        injective (§11.6): ``TaskNotCancelableError`` and
        ``PushNotificationNotSupportedError`` are both HTTP 400, so only the
        reason distinguishes them.
        """
        if not isinstance(details, list):
            return None, ()
        for item in details:
            if isinstance(item, dict) and item.get('@type') == ERROR_INFO_TYPE:
                reason = item.get('reason')
                return (reason if isinstance(reason, str) else None), tuple(details)
        return None, tuple(details)

    @staticmethod
    def _streaming_binding(operation: Operation) -> None:
        """Reject a non-streaming operation passed to :meth:`stream`."""
        if not binding_for_operation(operation).streaming:
            raise DispatchError(
                f'{operation.value} is not a streaming operation; '
                f'use dispatch()'
            )
