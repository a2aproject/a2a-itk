"""The one table binding abstract ACTS names to concrete wire representations.

An ACTS step says ``operation: get_task`` with ``params: {id: ...}``, and an
``expect_error`` says ``error_type: TaskNotFoundError``. Neither names a
method, a path, a status or a code — that is what lets one corpus run against
all three bindings. This module is where those abstract names become wire
facts, and it is deliberately the *only* place they do: a dispatcher that
built its own paths would drift from the one that parsed the errors.

Everything here is data. The dispatchers in :mod:`test_suite.acts.dispatcher`
read it; nothing in this module performs I/O.

## Which spec wins

ACTS §4.1 and §6.2 carry their own mapping tables, and both are **stale**
relative to the normative A2A specification. ACTS §6.2 settles the conflict
itself:

    "If this table conflicts with the A2A specification, the A2A
    specification takes precedence."

So the tables below follow **A2A §5.3** (method mapping) and **A2A §5.4**
(error codes), not the ACTS restatements of them. Where ACTS diverges it is
noted inline, because a reader comparing this file against the ACTS spec
would otherwise think it wrong.

The tables are normative-only. An implementation that answers differently from
§5.4 is reported as non-conformant, which is the point of the suite; widening
a row to accommodate one is not a fix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from test_suite.acts.schema import ErrorType, HttpMethod, Operation


#: Media type for JSON-RPC requests and responses (A2A §9.1).
JSONRPC_CONTENT_TYPE = 'application/json'

#: Media type REST requests and responses **SHOULD** use (A2A §11.1).
REST_CONTENT_TYPE = 'application/a2a+json'

#: Media type of a streaming response on both HTTP bindings (A2A §9.1, §11.7).
SSE_CONTENT_TYPE = 'text/event-stream'

#: Fully-qualified gRPC service name, as it appears in the generated
#: descriptor — note the ``lf.`` prefix, which the A2A spec's prose omits.
GRPC_SERVICE = 'lf.a2a.v1.A2AService'

#: Where an agent card is served, unauthenticated (A2A §8.6, IANA §3335).
WELL_KNOWN_AGENT_CARD_PATH = '/.well-known/agent-card.json'

#: ``google.rpc.ErrorInfo.domain`` on every A2A error (A2A §11.6).
ERROR_DOMAIN = 'a2a-protocol.org'

#: ProtoJSON ``@type`` marking the ``ErrorInfo`` detail (A2A §11.6).
ERROR_INFO_TYPE = 'type.googleapis.com/google.rpc.ErrorInfo'

_PLACEHOLDER_RE = re.compile(r'\{(\w+)\}')


@dataclass(frozen=True, slots=True)
class OperationBinding:
    """How one abstract operation reaches the wire on each binding."""

    jsonrpc_method: str
    grpc_method: str
    rest_method: HttpMethod
    rest_path: str
    streaming: bool = False
    #: Served over plain HTTP on *every* binding, not as an RPC. True only for
    #: ``get_agent_card``: the card is what tells a client which binding to
    #: speak, so fetching it cannot presuppose one. Confirmed by the generated
    #: service, which has no ``GetAgentCard`` RPC.
    http_only: bool = False
    #: Protobuf message type names, resolved by the gRPC dispatcher against
    #: ``pyproto.a2a_pb2``, or against ``google.protobuf`` when written
    #: ``google.protobuf.Empty``. Names rather than classes so this module
    #: stays data-only and importable without the generated code.
    grpc_request: str = ''
    grpc_response: str = ''

    @property
    def path_params(self) -> tuple[str, ...]:
        """Param names the REST path consumes, in order of appearance.

        The dispatcher must strip these from the payload after substituting
        them, or they are sent twice — once in the path and once in the body.
        """
        return tuple(_PLACEHOLDER_RE.findall(self.rest_path))

    def format_path(self, params: Mapping[str, object]) -> tuple[str, dict[str, object]]:
        """Substitute path placeholders, returning the path and what is left.

        Raises :class:`KeyError` naming the missing param rather than emitting
        a path with a literal ``{id}`` in it, which would 404 somewhere far
        from the cause.
        """
        remaining = dict(params)
        path = self.rest_path
        for name in self.path_params:
            if name not in remaining:
                raise KeyError(
                    f'{self.rest_path} needs param {name!r}; got '
                    f'{sorted(remaining)}'
                )
            path = path.replace('{' + name + '}', str(remaining.pop(name)))
        return path, remaining


@dataclass(frozen=True, slots=True)
class ErrorBinding:
    """How one abstract error name appears on each binding.

    ``grpc_status``, ``http_status`` and ``reason`` are ``None`` for the
    standard JSON-RPC errors, which A2A defines only for the JSON-RPC binding
    (§9.5) and never maps onto gRPC or REST. ``None`` means "the spec does not
    say", and the runner should report an unmappable assertion rather than
    invent a status — see :func:`binding_for_error`.
    """

    jsonrpc_code: int
    grpc_status: str | None = None
    http_status: int | None = None
    reason: str | None = None
    #: Set when the ACTS name is not the A2A name, or has no A2A row at all.
    #: The value is the A2A error this one is answered by.
    aliases: ErrorType | None = None


# --------------------------------------------------------------------------
# Operations — A2A §5.3.
#
# Path placeholders are named for the params the corpus actually supplies,
# which is not always what §11.3 writes. The spec spells the push-config
# paths `/tasks/{id}/pushNotificationConfigs/{configId}`; every corpus step
# passes `taskId` for the task and `id` for the config, so the templates use
# those names and the substitution is total. Renaming them to match the spec's
# prose would break every push-config step for no gain — the placeholder is an
# internal join key, not part of the wire format.
# --------------------------------------------------------------------------

_OPERATIONS: dict[Operation, OperationBinding] = {
    Operation.SEND_MESSAGE: OperationBinding(
        jsonrpc_method='SendMessage',
        grpc_method='SendMessage',
        rest_method=HttpMethod.POST,
        rest_path='/message:send',
        grpc_request='SendMessageRequest',
        grpc_response='SendMessageResponse',
    ),
    Operation.SEND_STREAMING_MESSAGE: OperationBinding(
        jsonrpc_method='SendStreamingMessage',
        grpc_method='SendStreamingMessage',
        rest_method=HttpMethod.POST,
        rest_path='/message:stream',
        streaming=True,
        grpc_request='SendMessageRequest',
        grpc_response='StreamResponse',
    ),
    Operation.GET_TASK: OperationBinding(
        jsonrpc_method='GetTask',
        grpc_method='GetTask',
        rest_method=HttpMethod.GET,
        rest_path='/tasks/{id}',
        grpc_request='GetTaskRequest',
        grpc_response='Task',
    ),
    Operation.LIST_TASKS: OperationBinding(
        jsonrpc_method='ListTasks',
        grpc_method='ListTasks',
        rest_method=HttpMethod.GET,
        rest_path='/tasks',
        grpc_request='ListTasksRequest',
        grpc_response='ListTasksResponse',
    ),
    Operation.CANCEL_TASK: OperationBinding(
        jsonrpc_method='CancelTask',
        grpc_method='CancelTask',
        rest_method=HttpMethod.POST,
        rest_path='/tasks/{id}:cancel',
        grpc_request='CancelTaskRequest',
        grpc_response='Task',
    ),
    # ACTS §4.1 says `GET` here. A2A §5.3 and §11.3.2 both say POST, and a GET
    # cannot carry the subscribe body, so POST it is.
    Operation.SUBSCRIBE_TO_TASK: OperationBinding(
        jsonrpc_method='SubscribeToTask',
        grpc_method='SubscribeToTask',
        rest_method=HttpMethod.POST,
        rest_path='/tasks/{id}:subscribe',
        streaming=True,
        grpc_request='SubscribeToTaskRequest',
        grpc_response='StreamResponse',
    ),
    # Not an RPC on any binding: the card is how a client learns which
    # bindings the agent speaks, so it is fetched over plain HTTP first.
    Operation.GET_AGENT_CARD: OperationBinding(
        jsonrpc_method='',
        grpc_method='',
        rest_method=HttpMethod.GET,
        rest_path=WELL_KNOWN_AGENT_CARD_PATH,
        http_only=True,
    ),
    # Absent from ACTS §4.1 entirely; A2A §5.3 defines it, and three corpus
    # tests exercise it through raw steps.
    Operation.GET_EXTENDED_AGENT_CARD: OperationBinding(
        jsonrpc_method='GetExtendedAgentCard',
        grpc_method='GetExtendedAgentCard',
        rest_method=HttpMethod.GET,
        rest_path='/extendedAgentCard',
        grpc_request='GetExtendedAgentCardRequest',
        grpc_response='AgentCard',
    ),
    # The four push-config rows below are where ACTS §4.1 is most wrong: it
    # omits the `Task` infix and writes the collection as `pushNotifications`.
    # A dispatcher built from that table 404s on every push-config call.
    # The create RPC takes the resource itself as its request message, not a
    # wrapper — the one row where request and response types coincide.
    Operation.CREATE_PUSH_CONFIG: OperationBinding(
        jsonrpc_method='CreateTaskPushNotificationConfig',
        grpc_method='CreateTaskPushNotificationConfig',
        rest_method=HttpMethod.POST,
        rest_path='/tasks/{taskId}/pushNotificationConfigs',
        grpc_request='TaskPushNotificationConfig',
        grpc_response='TaskPushNotificationConfig',
    ),
    Operation.GET_PUSH_CONFIG: OperationBinding(
        jsonrpc_method='GetTaskPushNotificationConfig',
        grpc_method='GetTaskPushNotificationConfig',
        rest_method=HttpMethod.GET,
        rest_path='/tasks/{taskId}/pushNotificationConfigs/{id}',
        grpc_request='GetTaskPushNotificationConfigRequest',
        grpc_response='TaskPushNotificationConfig',
    ),
    Operation.LIST_PUSH_CONFIGS: OperationBinding(
        jsonrpc_method='ListTaskPushNotificationConfigs',
        grpc_method='ListTaskPushNotificationConfigs',
        rest_method=HttpMethod.GET,
        rest_path='/tasks/{taskId}/pushNotificationConfigs',
        grpc_request='ListTaskPushNotificationConfigsRequest',
        grpc_response='ListTaskPushNotificationConfigsResponse',
    ),
    Operation.DELETE_PUSH_CONFIG: OperationBinding(
        jsonrpc_method='DeleteTaskPushNotificationConfig',
        grpc_method='DeleteTaskPushNotificationConfig',
        rest_method=HttpMethod.DELETE,
        rest_path='/tasks/{taskId}/pushNotificationConfigs/{id}',
        grpc_request='DeleteTaskPushNotificationConfigRequest',
        grpc_response='google.protobuf.Empty',
    ),
}

#: Abstract operation -> its wire binding on all three transports.
OPERATIONS: Mapping[Operation, OperationBinding] = MappingProxyType(_OPERATIONS)


# --------------------------------------------------------------------------
# Errors — A2A §5.4 for the nine A2A-specific errors, §9.5 for the standard
# JSON-RPC ones. `reason` is the §11.6 ErrorInfo string: UPPER_SNAKE of the
# name minus the `Error` suffix. That derivation is exact for all nine, but it
# stays a table rather than a function because it is the wire contract, and one
# row of drift upstream should show up as a diff here.
# --------------------------------------------------------------------------

_ERRORS: dict[ErrorType, ErrorBinding] = {
    ErrorType.TASK_NOT_FOUND: ErrorBinding(
        jsonrpc_code=-32001,
        grpc_status='NOT_FOUND',
        http_status=404,
        reason='TASK_NOT_FOUND',
    ),
    ErrorType.TASK_NOT_CANCELABLE: ErrorBinding(
        jsonrpc_code=-32002,
        grpc_status='FAILED_PRECONDITION',
        http_status=400,
        reason='TASK_NOT_CANCELABLE',
    ),
    ErrorType.PUSH_NOTIFICATION_NOT_SUPPORTED: ErrorBinding(
        jsonrpc_code=-32003,
        grpc_status='FAILED_PRECONDITION',
        http_status=400,
        reason='PUSH_NOTIFICATION_NOT_SUPPORTED',
    ),
    ErrorType.UNSUPPORTED_OPERATION: ErrorBinding(
        jsonrpc_code=-32004,
        grpc_status='FAILED_PRECONDITION',
        http_status=400,
        reason='UNSUPPORTED_OPERATION',
    ),
    ErrorType.CONTENT_TYPE_NOT_SUPPORTED: ErrorBinding(
        jsonrpc_code=-32005,
        grpc_status='INVALID_ARGUMENT',
        http_status=400,
        reason='CONTENT_TYPE_NOT_SUPPORTED',
    ),
    ErrorType.EXTENSION_SUPPORT_REQUIRED: ErrorBinding(
        jsonrpc_code=-32008,
        grpc_status='FAILED_PRECONDITION',
        http_status=400,
        reason='EXTENSION_SUPPORT_REQUIRED',
    ),
    ErrorType.VERSION_NOT_SUPPORTED: ErrorBinding(
        jsonrpc_code=-32009,
        grpc_status='FAILED_PRECONDITION',
        http_status=400,
        reason='VERSION_NOT_SUPPORTED',
    ),
    # ACTS's name for A2A's `ExtendedAgentCardNotConfiguredError`. Same error,
    # same -32007; only the spelling differs, so it binds to the A2A row.
    ErrorType.EXTENDED_CARD_NOT_SUPPORTED: ErrorBinding(
        jsonrpc_code=-32007,
        grpc_status='FAILED_PRECONDITION',
        http_status=400,
        reason='EXTENDED_AGENT_CARD_NOT_CONFIGURED',
    ),
    # ACTS invents this one; A2A has no such error. §3.3.2 says an agent that
    # cannot stream returns `UnsupportedOperationError`, so that is what the
    # wire will carry and what this must match. ACTS §6.2 assigns it -32007,
    # which actually belongs to ExtendedAgentCardNotConfigured — following
    # that would make the two indistinguishable on the wire.
    ErrorType.STREAMING_NOT_SUPPORTED: ErrorBinding(
        jsonrpc_code=-32004,
        grpc_status='FAILED_PRECONDITION',
        http_status=400,
        reason='UNSUPPORTED_OPERATION',
        aliases=ErrorType.UNSUPPORTED_OPERATION,
    ),
    # The standard JSON-RPC errors (A2A §9.5). A2A never maps these onto gRPC
    # or REST, so the other columns stay None rather than being guessed.
    ErrorType.JSON_PARSE: ErrorBinding(jsonrpc_code=-32700),
    ErrorType.METHOD_NOT_FOUND: ErrorBinding(jsonrpc_code=-32601),
    ErrorType.INVALID_PARAMS: ErrorBinding(jsonrpc_code=-32602),
    ErrorType.INTERNAL: ErrorBinding(jsonrpc_code=-32603),
}

#: Abstract error name -> its wire representation on each binding.
ERRORS: Mapping[ErrorType, ErrorBinding] = MappingProxyType(_ERRORS)


# --------------------------------------------------------------------------
# gRPC status -> HTTP status, the canonical google.rpc.Code transcoding.
#
# gRPC has no HTTP status, but 86 of the corpus's 111 tests are binding-
# agnostic and many assert `status: 200` to mean "the call succeeded". Giving
# the gRPC dispatcher a derived status lets those run unchanged. The
# derivation is the spec's own: A2A §5.4's HTTP column is exactly this table
# applied to its gRPC column.
# --------------------------------------------------------------------------

GRPC_STATUS_TO_HTTP: Mapping[str, int] = MappingProxyType({
    'OK': 200,
    'CANCELLED': 499,
    'UNKNOWN': 500,
    'INVALID_ARGUMENT': 400,
    'DEADLINE_EXCEEDED': 504,
    'NOT_FOUND': 404,
    'ALREADY_EXISTS': 409,
    'PERMISSION_DENIED': 403,
    'RESOURCE_EXHAUSTED': 429,
    'FAILED_PRECONDITION': 400,
    'ABORTED': 409,
    'OUT_OF_RANGE': 400,
    'UNIMPLEMENTED': 501,
    'INTERNAL': 500,
    'UNAVAILABLE': 503,
    'DATA_LOSS': 500,
    'UNAUTHENTICATED': 401,
})


def http_status_for_grpc(grpc_status: str) -> int:
    """Canonical HTTP status for a gRPC status name; 500 when unrecognised."""
    return GRPC_STATUS_TO_HTTP.get(grpc_status, 500)


def binding_for_operation(operation: Operation) -> OperationBinding:
    """The wire binding for ``operation``. Total over the enum."""
    return OPERATIONS[operation]


def binding_for_error(error: ErrorType) -> ErrorBinding:
    """The wire binding for ``error``. Total over the enum."""
    return ERRORS[error]


# Reverse lookups need a single winner per wire value, and two ACTS names
# share -32004 / UNSUPPORTED_OPERATION. The alias loses: a wire error is
# reported under the A2A name, and `StreamingNotSupportedError` is not one.
_CANONICAL = {e: b for e, b in _ERRORS.items() if b.aliases is None}

_BY_JSONRPC_CODE: Mapping[int, ErrorType] = MappingProxyType(
    {b.jsonrpc_code: e for e, b in _CANONICAL.items()}
)

_BY_REASON: Mapping[str, ErrorType] = MappingProxyType(
    {b.reason: e for e, b in _CANONICAL.items() if b.reason is not None}
)


def error_for_jsonrpc_code(code: int) -> ErrorType | None:
    """Abstract error name for a JSON-RPC ``error.code``, if it names one."""
    return _BY_JSONRPC_CODE.get(code)


def error_for_reason(reason: str) -> ErrorType | None:
    """Abstract error name for a ``google.rpc.ErrorInfo.reason``, if known.

    This is the signal to match REST and gRPC errors on. A2A §11.6 mandates
    the ``ErrorInfo`` precisely because HTTP status is not injective —
    ``TaskNotCancelableError`` and ``PushNotificationNotSupportedError`` are
    both 400 — so status alone cannot identify the error and this can.
    """
    return _BY_REASON.get(reason)


def errors_sharing_http_status(status: int) -> tuple[ErrorType, ...]:
    """Every abstract error that maps to ``status``.

    Lets a caller say "this 400 is consistent with the expected error" without
    pretending the status identified it.
    """
    return tuple(
        e for e, b in _CANONICAL.items() if b.http_status == status
    )
