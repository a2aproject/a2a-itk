"""The gRPC adapter (A2A §10).

Driven against a real ``grpc.aio`` server running an in-process fake servicer
on an ephemeral port. A mock channel would let a wrong method path or a
mis-typed request message pass unnoticed, which is most of what can actually
go wrong here — so the tests pay for a real server.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Iterator

import grpc
import pytest
from google.protobuf.any_pb2 import Any as ProtoAny
from google.rpc import error_details_pb2, status_pb2

from pyproto import a2a_pb2, a2a_pb2_grpc

from test_suite.acts.dispatcher import (
    DispatchError,
    GrpcDispatcher,
    UnsupportedByBinding,
)
from test_suite.acts.dispatcher.grpc import STATUS_DETAILS_KEY
from test_suite.acts.schema import (
    ErrorType,
    HttpMethod,
    Operation,
    RawBlock,
    TransportBinding,
)


def error_trailers(
    code: int, reason: str, message: str = 'boom'
) -> list[tuple[str, bytes]]:
    """A ``google.rpc.Status`` in ``grpc-status-details-bin``.

    This is where ``ErrorInfo`` — and so the abstract error name — actually
    travels on gRPC; ``details()`` carries only the human-readable message.
    """
    detail = ProtoAny()
    detail.Pack(
        error_details_pb2.ErrorInfo(reason=reason, domain='a2a-protocol.org')
    )
    status = status_pb2.Status(code=code, message=message, details=[detail])
    return [(STATUS_DETAILS_KEY, status.SerializeToString())]


class FakeAgent(a2a_pb2_grpc.A2AServiceServicer):
    """Records what it was called with and replies as configured."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, list]] = []
        #: RPC name -> (grpc.StatusCode, reason or None, message)
        self.fail_with: dict[str, tuple[grpc.StatusCode, str | None, str]] = {}
        self.task = a2a_pb2.Task(
            id='t1',
            context_id='c1',
            status=a2a_pb2.TaskStatus(state=a2a_pb2.TASK_STATE_COMPLETED),
        )
        self.stream_events = 2

    async def _record(self, name, request, context):
        self.calls.append((name, request, list(context.invocation_metadata())))
        if name in self.fail_with:
            code, reason, message = self.fail_with[name]
            if reason is not None:
                context.set_trailing_metadata(error_trailers(code.value[0], reason, message))
            await context.abort(code, message)

    async def SendMessage(self, request, context):
        await self._record('SendMessage', request, context)
        return a2a_pb2.SendMessageResponse(task=self.task)

    async def GetTask(self, request, context):
        await self._record('GetTask', request, context)
        return self.task

    async def ListTasks(self, request, context):
        await self._record('ListTasks', request, context)
        return a2a_pb2.ListTasksResponse(tasks=[self.task])

    async def CancelTask(self, request, context):
        await self._record('CancelTask', request, context)
        return self.task

    async def CreateTaskPushNotificationConfig(self, request, context):
        await self._record('CreateTaskPushNotificationConfig', request, context)
        return a2a_pb2.TaskPushNotificationConfig(
            id='c1', task_id=request.task_id, url=request.url
        )

    async def GetTaskPushNotificationConfig(self, request, context):
        await self._record('GetTaskPushNotificationConfig', request, context)
        return a2a_pb2.TaskPushNotificationConfig(id=request.id, task_id=request.task_id)

    async def ListTaskPushNotificationConfigs(self, request, context):
        await self._record('ListTaskPushNotificationConfigs', request, context)
        return a2a_pb2.ListTaskPushNotificationConfigsResponse()

    async def DeleteTaskPushNotificationConfig(self, request, context):
        await self._record('DeleteTaskPushNotificationConfig', request, context)
        from google.protobuf import empty_pb2

        return empty_pb2.Empty()

    async def GetExtendedAgentCard(self, request, context):
        await self._record('GetExtendedAgentCard', request, context)
        return a2a_pb2.AgentCard(name='extended-agent')

    async def SendStreamingMessage(self, request, context):
        await self._record('SendStreamingMessage', request, context)
        for index in range(self.stream_events):
            yield a2a_pb2.StreamResponse(
                task=a2a_pb2.Task(id=f't{index}', context_id='c1')
            )

    async def SubscribeToTask(self, request, context):
        await self._record('SubscribeToTask', request, context)
        yield a2a_pb2.StreamResponse(task=self.task)


@contextlib.contextmanager
def serving(agent: FakeAgent) -> Iterator[str]:
    """Run ``agent`` on an ephemeral port for the duration of the block."""
    loop = asyncio.new_event_loop()

    async def start():
        server = grpc.aio.server()
        a2a_pb2_grpc.add_A2AServiceServicer_to_server(agent, server)
        port = server.add_insecure_port('127.0.0.1:0')
        await server.start()
        return server, port

    server, port = loop.run_until_complete(start())
    try:
        yield f'127.0.0.1:{port}', loop
    finally:
        loop.run_until_complete(server.stop(None))
        loop.close()


def call(agent: FakeAgent, coro_factory):
    """Run one dispatcher coroutine against ``agent``."""
    with serving(agent) as (target, loop):
        async def run():
            dispatcher = GrpcDispatcher(target)
            try:
                return await coro_factory(dispatcher)
            finally:
                await dispatcher.aclose()

        return loop.run_until_complete(run())


class TestDispatch:
    def test_binding_is_grpc(self):
        assert GrpcDispatcher('x:1').binding is TransportBinding.GRPC

    def test_unary_call_reaches_the_right_rpc(self):
        agent = FakeAgent()
        call(agent, lambda d: d.dispatch(Operation.GET_TASK, {'id': 't1'}))
        assert [name for name, _, _ in agent.calls] == ['GetTask']
        assert agent.calls[0][1].id == 't1'

    def test_response_is_protojson_with_camel_case_and_enum_names(self):
        """What the corpus asserts (`state: TASK_STATE_COMPLETED`), and what
        the two HTTP bindings put on the wire."""
        response = call(
            FakeAgent(), lambda d: d.dispatch(Operation.GET_TASK, {'id': 't1'})
        )
        assert response.ok
        assert response.payload['contextId'] == 'c1'
        assert response.payload['status']['state'] == 'TASK_STATE_COMPLETED'

    def test_success_derives_http_200(self):
        """gRPC has no HTTP status, but 86 binding-agnostic corpus tests
        assert `status: 200` to mean "it succeeded"."""
        response = call(
            FakeAgent(), lambda d: d.dispatch(Operation.GET_TASK, {'id': 't1'})
        )
        assert response.status == 200

    def test_params_are_adapted_before_the_request_is_built(self):
        """`taskId` is a Message field; SendMessageRequest has no such field,
        so an unadapted call would not even serialize."""
        agent = FakeAgent()
        call(
            agent,
            lambda d: d.dispatch(
                Operation.SEND_MESSAGE,
                {'taskId': 't9', 'message': {'role': 'ROLE_USER'}},
            ),
        )
        request = agent.calls[0][1]
        assert request.message.task_id == 't9'

    def test_nested_push_config_is_flattened_onto_the_request(self):
        agent = FakeAgent()
        call(
            agent,
            lambda d: d.dispatch(
                Operation.CREATE_PUSH_CONFIG,
                {'taskId': 't1', 'pushNotificationConfig': {'url': 'http://cb'}},
            ),
        )
        request = agent.calls[0][1]
        assert request.task_id == 't1'
        assert request.url == 'http://cb'

    def test_camel_case_params_reach_snake_case_proto_fields(self):
        agent = FakeAgent()
        call(
            agent,
            lambda d: d.dispatch(Operation.GET_TASK, {'id': 't1', 'historyLength': 3}),
        )
        assert agent.calls[0][1].history_length == 3

    def test_headers_travel_as_lowercased_metadata(self):
        """gRPC rejects uppercase metadata keys; ACTS writes HTTP-style."""
        agent = FakeAgent()
        call(
            agent,
            lambda d: d.dispatch(
                Operation.GET_TASK, {'id': 't1'}, headers={'A2A-Version': '1.0'}
            ),
        )
        metadata = dict(agent.calls[0][2])
        assert metadata['a2a-version'] == '1.0'

    def test_delete_returns_empty_without_error(self):
        response = call(
            FakeAgent(),
            lambda d: d.dispatch(
                Operation.DELETE_PUSH_CONFIG, {'taskId': 't1', 'id': 'c1'}
            ),
        )
        assert response.ok
        assert response.payload == {}

    def test_params_that_do_not_fit_the_message_fail_clearly(self):
        with pytest.raises(DispatchError, match='do not fit GetTaskRequest'):
            call(
                FakeAgent(),
                lambda d: d.dispatch(Operation.GET_TASK, {'nonsuchfield': 1}),
            )

    def test_streaming_operation_is_rejected_by_dispatch(self):
        with pytest.raises(DispatchError, match='use stream'):
            call(
                FakeAgent(),
                lambda d: d.dispatch(Operation.SEND_STREAMING_MESSAGE, {'message': {}}),
            )


class TestErrors:
    def test_error_name_comes_from_error_info_in_the_trailers(self):
        agent = FakeAgent()
        agent.fail_with['GetTask'] = (
            grpc.StatusCode.NOT_FOUND,
            'TASK_NOT_FOUND',
            'no such task',
        )
        response = call(agent, lambda d: d.dispatch(Operation.GET_TASK, {'id': 'x'}))
        assert not response.ok
        assert response.error.error_type is ErrorType.TASK_NOT_FOUND
        assert response.error.reason == 'TASK_NOT_FOUND'
        assert response.error.status == 'NOT_FOUND'
        assert 'no such task' in response.error.message

    def test_status_is_transcoded_to_http(self):
        agent = FakeAgent()
        agent.fail_with['GetTask'] = (
            grpc.StatusCode.NOT_FOUND,
            'TASK_NOT_FOUND',
            'gone',
        )
        response = call(agent, lambda d: d.dispatch(Operation.GET_TASK, {'id': 'x'}))
        assert response.status == 404

    def test_six_errors_share_failed_precondition_so_reason_decides(self):
        """The gRPC status cannot identify the error on its own."""
        agent = FakeAgent()
        agent.fail_with['CancelTask'] = (
            grpc.StatusCode.FAILED_PRECONDITION,
            'TASK_NOT_CANCELABLE',
            'terminal',
        )
        response = call(agent, lambda d: d.dispatch(Operation.CANCEL_TASK, {'id': 'x'}))
        assert response.error.error_type is ErrorType.TASK_NOT_CANCELABLE
        assert response.error.status == 'FAILED_PRECONDITION'
        assert response.status == 400

    def test_error_without_error_info_is_reported_unnamed(self):
        """A SUT omitting the mandated ErrorInfo is a finding, not something
        to guess from the status."""
        agent = FakeAgent()
        agent.fail_with['GetTask'] = (grpc.StatusCode.NOT_FOUND, None, 'bare')
        response = call(agent, lambda d: d.dispatch(Operation.GET_TASK, {'id': 'x'}))
        assert not response.ok
        assert response.error.error_type is None
        assert response.error.status == 'NOT_FOUND'

    def test_unknown_reason_is_reported_unnamed(self):
        agent = FakeAgent()
        agent.fail_with['GetTask'] = (
            grpc.StatusCode.INTERNAL,
            'SOMETHING_ELSE',
            'odd',
        )
        response = call(agent, lambda d: d.dispatch(Operation.GET_TASK, {'id': 'x'}))
        assert response.error.reason == 'SOMETHING_ELSE'
        assert response.error.error_type is None

    def test_an_rpc_error_is_a_result_not_a_raised_exception(self):
        """The runner reports it as a test outcome, like the HTTP bindings."""
        agent = FakeAgent()
        agent.fail_with['GetTask'] = (
            grpc.StatusCode.NOT_FOUND,
            'TASK_NOT_FOUND',
            'x',
        )
        response = call(agent, lambda d: d.dispatch(Operation.GET_TASK, {'id': 'x'}))
        assert response.error is not None


class TestStreaming:
    @staticmethod
    def _collect(agent, operation, params):
        async def factory(dispatcher):
            return [e async for e in dispatcher.stream(operation, params)]

        return call(agent, factory)

    def test_events_arrive_in_order(self):
        agent = FakeAgent()
        agent.stream_events = 3
        events = self._collect(
            agent, Operation.SEND_STREAMING_MESSAGE, {'message': {'role': 'ROLE_USER'}}
        )
        assert [e.index for e in events] == [0, 1, 2]
        assert [e.data['task']['id'] for e in events] == ['t0', 't1', 't2']

    def test_events_have_no_raw_text_on_this_binding(self):
        """They were never text — the field is honest about that."""
        agent = FakeAgent()
        agent.stream_events = 1
        events = self._collect(
            agent, Operation.SEND_STREAMING_MESSAGE, {'message': {}}
        )
        assert events[0].raw is None

    def test_subscribe_streams(self):
        agent = FakeAgent()
        events = self._collect(agent, Operation.SUBSCRIBE_TO_TASK, {'id': 't1'})
        assert [name for name, _, _ in agent.calls] == ['SubscribeToTask']
        # A stream event is a StreamResponse, so the task sits under `task`.
        assert events[0].data['task']['status']['state'] == 'TASK_STATE_COMPLETED'

    def test_non_streaming_operation_is_rejected(self):
        with pytest.raises(DispatchError, match='not a streaming operation'):
            self._collect(FakeAgent(), Operation.GET_TASK, {'id': 't1'})


class TestRawIsUnsupported:
    def test_dispatch_raw_refuses_with_an_explanation(self):
        """There is nothing below protobuf for a `raw` block to describe,
        which is why §4.4 makes an all-raw test name its transport."""
        raw = RawBlock(method=HttpMethod.POST, path='/', body={})
        with pytest.raises(UnsupportedByBinding, match='no raw-request form'):
            call(FakeAgent(), lambda d: d.dispatch_raw(raw))

    def test_it_is_a_dispatch_error_subclass(self):
        assert issubclass(UnsupportedByBinding, DispatchError)


class TestStreamRawIsRefused:
    """gRPC has no raw-request form, streaming or otherwise (ACTS §4.4)."""

    def test_stream_raw_raises_unsupported_by_binding(self):
        async def run():
            dispatcher = GrpcDispatcher('localhost:1')
            raw = RawBlock(method=HttpMethod.POST, path='/')
            return [event async for event in dispatcher.stream_raw(raw)]

        with pytest.raises(UnsupportedByBinding, match='raw-request form'):
            asyncio.run(run())
