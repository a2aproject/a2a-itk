"""What every dispatcher returns, and the contract all three implement.

A dispatcher turns an abstract ACTS step into one call on one binding and
reports what came back in a shape the runner can assert against, without the
runner knowing which binding ran. Everything binding-specific — method names,
paths, status codes — comes from :mod:`test_suite.acts.wire_map`.

## Why the transports are driven at a low level

A conformance suite asserts on what an application-facing client is built to
hide: the HTTP status, the response headers, the exact ``error.code``, whether
the body was valid JSON at all. Several tests also send payloads a
well-behaved client would refuse to construct. So the HTTP bindings are driven
with ``httpx`` directly and gRPC with the generated stubs over ``grpc.aio``,
rather than through a typed client that parses responses and raises on error.

The generated protobuf message types are reused as-is — they are a compiled
artifact of the proto, not behaviour, and regenerating them here would only
duplicate them.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping

from test_suite.acts.schema import (
    ErrorType,
    Operation,
    RawBlock,
    TransportBinding,
)


class DispatchError(RuntimeError):
    """The call could not be made or its reply could not be read.

    Distinct from a :class:`WireError`, which *is* the reply: an agent
    answering ``TaskNotFoundError`` is a well-formed conformance result, while
    a connection refused or an unparseable body is a failure of the run. The
    runner reports the first as a test outcome and the second as an error.
    """


class MalformedResponse(DispatchError):
    """The SUT answered, but not in a shape this binding permits.

    Distinct from its parent for the same reason :class:`WireError` is
    distinct from :class:`DispatchError`: the exchange *happened*. A streaming
    call answered with ``Content-Type: application/json`` instead of
    ``text/event-stream`` is a conformance finding, and reporting it as a
    broken run would file our tooling's name against the SUT's defect.
    """


class UnsupportedByBinding(DispatchError):
    """This binding cannot express the requested call.

    Raised for ``dispatch_raw`` on gRPC — a "raw request" is an HTTP notion,
    and below protobuf there is no equivalent an ACTS test could describe.
    """


@dataclass(frozen=True, slots=True)
class WireError:
    """An error the SUT returned, normalized across bindings.

    ``error_type`` is the abstract name when the wire identified one — a
    JSON-RPC ``error.code`` or a ``google.rpc.ErrorInfo.reason``. It is
    ``None`` when the SUT returned an error this suite cannot name, which is
    itself a finding and must not be silently mapped to something plausible.
    """

    message: str
    error_type: ErrorType | None = None
    #: JSON-RPC ``error.code``; ``None`` on the other bindings.
    code: int | None = None
    #: gRPC status name, or the ``status`` field of a REST ``google.rpc.Status``.
    status: str | None = None
    #: ``google.rpc.ErrorInfo.reason``, when the response carried one.
    reason: str | None = None
    #: The ``data`` / ``details`` array as it arrived.
    details: tuple[Any, ...] = ()
    #: The error object exactly as parsed, for assertions this does not model.
    raw: Any = None


@dataclass(frozen=True, slots=True)
class WireResponse:
    """One non-streaming reply, normalized across bindings.

    ``status`` is the HTTP status. gRPC has none, so the gRPC dispatcher
    derives it from the gRPC status by the canonical transcoding — see
    :data:`test_suite.acts.wire_map.GRPC_STATUS_TO_HTTP`. Without that, the 86
    binding-agnostic corpus tests asserting ``status: 200`` could not run on
    gRPC at all.
    """

    status: int
    payload: Any = None
    error: WireError | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    #: Undecoded response body, for the tests that assert on the wire itself.
    #: ``None`` on gRPC, where there is no text body to show.
    raw_body: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the SUT answered without an error."""
        return self.error is None


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One event from a streaming call.

    ``raw`` is the SSE ``data:`` payload before parsing, kept because some
    streaming assertions are about framing rather than content. It is ``None``
    on gRPC, where events arrive as messages and were never text.
    """

    index: int
    data: Any
    raw: str | None = None
    #: SSE ``event:`` field when the server set one.
    event: str | None = None
    #: HTTP status of the response carrying the stream. Repeated on every
    #: event because one stream has one status, and a step asserting
    #: ``expect.status`` alongside ``expect_stream`` needs an *observed* value
    #: rather than one inferred from the stream having worked. ``None`` on
    #: gRPC, which has no HTTP status of its own.
    status: int | None = None


class Dispatcher(abc.ABC):
    """Binds abstract operations to one wire protocol.

    Implementations are async and own a connection, so use them as async
    context managers (or call :meth:`aclose`). One instance serves many steps;
    the runner's per-test isolation is about variable scope, not connections.
    """

    #: Which binding this dispatcher speaks. Used to select it for a test's
    #: ``transport`` restriction.
    binding: TransportBinding

    @abc.abstractmethod
    async def dispatch(
        self,
        operation: Operation,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> WireResponse:
        """Perform ``operation`` and return the reply.

        An error *from the SUT* is returned as ``response.error``, not raised.
        Only a failure to complete the exchange raises :class:`DispatchError`.
        """

    @abc.abstractmethod
    async def dispatch_raw(
        self,
        raw: RawBlock,
        headers: Mapping[str, str] | None = None,
    ) -> WireResponse:
        """Send a hand-built request verbatim.

        The body is sent as given — including when it is not valid JSON, which
        is the point of the ParseError tests. Raises
        :class:`UnsupportedByBinding` on gRPC.
        """

    @abc.abstractmethod
    def stream(
        self,
        operation: Operation,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Perform a streaming ``operation``, yielding events in order.

        Returns an async iterator rather than awaiting a list so that
        time-bounded assertions (``timeout_ms``) can act on events as they
        arrive instead of after the stream closes.
        """

    @abc.abstractmethod
    def stream_raw(
        self,
        raw: RawBlock,
        headers: Mapping[str, str] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream the response to a hand-built request.

        Events arrive **unwrapped**, matching :meth:`dispatch_raw`: a raw
        streaming test asserts on the envelope each event comes in. Raises
        :class:`UnsupportedByBinding` on gRPC.
        """

    async def aclose(self) -> None:
        """Release the underlying connection. Idempotent."""

    async def __aenter__(self) -> Dispatcher:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
