"""A conforming A2A agent that runs in process, for exercising the ACTS runner.

Every other ACTS test scripts single replies. This agent keeps real state:
tasks that move through their states, streams that several subscribers follow,
push notifications, and the A2A 1.1 additions (generation, timeline, long
polling, client-written artifacts). A conformance run against it goes through
the real dispatcher and the real runner, so it shows that the stack can report
a pass, which a scripted reply cannot.

It is a test fixture, not a reference implementation. It was written from the
A2A specification by the same people who write the runner, so a test passing
against it shows that the runner can execute the test and tell a pass from a
failure. It does not show that the test reads the specification correctly.

Three things keep it honest as a fixture:

- **Its state lives in the specification's own protobuf messages**, and every
  reply is their ProtoJSON rendering. Field names, enum spellings, omitted
  defaults and 64-bit integers written as strings are therefore what a real
  agent puts on the wire, without this module restating them.
- **Its error codes are written out here** and not read from the runner's wire
  map. An agent that took them from the runner would agree with a wrong row.
- **It runs on virtual time** (:class:`VirtualTimeLoop`). A behavior that holds
  a task for seconds and a test that polls it both take no real time, and the
  order of events does not depend on how fast the machine is.

It is also an ITK agent. A first message that names a `tck-*` behavior is
played as ACTS defines it. A first message that carries an `itk.Instruction`
is carried out instead: the agent replies with text, calls other agents, or
does both in order.

Only the JSON-RPC binding is served, and the agent calls other agents over
JSON-RPC only.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import json
import re
import selectors
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError

from pyproto import a2a_pb2 as pb
from pyproto import instruction_pb2


_T = TypeVar('_T')


# ---------------------------------------------------------------------------
# Virtual time
# ---------------------------------------------------------------------------


class _NeverWaitingSelector(selectors.DefaultSelector):
    """A selector that reports how long it was asked to wait, and does not.

    The event loop calls ``select(timeout)`` when no callback is ready, with
    the time until its next timer. Nothing here does real I/O, so no event can
    arrive in that time and waiting it out would only be slow.
    """

    def __init__(self, advance: Callable[[float], None]) -> None:
        super().__init__()
        self._advance = advance

    def select(self, timeout: float | None = None) -> list[Any]:
        ready = super().select(0)
        if ready:
            return ready
        if timeout is None:
            # No callback is ready and no timer is pending, so nothing can
            # ever run again. On a real loop this is a test that hangs until
            # CI kills it; saying so is more useful.
            raise RuntimeError(
                'the event loop has nothing left to run and nothing scheduled: '
                'a coroutine is waiting for something that will never happen'
            )
        if timeout > 0:
            self._advance(timeout)
        return ready


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    """An event loop whose clock jumps to the next timer when it is idle.

    ``asyncio.sleep``, ``asyncio.wait_for`` and ``loop.time()`` all read this
    clock, so code under test needs no changes: it waits exactly as long as it
    asked to, in virtual seconds, and the wait costs nothing.

    Time moves only when no callback is ready. Everything that can run at one
    instant therefore finishes before the clock advances, which makes the
    order of events a property of the code and not of the scheduler.

    Not for code that does real I/O or waits on a thread: neither is given
    time to finish.
    """

    def __init__(self) -> None:
        self._virtual_now = 0.0
        super().__init__(_NeverWaitingSelector(self._advance))

    def _advance(self, seconds: float) -> None:
        self._virtual_now += seconds

    def time(self) -> float:
        return self._virtual_now


def run_virtual(main: Coroutine[Any, Any, _T]) -> _T:
    """Run ``main`` to completion on a :class:`VirtualTimeLoop`."""
    with asyncio.Runner(loop_factory=VirtualTimeLoop) as runner:
        return runner.run(main)


# ---------------------------------------------------------------------------
# Wire constants
# ---------------------------------------------------------------------------

#: Where the agent card is served, on the host root.
CARD_PATH = '/.well-known/agent-card.json'

#: Where the JSON-RPC binding is mounted. Deliberately not the host root, so a
#: run has to take the URL from the card to reach it.
JSONRPC_PATH = '/jsonrpc'

#: The bearer token the ACTS runner presents on the operations it builds.
VALID_TOKEN = 'itk-valid-token'

#: A token that authenticates and does not authorize.
INSUFFICIENT_TOKEN = 'itk-insufficient-token'

#: The media type of the message part that carries an ITK instruction.
INSTRUCTION_MEDIA_TYPE = 'application/x-protobuf'

#: What an agent that holds its task open ends its reply with. A caller that
#: subscribes to the task again reads up to it and then has the whole reply.
TASK_FINISHED = 'task-finished'

#: Media types a message part may declare.
INPUT_MODES = ('text/plain', 'application/json', INSTRUCTION_MEDIA_TYPE)

VERSION_HEADER = 'A2A-Version'

#: What an absent version header means.
UNVERSIONED = '0.3'

ERROR_INFO_TYPE = 'type.googleapis.com/google.rpc.ErrorInfo'
ERROR_DOMAIN = 'a2a-protocol.org'

TERMINAL = frozenset({
    pb.TASK_STATE_COMPLETED,
    pb.TASK_STATE_FAILED,
    pb.TASK_STATE_CANCELED,
    pb.TASK_STATE_REJECTED,
})

INTERRUPTED = frozenset({
    pb.TASK_STATE_INPUT_REQUIRED,
    pb.TASK_STATE_AUTH_REQUIRED,
})

#: The first A2A version with generation, timeline and message updates.
V1_1 = (1, 1)


@dataclass(frozen=True)
class ErrorKind:
    """One error as JSON-RPC spells it: a code, and a reason for `ErrorInfo`."""

    code: int
    reason: str | None = None


TASK_NOT_FOUND = ErrorKind(-32001, 'TASK_NOT_FOUND')
TASK_NOT_CANCELABLE = ErrorKind(-32002, 'TASK_NOT_CANCELABLE')
PUSH_NOTIFICATION_NOT_SUPPORTED = ErrorKind(-32003, 'PUSH_NOTIFICATION_NOT_SUPPORTED')
UNSUPPORTED_OPERATION = ErrorKind(-32004, 'UNSUPPORTED_OPERATION')
CONTENT_TYPE_NOT_SUPPORTED = ErrorKind(-32005, 'CONTENT_TYPE_NOT_SUPPORTED')
VERSION_NOT_SUPPORTED = ErrorKind(-32009, 'VERSION_NOT_SUPPORTED')
TASK_GENERATION_MISMATCH = ErrorKind(-32010, 'TASK_GENERATION_MISMATCH')
PARSE_ERROR = ErrorKind(-32700)
INVALID_REQUEST = ErrorKind(-32600)
METHOD_NOT_FOUND = ErrorKind(-32601)
INVALID_PARAMS = ErrorKind(-32602)


class InstructionError(Exception):
    """An ITK instruction that cannot be carried out. It fails the task."""


class AgentError(Exception):
    """The agent refuses a request with a protocol error."""

    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class _Subscriber:
    """One open stream on a task."""

    version: tuple[int, int]
    #: The stream is the downlink of a live exchange. A request the agent
    #: rejects on that exchange is reported here, and on no other stream.
    live: bool = False
    #: Events to send, then ``None`` when the stream ends.
    queue: asyncio.Queue[pb.StreamResponse | None] = field(
        default_factory=asyncio.Queue
    )


@dataclass(eq=False)
class _Record:
    """A task and everything the agent keeps about it beside the task itself."""

    task: pb.Task
    behavior: str
    argument: str
    #: The A2A version of the request that created the task.
    version: tuple[int, int]
    #: Every mutation as the event that reported it, with its generation.
    #: What a subscriber resuming from a generation is replayed from.
    log: list[tuple[int, pb.StreamResponse]] = field(default_factory=list)
    subscribers: list[_Subscriber] = field(default_factory=list)
    push_configs: dict[str, pb.TaskPushNotificationConfig] = field(
        default_factory=dict
    )
    #: Requests the behavior has not read yet.
    inbox: collections.deque[pb.SendMessageRequest] = field(
        default_factory=collections.deque
    )
    #: The behavior is waiting for client input and has none to read.
    idle: bool = False
    worker: asyncio.Task[None] | None = None
    #: Set, and replaced, on every change to this record.
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    #: Position in the order of last updates, for listing newest first.
    touched: int = 0


class _Stream:
    """The events of one streaming call, in order.

    Not an async generator on purpose: a client may hang up before it reads
    the first event, and closing a generator that never started runs none of
    its cleanup, so the agent would go on following the task for it.
    """

    def __init__(
        self,
        first: list[pb.StreamResponse],
        subscriber: _Subscriber | None,
        hang_up: Callable[[], None],
    ) -> None:
        self._first = collections.deque(first)
        self._subscriber = subscriber
        self._hang_up = hang_up

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> pb.StreamResponse:
        if self._first:
            return self._first.popleft()
        if self._subscriber is None:
            raise StopAsyncIteration
        event = await self._subscriber.queue.get()
        if event is None:
            self.close()
            raise StopAsyncIteration
        return event

    def close(self) -> None:
        """Stop following the task. Safe to call more than once."""
        if self._subscriber is not None:
            self._subscriber = None
            self._hang_up()


class _Sse(httpx.AsyncByteStream):
    """A stream of events as a server-sent-events body for JSON-RPC."""

    def __init__(self, stream: _Stream, call_id: Any) -> None:
        self._stream = stream
        self._call_id = call_id

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for event in self._stream:
            envelope = {
                'jsonrpc': '2.0',
                'id': self._call_id,
                'result': MessageToDict(event),
            }
            yield f'data: {json.dumps(envelope)}\n\n'.encode()

    async def aclose(self) -> None:
        # The client went away, or read the stream to its end.
        self._stream.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: The member of a `StreamRequest` that carries a message, by the name the
#: generated stub gives it. Found by its type: A2A renamed the member after
#: the 1.1 release candidate.
_LIVE_SEND = next(
    member.name
    for member in pb.StreamRequest.DESCRIPTOR.oneofs_by_name['payload'].fields
    if member.message_type.name == 'SendMessageRequest'
)

#: The code a `StreamError` carries for each error a send can be refused
#: with: the name of the status the same refusal has on a unary call.
_STREAM_ERROR_CODES = {
    -32001: 'NOT_FOUND',
    -32004: 'FAILED_PRECONDITION',
    -32010: 'ABORTED',
}

#: A behavior name: `tck-` and hyphen-joined lowercase words. Greedy, so the
#: longest name wins without an ordered table.
_BEHAVIOR_NAME = re.compile(r'^(tck-[a-z0-9]+(?:-[a-z0-9]+)*)\s*(\S*)')


def _text_of(message: pb.Message) -> str:
    """The first text part of a message, or ``''``."""
    for part in message.parts:
        if part.WhichOneof('content') == 'text':
            return part.text
    return ''


def _behavior_in(text: str) -> tuple[str, str]:
    """The behavior a first message names, and the word that follows it."""
    match = _BEHAVIOR_NAME.match(text.strip())
    return (match.group(1), match.group(2)) if match else ('', '')


def _text_part(text: str) -> pb.Part:
    return pb.Part(text=text)


def _instruction_in(message: pb.Message) -> bytes | None:
    """The serialized ITK instruction a message carries, if it carries one."""
    for part in message.parts:
        if part.media_type == INSTRUCTION_MEDIA_TYPE:
            return part.raw
    return None


def _holds(instruction: instruction_pb2.Instruction) -> bool:
    """Does the instruction ask the agent to hold its own task open?

    Only the replies the agent itself gives count, not the ones in an
    instruction it passes on.
    """
    step = instruction.WhichOneof('step')
    if step == 'return_response':
        return instruction.return_response.hold_task
    if step == 'steps':
        return any(_holds(child) for child in instruction.steps.instructions)
    return False


def _agent_texts(reply: dict[str, Any]) -> list[str]:
    """The text an agent says in one response or stream event.

    A message the agent answers with, or the message of a task's status. A
    client's message reported back on a stream is not the agent's text.
    """
    if 'message' in reply:
        message = reply['message']
    else:
        update = reply.get('task') or reply.get('statusUpdate') or {}
        message = update.get('status', {}).get('message', {})
    return [part['text'] for part in message.get('parts', []) if 'text' in part]


def _envelope(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """A JSON-RPC call this agent makes."""
    return {'jsonrpc': '2.0', 'id': str(uuid.uuid4()), 'method': method, 'params': params}


def _result(method: str, body: dict[str, Any]) -> dict[str, Any]:
    """The result of a JSON-RPC call this agent made."""
    if 'error' in body:
        raise InstructionError(f'{method} was refused: {body["error"].get("message")}')
    return body['result']


def _data_part(data: Any) -> pb.Part:
    part = pb.Part()
    ParseDict(data, part.data)
    return part


def _version(text: str) -> tuple[int, int] | None:
    match = re.fullmatch(r'(\d+)\.(\d+)', text or '')
    return (int(match[1]), int(match[2])) if match else None


def _trim(entries: Any, limit: int | None) -> None:
    """Keep the ``limit`` most recent entries; ``None`` keeps all of them."""
    if limit is not None:
        del entries[: max(0, len(entries) - limit)]


def _optional(message: Any, name: str) -> Any | None:
    """A field's value if the request set it, else ``None``."""
    return getattr(message, name) if message.HasField(name) else None


def _parse(params: Any, message_type: type[_T]) -> _T:
    """Read JSON-RPC params into a request message.

    Unknown members are ignored: a client of a later minor version may send
    fields this agent has not heard of.
    """
    if not isinstance(params, dict):
        raise AgentError(INVALID_PARAMS, 'Invalid parameters: params must be an object')
    try:
        return ParseDict(params, message_type(), ignore_unknown_fields=True)
    except ParseError as exc:
        raise AgentError(INVALID_PARAMS, f'Invalid parameters: {exc}') from exc


def _json(status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status, json=body, headers={'Content-Type': 'application/json', **(headers or {})}
    )


def _http_error(
    status: int, name: str, message: str, headers: dict[str, str] | None = None
) -> httpx.Response:
    """An error that is about the HTTP request and not a JSON-RPC call."""
    return _json(
        status, {'error': {'code': status, 'status': name, 'message': message}}, headers
    )


def _rpc_error(call_id: Any, kind: ErrorKind, message: str) -> httpx.Response:
    """A JSON-RPC error response. It rides HTTP 200 like any other reply."""
    error: dict[str, Any] = {'code': kind.code, 'message': message}
    if kind.reason is not None:
        error['data'] = [
            {'@type': ERROR_INFO_TYPE, 'reason': kind.reason, 'domain': ERROR_DOMAIN}
        ]
    return _json(200, {'jsonrpc': '2.0', 'id': call_id, 'error': error})


#: What each operation's response parses into, for `tck-client-parse`.
_CLIENT_RESPONSES: dict[str, type] = {
    'send_message': pb.SendMessageResponse,
    'get_task': pb.Task,
    'get_agent_card': pb.AgentCard,
    'get_extended_agent_card': pb.AgentCard,
}


def _client_parse(operation: str, payload: Any) -> Any:
    """Read a canned response the way a client of this agent would.

    A JSON-RPC envelope is unwrapped first; an agent card is fetched over
    plain HTTP and arrives bare. The body is then parsed into the response
    message for the operation, ignoring members it does not know, and rendered
    back. What a client could not parse raises.
    """
    if isinstance(payload, dict) and payload.get('jsonrpc') == '2.0':
        if 'error' in payload:
            return {'error': payload['error']}
        payload = payload.get('result')
    message_type = _CLIENT_RESPONSES.get(operation)
    if message_type is None:
        raise ValueError(f'no client call is known for {operation!r}')
    return MessageToDict(ParseDict(payload, message_type(), ignore_unknown_fields=True))


#: The operations a client reads as a stream of events, for `tck-client-parse`.
_CLIENT_STREAMS = frozenset({
    'send_streaming_message', 'subscribe_to_task', 'send_live_message',
})


def _client_read_stream(operation: str, wire_stream: Any) -> Any:
    """Read a canned stream the way a client of this agent would.

    Each event is unwrapped from its JSON-RPC envelope and parsed as a stream
    response. An event of a kind the client does not know is skipped, and the
    client reads on: a later minor version may add kinds. A JSON-RPC error
    fails the call, and is reported as it is for a single response.
    """
    if operation not in _CLIENT_STREAMS:
        raise ValueError(f'{operation!r} is not answered with a stream')
    events: list[Any] = []
    for item in wire_stream:
        if isinstance(item, dict) and item.get('jsonrpc') == '2.0':
            if 'error' in item:
                return {'error': item['error']}
            item = item.get('result')
        event = ParseDict(item, pb.StreamResponse(), ignore_unknown_fields=True)
        if event.WhichOneof('payload') is not None:
            events.append(MessageToDict(event))
    return {'events': events}


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------

PushSender = Callable[[str, dict[str, str], dict[str, Any]], Awaitable[Any]]


class MockAgent:
    """An A2A agent serving JSON-RPC, for one event loop.

    Use :meth:`handle` as an ``httpx.MockTransport`` handler.

    Args:
        base_url: The host root the agent pretends to be served from. The card
            is at ``{base_url}/.well-known/agent-card.json`` and JSON-RPC at
            ``{base_url}/jsonrpc``.
        versions: The A2A versions the agent serves, as ``Major.Minor``. One
            interface is listed per version, all at the same URL.
        reduced_capabilities: Advertise no optional capability and refuse the
            operations they gate. What `ITK_ACTS_REDUCED_CAPABILITIES` asks of
            a real agent.
        require_auth: Declare a security requirement on the card and enforce it
            on every operation. What `ITK_ACTS_AUTH` asks of a real agent.
        push_sender: Delivers one push notification: ``(url, headers, body)``.
            Without one the agent does not advertise push notifications.
        client: What the agent calls other agents with when an instruction
            tells it to. Without one such a call fails.
    """

    #: Seconds a request takes to arrive. On the virtual clock this is what
    #: lets background work that is ready to run finish before the next
    #: request is handled, as it would between two requests over a network.
    latency_s = 0.001

    #: How long `tck-long-running` works before it completes.
    long_running_s = 1.0

    #: How long a `GetTask` naming the task's current generation is held
    #: before it is answered with the unchanged task.
    long_poll_timeout_s = 30.0

    def __init__(
        self,
        *,
        base_url: str = 'http://sut.test',
        versions: tuple[str, ...] = ('1.0', '1.1'),
        reduced_capabilities: bool = False,
        require_auth: bool = False,
        push_sender: PushSender | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip('/')
        self.versions = tuple(sorted({_version(v) for v in versions}, reverse=True))
        self.reduced_capabilities = reduced_capabilities
        self.require_auth = require_auth
        self._push_sender = push_sender
        self._client = client

        self._tasks: dict[str, _Record] = {}
        self._sequence = 0
        self._outbox: asyncio.Queue[tuple[str, dict[str, str], dict[str, Any]]] | None = None
        self._postman: asyncio.Task[None] | None = None

        #: Behavior name to the coroutine that plays it. An instance attribute
        #: so a test can swap one out to build a deliberately broken agent.
        self.behaviors: dict[str, Callable[[_Record, pb.SendMessageRequest], Awaitable[None]]] = {
            'tck-complete-task': self._complete,
            'tck-task-failure': self._fail,
            'tck-auth-required': self._auth_required,
            'tck-multi-turn': self._multi_turn,
            'tck-cancel': self._wait_for_cancel,
            'tck-long-running': self._long_running,
            'tck-artifact-text': self._artifact,
            'tck-artifact-data': self._artifact,
            'tck-artifact-file': self._artifact,
            'tck-artifact-file-url': self._artifact,
            'tck-stream-basic': self._stream_basic,
            'tck-stream-chunked': self._stream_chunked,
            'tck-client-parse': self._client_parse,
            # A2A 1.1 only.
            'tck-hold-working': self._hold_working,
            'tck-progress-update': self._progress_update,
            'tck-stream-input-required': self._stream_input_required,
            'tck-user-artifact-echo': self._user_artifact_echo,
            'tck-live-echo': self._live_echo,
            'tck-live-reject': self._live_echo,
        }

    # -- what the agent declares --------------------------------------------

    @property
    def jsonrpc_url(self) -> str:
        return f'{self.base_url}{JSONRPC_PATH}'

    @property
    def serves_1_1(self) -> bool:
        return V1_1 in self.versions

    def declared_behaviors(self) -> list[str]:
        """The `tck-*` prefixes this agent implements.

        `tck-message-response` is answered before a task exists, so it has no
        entry in :attr:`behaviors`.
        """
        return sorted([*self.behaviors, 'tck-message-response'])

    def behaviors_file(self) -> str:
        """The agent's behavior contract, as the file an SDK would ship."""
        lines = ['acts_version: "1.0"', 'behaviors:']
        for prefix in self.declared_behaviors():
            lines.append(f'  - prefix: "{prefix}"')
            lines.append(f'    description: "{prefix} as the ACTS specification defines it"')
        return '\n'.join(lines) + '\n'

    def card(self) -> dict[str, Any]:
        """The public agent card."""
        optional = not self.reduced_capabilities
        card: dict[str, Any] = {
            'name': 'ACTS mock agent',
            'description': (
                'An in-process agent that follows the A2A specification, for '
                'testing the ACTS runner and the traversal harness.'
            ),
            'version': '1.0.0',
            'supportedInterfaces': [
                {
                    'url': self.jsonrpc_url,
                    'protocolBinding': 'JSONRPC',
                    'protocolVersion': f'{major}.{minor}',
                }
                for major, minor in self.versions
            ],
            'capabilities': {
                'streaming': optional,
                'pushNotifications': self._pushes,
                'extendedAgentCard': optional,
            },
            'defaultInputModes': list(INPUT_MODES),
            'defaultOutputModes': ['text/plain'],
            'skills': [
                {
                    'id': 'acts-behaviors',
                    'name': 'ACTS behaviors',
                    'description': 'Plays the behavior the first message of a task names.',
                    'tags': ['conformance'],
                }
            ],
        }
        if self.serves_1_1:
            card['capabilities']['userArtifacts'] = optional
        if optional or self.require_auth:
            # The scheme the extended card is guarded with, required or not.
            card['securitySchemes'] = {
                'bearerAuth': {'httpAuthSecurityScheme': {'scheme': 'Bearer'}}
            }
        if self.require_auth:
            card['securityRequirements'] = [{'schemes': {'bearerAuth': {'list': []}}}]
        return card

    def extended_card(self) -> dict[str, Any]:
        """The card an authenticated client is given: one more skill."""
        card = self.card()
        card['skills'] = [
            *card['skills'],
            {
                'id': 'acts-extended',
                'name': 'Extended skill',
                'description': 'Listed only on the extended agent card.',
                'tags': ['conformance'],
            },
        ]
        return card

    # -- HTTP -----------------------------------------------------------------

    async def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one HTTP request."""
        written = request.extensions.get('trace')
        if written is not None:
            # A caller that asked to follow the request is told it has been
            # written out, as the HTTP client does on a real connection. The
            # name is the client library's.
            await written('http11.send_request_body.complete', {})
        await asyncio.sleep(self.latency_s)
        path = request.url.path

        if path == CARD_PATH:
            if request.method != 'GET':
                return _http_error(405, 'METHOD_NOT_ALLOWED', 'the agent card is read with GET')
            return self._card_response(self.card())

        if path.rstrip('/') != JSONRPC_PATH:
            return _http_error(404, 'NOT_FOUND', f'nothing is served at {path}')

        if self.require_auth:
            refusal = self._credential_refusal(request)
            if refusal is not None:
                return refusal

        if request.method == 'GET' and self.serves_1_1:
            # Tentative: an agent card served at the interface URL as well.
            return self._card_response(self.card())
        if request.method != 'POST':
            return _http_error(405, 'METHOD_NOT_ALLOWED', 'JSON-RPC calls are sent with POST')
        return await self._call(request)

    def _card_response(self, card: dict[str, Any]) -> httpx.Response:
        body = json.dumps(card, sort_keys=True).encode()
        return _json(
            200,
            card,
            {
                'Cache-Control': 'public, max-age=300',
                'ETag': f'"{hashlib.sha256(body).hexdigest()[:16]}"',
            },
        )

    def _credential_refusal(self, request: httpx.Request) -> httpx.Response | None:
        """Why the request's credential is not accepted, or ``None`` if it is."""
        presented = request.headers.get('Authorization', '')
        if presented == f'Bearer {VALID_TOKEN}':
            return None
        if presented == f'Bearer {INSUFFICIENT_TOKEN}':
            return _http_error(403, 'PERMISSION_DENIED', 'The token lacks the required scope.')
        return _http_error(
            401,
            'UNAUTHENTICATED',
            'A bearer token is required.',
            {'WWW-Authenticate': 'Bearer realm="a2a"'},
        )

    #: JSON-RPC method to the name of the method that serves it. Names, so
    #: that a test replacing one on an instance is what gets called.
    _METHODS = {
        'SendMessage': '_rpc_send_message',
        'SendStreamingMessage': '_rpc_send_streaming_message',
        'GetTask': '_rpc_get_task',
        'ListTasks': '_rpc_list_tasks',
        'CancelTask': '_rpc_cancel_task',
        'SubscribeToTask': '_rpc_subscribe_to_task',
        'SendLiveMessage': '_rpc_send_live_message',
        'CreateTaskPushNotificationConfig': '_rpc_create_push_config',
        'GetTaskPushNotificationConfig': '_rpc_get_push_config',
        'ListTaskPushNotificationConfigs': '_rpc_list_push_configs',
        'DeleteTaskPushNotificationConfig': '_rpc_delete_push_config',
        'GetExtendedAgentCard': '_rpc_get_extended_card',
    }

    async def _call(self, request: httpx.Request) -> httpx.Response:
        """Serve one JSON-RPC call."""
        try:
            envelope = json.loads(request.content)
        except ValueError:
            return _rpc_error(None, PARSE_ERROR, 'Invalid JSON payload')

        call_id = envelope.get('id') if isinstance(envelope, dict) else None
        if (
            not isinstance(envelope, dict)
            or envelope.get('jsonrpc') != '2.0'
            or not isinstance(envelope.get('method'), str)
        ):
            return _rpc_error(
                call_id,
                INVALID_REQUEST,
                'Request payload validation error: a call needs "jsonrpc": "2.0" and a method',
            )
        method = envelope['method']

        try:
            version = self._negotiate(request.headers.get(VERSION_HEADER))
            name = self._METHODS.get(method)
            if name is None:
                raise AgentError(METHOD_NOT_FOUND, f'Method not found: {method}')
            if method == 'GetExtendedAgentCard' and not self.reduced_capabilities:
                # Guarded whether or not the agent requires a credential
                # elsewhere: the extended card exists to be shown only to an
                # authenticated client.
                refusal = self._credential_refusal(request)
                if refusal is not None:
                    return refusal
            result = await getattr(self, name)(envelope.get('params') or {}, version)
        except AgentError as exc:
            return _rpc_error(call_id, exc.kind, str(exc))

        if isinstance(result, _Stream):
            return httpx.Response(
                200,
                headers={'Content-Type': 'text/event-stream'},
                stream=_Sse(result, call_id),
            )
        return _json(200, {'jsonrpc': '2.0', 'id': call_id, 'result': result})

    def _negotiate(self, header: str | None) -> tuple[int, int]:
        """The version a request asks for, if this agent serves it."""
        requested = header if header else UNVERSIONED
        version = _version(requested)
        if version not in self.versions:
            served = ', '.join(f'{major}.{minor}' for major, minor in self.versions)
            raise AgentError(
                VERSION_NOT_SUPPORTED,
                f'A2A version {requested} is not supported; this interface serves {served}',
            )
        return version

    async def _rpc_send_message(self, params: Any, version: tuple[int, int]) -> Any:
        request = _parse(params, pb.SendMessageRequest)
        return MessageToDict(await self.send_message(request, version))

    async def _rpc_send_streaming_message(self, params: Any, version: tuple[int, int]) -> Any:
        return self.send_streaming_message(_parse(params, pb.SendMessageRequest), version)

    async def _rpc_get_task(self, params: Any, version: tuple[int, int]) -> Any:
        return MessageToDict(await self.get_task(_parse(params, pb.GetTaskRequest)))

    async def _rpc_list_tasks(self, params: Any, version: tuple[int, int]) -> Any:
        return self.list_tasks(_parse(params, pb.ListTasksRequest))

    async def _rpc_cancel_task(self, params: Any, version: tuple[int, int]) -> Any:
        return MessageToDict(self.cancel_task(_parse(params, pb.CancelTaskRequest)))

    async def _rpc_subscribe_to_task(self, params: Any, version: tuple[int, int]) -> Any:
        return self.subscribe_to_task(_parse(params, pb.SubscribeToTaskRequest), version)

    async def _rpc_send_live_message(self, params: Any, version: tuple[int, int]) -> Any:
        answer = self.send_live_message(_parse(params, pb.StreamRequest), version)
        if isinstance(answer, pb.Task):
            return MessageToDict(answer)
        return answer

    async def _rpc_create_push_config(self, params: Any, version: tuple[int, int]) -> Any:
        config = _parse(params, pb.TaskPushNotificationConfig)
        return MessageToDict(self.create_push_config(config, version))

    async def _rpc_get_push_config(self, params: Any, version: tuple[int, int]) -> Any:
        request = _parse(params, pb.GetTaskPushNotificationConfigRequest)
        return MessageToDict(self.get_push_config(request))

    async def _rpc_list_push_configs(self, params: Any, version: tuple[int, int]) -> Any:
        request = _parse(params, pb.ListTaskPushNotificationConfigsRequest)
        return self.list_push_configs(request)

    async def _rpc_delete_push_config(self, params: Any, version: tuple[int, int]) -> Any:
        request = _parse(params, pb.DeleteTaskPushNotificationConfigRequest)
        self.delete_push_config(request)
        return {}

    async def _rpc_get_extended_card(self, params: Any, version: tuple[int, int]) -> Any:
        if self.reduced_capabilities:
            raise AgentError(
                UNSUPPORTED_OPERATION, 'this agent has no extended agent card'
            )
        return self.extended_card()

    # -- operations -----------------------------------------------------------

    async def send_message(
        self, request: pb.SendMessageRequest, version: tuple[int, int]
    ) -> pb.SendMessageResponse:
        accepted = self._accept(request, version)
        if isinstance(accepted, pb.Message):
            return pb.SendMessageResponse(message=accepted)

        self._hand_over(accepted, request)
        if not request.configuration.return_immediately:
            await self._until(accepted, lambda: self._at_rest(accepted))
        return pb.SendMessageResponse(
            task=self._view(
                accepted,
                history_length=_optional(request.configuration, 'history_length'),
                timeline_length=_optional(request.configuration, 'timeline_length'),
            )
        )

    def send_streaming_message(
        self, request: pb.SendMessageRequest, version: tuple[int, int]
    ) -> _Stream:
        if self.reduced_capabilities:
            raise AgentError(UNSUPPORTED_OPERATION, 'this agent does not support streaming')
        accepted = self._accept(request, version)
        if isinstance(accepted, pb.Message):
            return _Stream([pb.StreamResponse(message=accepted)], None, lambda: None)

        # Subscribed before the behavior is given the request, so the stream
        # opens with the task as it was accepted and misses nothing after.
        stream = self._follow(accepted, version, [self._snapshot(accepted)])
        self._hand_over(accepted, request)
        return stream

    async def get_task(self, request: pb.GetTaskRequest) -> pb.Task:
        record = self._find(request.id)
        seen = _optional(request, 'current_generation')
        if seen is not None and record.task.generation <= seen:
            # A long poll: held until the task moves past the generation the
            # client has, or until this agent's own patience runs out.
            try:
                await asyncio.wait_for(
                    self._until(record, lambda: record.task.generation > seen),
                    self.long_poll_timeout_s,
                )
            except (asyncio.TimeoutError, TimeoutError):
                pass
        return self._view(
            record,
            history_length=_optional(request, 'history_length'),
            timeline_length=_optional(request, 'timeline_length'),
        )

    def list_tasks(self, request: pb.ListTasksRequest) -> dict[str, Any]:
        matching = [
            record
            for record in self._tasks.values()
            if (not request.context_id or record.task.context_id == request.context_id)
            and (not request.status or record.task.status.state == request.status)
        ]
        # Most recently updated first.
        matching.sort(key=lambda record: record.touched, reverse=True)

        size = _optional(request, 'page_size') or 50
        try:
            start = int(request.page_token or 0)
        except ValueError:
            raise AgentError(INVALID_PARAMS, 'Invalid parameters: unknown pageToken') from None
        page = matching[start : start + size]
        more = start + size < len(matching)

        tasks = []
        for record in page:
            view = self._view(
                record,
                history_length=_optional(request, 'history_length'),
                timeline_length=_optional(request, 'timeline_length'),
            )
            if not request.include_artifacts:
                # Left out altogether, not sent empty.
                del view.artifacts[:]
            tasks.append(MessageToDict(view))
        # Written out by hand: every member of this response is always present,
        # the page token as an empty string on the last page.
        return {
            'tasks': tasks,
            'nextPageToken': str(start + size) if more else '',
            'pageSize': len(tasks),
            'totalSize': len(matching),
        }

    def cancel_task(self, request: pb.CancelTaskRequest) -> pb.Task:
        record = self._find(request.id)
        if record.task.status.state in TERMINAL:
            raise AgentError(
                TASK_NOT_CANCELABLE,
                f'task {request.id} is already in a terminal state and cannot be canceled',
            )
        self._set_status(record, pb.TASK_STATE_CANCELED, 'canceled at the client\'s request')
        if record.worker is not None:
            record.worker.cancel()
        return self._view(record)

    def send_live_message(
        self, request: pb.StreamRequest, version: tuple[int, int]
    ) -> _Stream | pb.Task:
        """One call of the two-way exchange A2A 1.1 adds.

        Over HTTP the exchange is several calls to this operation. The call
        that opens it is answered with the stream of the task's events, the
        downlink, which stays open. Each later call carries one more message
        and is answered with the task as it is, and with no events.

        A2A does not say yet how a server tells the two kinds of call apart.
        This agent goes by the task: a message that names a task with a live
        exchange open is a later request of that exchange. A message for any
        other task, or for none, opens an exchange.
        """
        if version < V1_1:
            raise AgentError(
                METHOD_NOT_FOUND, 'Method not found: SendLiveMessage is not in A2A 1.0'
            )
        if self.reduced_capabilities:
            raise AgentError(UNSUPPORTED_OPERATION, 'this agent does not support streaming')

        kind = request.WhichOneof('payload')
        if kind == 'subscribe':
            return self.subscribe_to_task(request.subscribe, version, live=True)
        if kind != _LIVE_SEND:
            raise AgentError(
                INVALID_PARAMS,
                'Invalid parameters: a live request carries a message or a subscription',
            )

        send = getattr(request, _LIVE_SEND)
        exchange = self._tasks.get(send.message.task_id) if send.HasField('message') else None
        if exchange is not None and any(s.live for s in exchange.subscribers):
            return self._live_uplink(exchange, send, version)

        accepted = self._accept(send, version)
        if isinstance(accepted, pb.Message):
            return _Stream([pb.StreamResponse(message=accepted)], None, lambda: None)
        # The snapshot that opens the downlink already has the message in it.
        stream = self._follow(accepted, version, [self._snapshot(accepted)], live=True)
        self._hand_over(accepted, send)
        return stream

    def _live_uplink(
        self, record: _Record, send: pb.SendMessageRequest, version: tuple[int, int]
    ) -> pb.Task:
        """A later request of an open live exchange.

        Answered with the task as it is now. A request the agent does not
        take is reported as a `StreamError` on the downlink of the exchange,
        and the call is still answered with the task: once an exchange is
        open, a rejected request does not fail a call.
        """
        declined = record.behavior == 'tck-live-reject' and _text_of(
            send.message
        ).strip().startswith('reject')
        try:
            if declined:
                raise AgentError(INVALID_PARAMS, 'the agent declined this message, as asked')
            self._hand_over(self._accept(send, version), send)
        except AgentError as exc:
            rejection = pb.StreamResponse()
            rejection.error.task_id = record.task.id
            rejection.error.code = (
                'UNAVAILABLE' if declined
                else _STREAM_ERROR_CODES.get(exc.kind.code, 'INVALID_ARGUMENT')
            )
            rejection.error.message = str(exc)
            rejection.error.reference_id = send.message.message_id
            # Not a change to the task: no generation, no entry in the log,
            # and nothing for another subscriber or a webhook.
            for subscriber in record.subscribers:
                if subscriber.live:
                    subscriber.queue.put_nowait(rejection)
        return self._view(record)

    def subscribe_to_task(
        self,
        request: pb.SubscribeToTaskRequest,
        version: tuple[int, int],
        *,
        live: bool = False,
    ) -> _Stream:
        if self.reduced_capabilities:
            raise AgentError(UNSUPPORTED_OPERATION, 'this agent does not support streaming')
        record = self._find(request.id)
        state = record.task.status.state
        if state in TERMINAL:
            raise AgentError(
                UNSUPPORTED_OPERATION,
                f'task {request.id} is in a terminal state; there is nothing to subscribe to',
            )

        resume_from = _optional(request, 'last_seen_generation')
        if resume_from is None:
            first = [self._snapshot(record)]
        else:
            # A client resuming is replayed what followed the generation it
            # has, and is not sent the task again.
            first = [
                event
                for generation, event in record.log
                if generation > resume_from and _visible(event, version)
            ]
        if state in INTERRUPTED and version < V1_1:
            # Before 1.1 a stream ends when the task waits for the client.
            return _Stream(first, None, lambda: None)
        return self._follow(record, version, first, live=live)

    def create_push_config(
        self, config: pb.TaskPushNotificationConfig, version: tuple[int, int]
    ) -> pb.TaskPushNotificationConfig:
        record = self._push_target(config.task_id)
        if not config.url:
            raise AgentError(INVALID_PARAMS, 'Invalid parameters: url is required')
        stored = pb.TaskPushNotificationConfig()
        stored.CopyFrom(config)
        stored.id = config.id or str(uuid.uuid4())
        if not stored.protocol_version:
            stored.protocol_version = f'{version[0]}.{version[1]}'
        record.push_configs[stored.id] = stored
        return stored

    def get_push_config(
        self, request: pb.GetTaskPushNotificationConfigRequest
    ) -> pb.TaskPushNotificationConfig:
        record = self._push_target(request.task_id)
        config = record.push_configs.get(request.id)
        if config is None:
            raise AgentError(
                TASK_NOT_FOUND,
                f'task {request.task_id} has no push notification config {request.id}',
            )
        return config

    def list_push_configs(
        self, request: pb.ListTaskPushNotificationConfigsRequest
    ) -> dict[str, Any]:
        record = self._push_target(request.task_id)
        return {
            'configs': [MessageToDict(c) for c in record.push_configs.values()],
            'nextPageToken': '',
        }

    def delete_push_config(
        self, request: pb.DeleteTaskPushNotificationConfigRequest
    ) -> None:
        # Deleting what is already gone succeeds.
        self._push_target(request.task_id).push_configs.pop(request.id, None)

    # -- accepting a request ----------------------------------------------------

    def _accept(
        self, request: pb.SendMessageRequest, version: tuple[int, int]
    ) -> _Record | pb.Message:
        """Validate a send and record it on the task it creates or continues.

        Returns the task, or a `Message` when the agent answers without one.
        Everything is checked before anything is changed, and nothing here
        waits, so a refused request leaves no trace.
        """
        has_message = request.HasField('message')
        message = request.message
        updates = list(request.artifact_updates)
        push_config = (
            request.configuration.task_push_notification_config
            if request.configuration.HasField('task_push_notification_config')
            else None
        )

        if push_config is not None:
            if not self._pushes:
                raise AgentError(
                    PUSH_NOTIFICATION_NOT_SUPPORTED,
                    'this agent does not support push notifications',
                )
            if not push_config.url:
                raise AgentError(
                    INVALID_PARAMS,
                    'Invalid parameters: a push notification config needs a url',
                )

        if updates and not self._accepts_user_artifacts:
            raise AgentError(
                UNSUPPORTED_OPERATION,
                'this agent does not accept client-written artifacts',
            )

        if has_message:
            if not message.message_id:
                raise AgentError(
                    INVALID_PARAMS, 'Invalid parameters: message.messageId is required'
                )
            for part in message.parts:
                if part.media_type and part.media_type not in INPUT_MODES:
                    raise AgentError(
                        CONTENT_TYPE_NOT_SUPPORTED,
                        f'media type {part.media_type} is not supported; '
                        f'this agent accepts {", ".join(INPUT_MODES)}',
                    )
            task_id = message.task_id
            context_id = message.context_id
        else:
            # Artifacts on their own can only go to a task that exists.
            named = {update.task_id for update in updates}
            if len(named) != 1 or '' in named:
                raise AgentError(
                    INVALID_PARAMS,
                    'Invalid parameters: a message is required unless every '
                    'artifact update names the same existing task',
                )
            task_id = named.pop()
            context_id = next((u.context_id for u in updates if u.context_id), '')

        if task_id:
            record = self._find(task_id)
            if record.task.status.state in TERMINAL:
                raise AgentError(
                    UNSUPPORTED_OPERATION,
                    f'task {task_id} is in a terminal state and accepts no further input',
                )
            if context_id and context_id != record.task.context_id:
                raise AgentError(
                    INVALID_PARAMS,
                    f'Invalid parameters: task {task_id} does not belong to '
                    f'context {context_id}',
                )
            expected = _optional(request.configuration, 'if_generation_match')
            if expected is not None and expected != record.task.generation:
                raise AgentError(
                    TASK_GENERATION_MISMATCH,
                    f'task {task_id} is at generation {record.task.generation}, '
                    f'not {expected}; read the task again and retry',
                )
        else:
            behavior, argument = _behavior_in(_text_of(message))
            if behavior == 'tck-message-response':
                # Answered before any task exists: nothing is persisted.
                return pb.Message(
                    message_id=str(uuid.uuid4()),
                    context_id=context_id or str(uuid.uuid4()),
                    role=pb.ROLE_AGENT,
                    parts=[_text_part('a direct message response')],
                )
            record = self._create(context_id or str(uuid.uuid4()), behavior, argument, version)

        if push_config is not None:
            # Registered before the task changes, so that every change from
            # here on is delivered.
            config = pb.TaskPushNotificationConfig()
            config.CopyFrom(push_config)
            config.task_id = record.task.id
            self.create_push_config(config, version)

        # The message first, then the artifacts in the order they were listed.
        if has_message:
            self._add_client_message(record, message)
        for update in updates:
            self._add_artifact(
                record,
                update.artifact,
                append=update.append,
                last_chunk=update.last_chunk,
                role=pb.ROLE_USER,
            )
        return record

    @property
    def _accepts_user_artifacts(self) -> bool:
        return self.serves_1_1 and not self.reduced_capabilities

    def _find(self, task_id: str) -> _Record:
        record = self._tasks.get(task_id)
        if record is None:
            raise AgentError(
                TASK_NOT_FOUND, f'task {task_id} was not found; it does not exist'
            )
        return record

    @property
    def _pushes(self) -> bool:
        return not self.reduced_capabilities and self._push_sender is not None

    def _push_target(self, task_id: str) -> _Record:
        if not self._pushes:
            raise AgentError(
                PUSH_NOTIFICATION_NOT_SUPPORTED,
                'this agent does not support push notifications',
            )
        return self._find(task_id)

    def _create(
        self, context_id: str, behavior: str, argument: str, version: tuple[int, int]
    ) -> _Record:
        task = pb.Task(id=str(uuid.uuid4()), context_id=context_id, generation=1)
        task.status.state = pb.TASK_STATE_SUBMITTED
        task.status.timestamp.GetCurrentTime()
        record = _Record(task=task, behavior=behavior, argument=argument, version=version)
        self._tasks[task.id] = record
        self._touch(record)
        return record

    # -- mutations --------------------------------------------------------------
    #
    # Each of these changes the task once, advances its generation by one and
    # reports the change as exactly one event carrying the new generation.

    def _add_client_message(self, record: _Record, message: pb.Message) -> None:
        stored = pb.Message()
        stored.CopyFrom(message)
        stored.task_id = record.task.id
        stored.context_id = record.task.context_id

        generation = self._advance(record)
        record.task.timeline.add(generation=generation).message.CopyFrom(stored)
        record.task.history.add().CopyFrom(stored)

        event = pb.StreamResponse()
        event.message_update.task_id = record.task.id
        event.message_update.context_id = record.task.context_id
        event.message_update.message.CopyFrom(stored)
        event.message_update.generation = generation
        self._emit(record, generation, event)

    def _set_status(self, record: _Record, state: int, text: str = '') -> None:
        status = record.task.status
        generation = self._advance(record)
        status.Clear()
        status.state = state
        status.timestamp.GetCurrentTime()
        if text:
            said = status.message
            said.message_id = str(uuid.uuid4())
            said.task_id = record.task.id
            said.context_id = record.task.context_id
            said.role = pb.ROLE_AGENT
            said.parts.append(_text_part(text))
            record.task.history.add().CopyFrom(said)
        # An agent's message is recorded in the timeline inside its status.
        record.task.timeline.add(generation=generation).status.CopyFrom(status)

        event = pb.StreamResponse()
        event.status_update.task_id = record.task.id
        event.status_update.context_id = record.task.context_id
        event.status_update.status.CopyFrom(status)
        event.status_update.generation = generation
        self._emit(record, generation, event)

    def _add_artifact(
        self,
        record: _Record,
        chunk: pb.Artifact,
        *,
        append: bool = False,
        last_chunk: bool = True,
        role: int = pb.ROLE_AGENT,
    ) -> None:
        generation = self._advance(record)
        stored = next(
            (a for a in record.task.artifacts if a.artifact_id == chunk.artifact_id),
            None,
        )
        if stored is not None and append:
            stored.parts.extend(chunk.parts)
        else:
            if stored is None:
                stored = record.task.artifacts.add()
            stored.CopyFrom(chunk)
            # The generation an artifact sits at is the server's to assign,
            # whoever wrote the artifact.
            stored.ClearField('end_generation')
            stored.start_generation = generation
            stored.role = role
        if last_chunk:
            stored.end_generation = generation

        event = pb.StreamResponse()
        update = event.artifact_update
        update.task_id = record.task.id
        update.context_id = record.task.context_id
        update.artifact.CopyFrom(chunk)
        update.artifact.role = role
        update.artifact.start_generation = stored.start_generation
        update.artifact.ClearField('end_generation')
        if last_chunk:
            update.artifact.end_generation = generation
        update.append = append
        update.last_chunk = last_chunk
        update.generation = generation
        self._emit(record, generation, event)

    def _advance(self, record: _Record) -> int:
        record.task.generation += 1
        return record.task.generation

    def _emit(self, record: _Record, generation: int, event: pb.StreamResponse) -> None:
        """Record one mutation's event and send it to everyone following."""
        record.log.append((generation, event))
        self._touch(record)

        for subscriber in record.subscribers:
            if _visible(event, subscriber.version):
                subscriber.queue.put_nowait(event)

        state = record.task.status.state
        if event.WhichOneof('payload') == 'status_update':
            for subscriber in list(record.subscribers):
                # A terminal state ends every stream. A state that waits for
                # the client ends one only for a client older than 1.1.
                if state in TERMINAL or (state in INTERRUPTED and subscriber.version < V1_1):
                    subscriber.queue.put_nowait(None)
                    record.subscribers.remove(subscriber)

        for config in record.push_configs.values():
            if _visible(event, _version(config.protocol_version) or (1, 0)):
                self._post(config, event)

        self._notify(record)

    def _touch(self, record: _Record) -> None:
        self._sequence += 1
        record.touched = self._sequence

    def _notify(self, record: _Record) -> None:
        changed, record.changed = record.changed, asyncio.Event()
        changed.set()

    async def _until(self, record: _Record, holds: Callable[[], bool]) -> None:
        """Wait until ``holds()`` is true of the record."""
        while not holds():
            await record.changed.wait()

    # -- views and streams --------------------------------------------------------

    def _view(
        self,
        record: _Record,
        *,
        history_length: int | None = None,
        timeline_length: int | None = None,
    ) -> pb.Task:
        """A copy of the task, cut to what the request asked for."""
        view = pb.Task()
        view.CopyFrom(record.task)
        _trim(view.history, history_length)
        _trim(view.timeline, timeline_length)
        return view

    def _snapshot(self, record: _Record) -> pb.StreamResponse:
        return pb.StreamResponse(task=self._view(record))

    def _follow(
        self,
        record: _Record,
        version: tuple[int, int],
        first: list[pb.StreamResponse],
        *,
        live: bool = False,
    ) -> _Stream:
        subscriber = _Subscriber(version, live=live)
        record.subscribers.append(subscriber)

        def hang_up() -> None:
            if subscriber in record.subscribers:
                record.subscribers.remove(subscriber)

        return _Stream(first, subscriber, hang_up)

    # -- push notifications -------------------------------------------------------

    def _post(self, config: pb.TaskPushNotificationConfig, event: pb.StreamResponse) -> None:
        """Queue one delivery. Deliveries leave in the order they were queued."""
        headers = {'Content-Type': 'application/a2a+json'}
        if config.token:
            headers['X-A2A-Notification-Token'] = config.token
        if config.authentication.scheme:
            headers['Authorization'] = (
                f'{config.authentication.scheme} {config.authentication.credentials}'
            )
        if self._outbox is None:
            self._outbox = asyncio.Queue()
            self._postman = asyncio.create_task(self._deliver())
        self._outbox.put_nowait((config.url, headers, MessageToDict(event)))

    async def _deliver(self) -> None:
        assert self._outbox is not None and self._push_sender is not None
        while True:
            url, headers, body = await self._outbox.get()
            try:
                await self._push_sender(url, headers, body)
            except Exception:  # noqa: BLE001
                # A receiver that refuses one notification still gets the next.
                pass

    # -- behaviors ----------------------------------------------------------------

    def _hand_over(self, record: _Record, request: pb.SendMessageRequest) -> None:
        """Give an accepted request to the task's behavior."""
        record.inbox.append(request)
        record.idle = False
        if record.worker is None:
            record.worker = asyncio.create_task(self._play(record))
        self._notify(record)

    async def _next_request(self, record: _Record) -> pb.SendMessageRequest:
        """The next request the client sends to the task."""
        while not record.inbox:
            record.idle = True
            self._notify(record)
            await record.changed.wait()
        record.idle = False
        return record.inbox.popleft()

    def _at_rest(self, record: _Record) -> bool:
        """Has the task stopped changing until the client acts?

        What a blocking send waits for: a terminal state, or a state that
        waits for the client once the behavior has read everything sent.
        """
        state = record.task.status.state
        return state in TERMINAL or (
            state in INTERRUPTED and record.idle and not record.inbox
        )

    async def _play(self, record: _Record) -> None:
        """Run the task's behavior from its first request to its end."""
        first = await self._next_request(record)
        behavior = self.behaviors.get(record.behavior, self._complete)
        if not record.behavior and _instruction_in(first.message) is not None:
            behavior = self._follow_instruction
        try:
            if record.behavior and record.behavior not in self.behaviors:
                # A message plainly meant for ACTS that names something this
                # agent does not do. Failing says so; completing would claim
                # a behavior that was never played.
                self._set_status(record, pb.TASK_STATE_WORKING)
                self._set_status(
                    record,
                    pb.TASK_STATE_FAILED,
                    f'unimplemented ACTS behavior {record.behavior}',
                )
                return
            await behavior(record, first)
            if record.task.status.state not in TERMINAL:
                self._set_status(
                    record,
                    pb.TASK_STATE_FAILED,
                    f'{record.behavior or "the default behavior"} ended without finishing the task',
                )
        finally:
            self._notify(record)

    async def _complete(self, record: _Record, request: pb.SendMessageRequest) -> None:
        self._set_status(record, pb.TASK_STATE_WORKING)
        self._set_status(
            record, pb.TASK_STATE_COMPLETED, f'{record.behavior or "task"} completed'
        )

    async def _fail(self, record: _Record, request: pb.SendMessageRequest) -> None:
        self._set_status(record, pb.TASK_STATE_WORKING)
        self._set_status(record, pb.TASK_STATE_FAILED, 'the task failed as requested')

    async def _auth_required(self, record: _Record, request: pb.SendMessageRequest) -> None:
        while True:
            self._set_status(record, pb.TASK_STATE_WORKING)
            self._set_status(
                record,
                pb.TASK_STATE_AUTH_REQUIRED,
                'authorization is required to continue this task',
            )
            await self._next_request(record)

    async def _multi_turn(self, record: _Record, request: pb.SendMessageRequest) -> None:
        while True:
            self._set_status(record, pb.TASK_STATE_WORKING)
            if _text_of(request.message).strip().lower() == 'done':
                self._set_status(record, pb.TASK_STATE_COMPLETED, 'multi-turn complete')
                return
            self._set_status(record, pb.TASK_STATE_INPUT_REQUIRED, 'more input please')
            request = await self._next_request(record)

    async def _wait_for_cancel(self, record: _Record, request: pb.SendMessageRequest) -> None:
        self._set_status(record, pb.TASK_STATE_WORKING)
        # Nobody sets this. Canceling the task cancels the wait.
        await asyncio.Event().wait()

    async def _long_running(self, record: _Record, request: pb.SendMessageRequest) -> None:
        self._set_status(record, pb.TASK_STATE_WORKING)
        await asyncio.sleep(self.long_running_s)
        self._add_artifact(
            record,
            pb.Artifact(
                artifact_id=str(uuid.uuid4()),
                name='long-running',
                parts=[_text_part('long running result')],
            ),
        )
        self._set_status(record, pb.TASK_STATE_COMPLETED, 'long running work finished')

    async def _artifact(self, record: _Record, request: pb.SendMessageRequest) -> None:
        parts = {
            'tck-artifact-text': [_text_part('generated text content')],
            'tck-artifact-data': [_data_part({'key': 'value', 'count': 1})],
            'tck-artifact-file': [
                pb.Part(raw=b'file bytes', filename='document.txt', media_type='text/plain')
            ],
            'tck-artifact-file-url': [
                pb.Part(
                    url='https://example.com/document.txt',
                    filename='document.txt',
                    media_type='text/plain',
                )
            ],
        }[record.behavior]
        self._set_status(record, pb.TASK_STATE_WORKING)
        self._add_artifact(
            record,
            pb.Artifact(artifact_id=str(uuid.uuid4()), name=record.behavior, parts=parts),
        )
        self._set_status(record, pb.TASK_STATE_COMPLETED, f'{record.behavior} completed')

    async def _stream_basic(self, record: _Record, request: pb.SendMessageRequest) -> None:
        self._set_status(record, pb.TASK_STATE_WORKING, 'streaming started')
        self._add_artifact(
            record,
            pb.Artifact(
                artifact_id=str(uuid.uuid4()),
                name='streamed',
                parts=[_text_part('streamed content')],
            ),
        )
        self._set_status(record, pb.TASK_STATE_COMPLETED, 'streaming finished')

    async def _stream_chunked(self, record: _Record, request: pb.SendMessageRequest) -> None:
        self._set_status(record, pb.TASK_STATE_WORKING, 'streaming started')
        artifact_id = str(uuid.uuid4())
        chunks = ['chunk one ', 'chunk two ', 'chunk three']
        for index, chunk in enumerate(chunks):
            self._add_artifact(
                record,
                pb.Artifact(artifact_id=artifact_id, name='chunked', parts=[_text_part(chunk)]),
                append=index > 0,
                last_chunk=index == len(chunks) - 1,
            )
        self._set_status(record, pb.TASK_STATE_COMPLETED, 'streaming finished')

    async def _client_parse(self, record: _Record, request: pb.SendMessageRequest) -> None:
        self._set_status(record, pb.TASK_STATE_WORKING)
        asked = next(
            (
                MessageToDict(part.data)
                for part in request.message.parts
                if part.WhichOneof('content') == 'data'
            ),
            None,
        )
        if not isinstance(asked, dict) or 'operation' not in asked:
            self._set_status(
                record,
                pb.TASK_STATE_FAILED,
                'tck-client-parse needs a data part with operation and wire_payload',
            )
            return
        try:
            if 'wire_stream' in asked:
                parsed = _client_read_stream(asked['operation'], asked['wire_stream'])
            else:
                parsed = _client_parse(asked['operation'], asked.get('wire_payload'))
        except (ParseError, ValueError) as exc:
            self._set_status(
                record, pb.TASK_STATE_FAILED, f'the client could not parse the payload: {exc}'
            )
            return
        self._add_artifact(
            record,
            pb.Artifact(
                artifact_id=str(uuid.uuid4()),
                name='tck-client-parse',
                parts=[_data_part(parsed)],
            ),
        )
        self._set_status(record, pb.TASK_STATE_COMPLETED, f'{asked["operation"]} parsed')

    async def _hold_working(self, record: _Record, request: pb.SendMessageRequest) -> None:
        try:
            hold_ms = int(record.argument)
        except ValueError:
            hold_ms = 2000
        self._set_status(record, pb.TASK_STATE_WORKING)
        # No update in this time. A message the client sends meanwhile is
        # added to the task when it is accepted, and is not answered.
        await asyncio.sleep(hold_ms / 1000)
        self._set_status(record, pb.TASK_STATE_COMPLETED, 'held, then completed')

    async def _progress_update(self, record: _Record, request: pb.SendMessageRequest) -> None:
        self._set_status(record, pb.TASK_STATE_WORKING)
        for step in (1, 2):
            await asyncio.sleep(0.1)
            self._set_status(record, pb.TASK_STATE_WORKING, f'progress {step} of 2')
        await asyncio.sleep(0.1)
        self._set_status(record, pb.TASK_STATE_COMPLETED, 'progress complete')

    async def _stream_input_required(
        self, record: _Record, request: pb.SendMessageRequest
    ) -> None:
        self._set_status(record, pb.TASK_STATE_WORKING)
        self._set_status(record, pb.TASK_STATE_INPUT_REQUIRED, 'input is required to continue')
        await self._next_request(record)
        self._set_status(record, pb.TASK_STATE_WORKING)
        self._set_status(record, pb.TASK_STATE_COMPLETED, 'input received, completed')

    async def _user_artifact_echo(
        self, record: _Record, request: pb.SendMessageRequest
    ) -> None:
        first = True
        while True:
            if request.HasField('message') and _text_of(request.message).strip() == 'done':
                self._set_status(record, pb.TASK_STATE_COMPLETED, 'artifacts received, completed')
                return
            if request.artifact_updates:
                last = request.artifact_updates[-1].artifact
                received = next(
                    (p.text for p in last.parts if p.WhichOneof('content') == 'text'), ''
                )
                self._set_status(
                    record, pb.TASK_STATE_INPUT_REQUIRED, f'artifact-received:{received}'
                )
            elif first:
                self._set_status(record, pb.TASK_STATE_INPUT_REQUIRED)
            # Otherwise a request without an artifact changes nothing: the
            # status and its message stay as they were.
            first = False
            request = await self._next_request(record)

    async def _live_echo(self, record: _Record, request: pb.SendMessageRequest) -> None:
        """`tck-live-echo` and `tck-live-reject`.

        Every message after the first is answered with a status update that
        counts it, until one says `done`. A message `tck-live-reject` declines
        never gets here, so it is neither answered nor counted.
        """
        self._set_status(record, pb.TASK_STATE_WORKING)
        answered = 0
        while True:
            request = await self._next_request(record)
            if _text_of(request.message).strip().lower() == 'done':
                self._set_status(record, pb.TASK_STATE_COMPLETED, 'live exchange complete')
                return
            answered += 1
            self._set_status(record, pb.TASK_STATE_WORKING, f'live-echo:{answered}')

    # -- ITK instructions ---------------------------------------------------------
    #
    # What the agent does with a message that carries an instruction: reply
    # with text, call another agent, or a series of both.

    async def _follow_instruction(
        self, record: _Record, request: pb.SendMessageRequest
    ) -> None:
        self._set_status(record, pb.TASK_STATE_WORKING)
        try:
            instruction = instruction_pb2.Instruction.FromString(
                _instruction_in(request.message)
            )
            major, minor = record.version
            reply = '\n'.join(await self._carry_out(instruction, f'{major}.{minor}'))
        except Exception as exc:  # noqa: BLE001
            # Whatever went wrong, the agent that sent the instruction is
            # told. Left to end the behavior, it would leave the task working
            # and that agent waiting.
            self._set_status(
                record, pb.TASK_STATE_FAILED, f'the instruction failed: {exc!r}'
            )
            return
        if _holds(instruction):
            self._set_status(record, pb.TASK_STATE_WORKING, f'{reply}\n{TASK_FINISHED}')
            # Nobody sets this. Canceling the task cancels the wait.
            await asyncio.Event().wait()
        else:
            self._set_status(record, pb.TASK_STATE_COMPLETED, reply)

    async def _carry_out(
        self, instruction: instruction_pb2.Instruction, served: str
    ) -> list[str]:
        """What the instruction makes this agent say, in order.

        ``served`` is the A2A version of the request that carried it.
        """
        step = instruction.WhichOneof('step')
        if step == 'return_response':
            response = instruction.return_response
            if response.report_protocol_version:
                return [f'{response.response} served:{served}']
            return [response.response]
        if step == 'steps':
            said: list[str] = []
            for child in instruction.steps.instructions:
                said += await self._carry_out(child, served)
            return said
        if step == 'call_agent':
            return await self._call_agent(instruction.call_agent)
        raise InstructionError('the instruction names no step')

    async def _call_agent(self, call: instruction_pb2.CallAgent) -> list[str]:
        """Pass the nested instruction to another agent; return what it says.

        An agent that fails says why, and that text is returned like any
        other reply.
        """
        if self._client is None:
            raise InstructionError('this agent was given nothing to call other agents with')
        if call.transport != 'jsonrpc':
            raise InstructionError(
                f'this agent calls over jsonrpc only, not over {call.transport}'
            )
        behavior = call.WhichOneof('behavior') or 'send_message'
        if behavior not in ('send_message', 'push_notification', 'resubscribe'):
            raise InstructionError(f'this agent does not implement {behavior}')
        if behavior == 'resubscribe' and not call.streaming:
            raise InstructionError('subscribing again needs a streaming call')
        if behavior == 'push_notification' and not call.push_notification.url:
            raise InstructionError('push_notification names no url')
        url, version = await self._interface_of(call)

        request = pb.SendMessageRequest()
        request.message.message_id = str(uuid.uuid4())
        request.message.role = pb.ROLE_USER
        request.message.parts.add(
            raw=call.instruction.SerializeToString(),
            media_type=INSTRUCTION_MEDIA_TYPE,
            filename='instruction.bin',
        )
        if behavior == 'push_notification':
            config = request.configuration.task_push_notification_config
            config.url = f'{call.push_notification.url}/notifications'
            config.token = 'itk-token'
            config.protocol_version = call.push_notification.protocol_version
        params = MessageToDict(request)

        if behavior == 'resubscribe':
            return await self._send_and_subscribe_again(
                url, version, params, resume=call.resubscribe.from_last_seen_generation
            )
        if not call.streaming:
            return _agent_texts(await self._rpc(url, version, 'SendMessage', params))
        said: list[str] = []
        async for event in self._rpc_stream(url, version, 'SendStreamingMessage', params):
            said += _agent_texts(event)
        return said

    async def _interface_of(self, call: instruction_pb2.CallAgent) -> tuple[str, str]:
        """The URL to call the agent at, and the A2A version to call it at.

        The version the instruction asks for. When it asks for none, the
        newest version that this agent serves and the other agent's card lists
        for JSON-RPC.
        """
        card = (await self._client.get(f'{call.agent_card_uri.rstrip("/")}{CARD_PATH}')).json()
        listed = {
            interface.get('protocolVersion'): interface['url']
            for interface in card['supportedInterfaces']
            if interface['protocolBinding'] == 'JSONRPC'
        }
        wanted = [f'{major}.{minor}' for major, minor in self.versions]
        if call.protocol_version:
            wanted = [call.protocol_version]
        for version in wanted:
            if version in listed:
                return listed[version], version
        raise InstructionError(
            f'{call.agent_card_uri} lists no JSON-RPC interface for A2A {" or ".join(wanted)}'
        )

    async def _send_and_subscribe_again(
        self, url: str, version: str, params: dict[str, Any], *, resume: bool
    ) -> list[str]:
        """Start a stream, hang up after its first event, and subscribe again.

        The other agent holds its task open, and marks the end of its reply
        with `task-finished`. Once that has arrived the task is canceled.

        With ``resume`` the second subscription names the generation of the
        task the first stream opened with. The other agent may then continue
        from that generation or open with the task again, and the reply is
        read from either.
        """
        opening = self._rpc_stream(url, version, 'SendStreamingMessage', params)
        async with contextlib.aclosing(opening):
            task = (await anext(opening, {})).get('task')
        if task is None:
            raise InstructionError('the stream did not open with the task')

        subscription = {'id': task['id']}
        if resume:
            subscription['lastSeenGeneration'] = task['generation']
        subscribed = self._rpc_stream(url, version, 'SubscribeToTask', subscription)
        async with contextlib.aclosing(subscribed):
            async for event in subscribed:
                for text in _agent_texts(event):
                    if text.endswith(TASK_FINISHED):
                        await self._rpc(url, version, 'CancelTask', {'id': task['id']})
                        return [text.removesuffix(f'\n{TASK_FINISHED}')]
        raise InstructionError(f'task {task["id"]} ended before it had finished its reply')

    async def _rpc(
        self, url: str, version: str, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """One JSON-RPC call to another agent."""
        response = await self._client.post(
            url, headers={VERSION_HEADER: version}, json=_envelope(method, params)
        )
        return _result(method, response.json())

    async def _rpc_stream(
        self, url: str, version: str, method: str, params: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """The events of one streaming JSON-RPC call to another agent.

        Close the generator to hang up.
        """
        async with self._client.stream(
            'POST', url, headers={VERSION_HEADER: version}, json=_envelope(method, params)
        ) as response:
            if not response.headers.get('Content-Type', '').startswith('text/event-stream'):
                refusal = json.loads(await response.aread())
                raise InstructionError(
                    f'{method} was not answered with a stream: '
                    f'{refusal.get("error", {}).get("message", refusal)}'
                )
            async for line in response.aiter_lines():
                if line.startswith('data: '):
                    yield _result(method, json.loads(line.removeprefix('data: ')))

    # -- shutdown -----------------------------------------------------------------

    async def aclose(self) -> None:
        """Stop every behavior still running and the notification sender."""
        running = [
            record.worker
            for record in self._tasks.values()
            if record.worker is not None and not record.worker.done()
        ]
        if self._postman is not None:
            running.append(self._postman)
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)


def _visible(event: pb.StreamResponse, version: tuple[int, int]) -> bool:
    """Is this event sent to a client of ``version``?

    A client message added to a task is reported only to clients of 1.1 or
    later; an older client does not know the event.
    """
    return event.WhichOneof('payload') != 'message_update' or version >= V1_1


__all__ = [
    'CARD_PATH',
    'INSUFFICIENT_TOKEN',
    'JSONRPC_PATH',
    'VALID_TOKEN',
    'AgentError',
    'MockAgent',
    'VirtualTimeLoop',
    'run_virtual',
]
