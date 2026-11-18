"""The gRPC adapter (A2A §10).

Driven against a real ``grpc.aio`` server running an in-process fake servicer
on an ephemeral port. A mock channel would let a wrong method path or a
mis-typed request message pass unnoticed, which is most of what can actually
go wrong here — so the tests pay for a real server.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any, Iterator

import grpc
import httpx
import pytest
from google.protobuf import unknown_fields
from google.protobuf.any_pb2 import Any as ProtoAny
from google.rpc import error_details_pb2, status_pb2

from pyproto import a2a_pb2, a2a_pb2_grpc

from test_suite.acts.dispatcher import (
    DispatchError,
    GrpcDispatcher,
    UnsupportedByBinding,
)
from test_suite.acts import load_suite
from test_suite.acts.dispatcher.base import (
    ExchangeEnded,
    MalformedResponse,
    StreamNotOpened,
)
from test_suite.acts.dispatcher.grpc import STATUS_DETAILS_KEY, _to_dict
from test_suite.acts.dispatcher.params import stream_request
from test_suite.acts.runner import Outcome, Runner
from test_suite.acts.schema import (
    ErrorType,
    HttpMethod,
    Operation,
    RawBlock,
    RunnerRequirement,
    TransportBinding,
)
from test_suite.acts.wire_map import GRPC_SERVICE, binding_for_error


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


def replying_http(payload: Any, *, status: int = 200):
    """An HTTP handler for the agent card, which never travels over gRPC."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        return httpx.Response(status, json=payload)

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


def fetch_card(handler):
    """Fetch the card with the HTTP side mocked and no gRPC server needed."""

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            dispatcher = GrpcDispatcher(
                '127.0.0.1:1',
                agent_card_url='http://sut.test',
                http_client=client,
            )
            try:
                return await dispatcher.dispatch(Operation.GET_AGENT_CARD)
            finally:
                await dispatcher.aclose()

    return asyncio.run(run())


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


class TestProtoJsonProjection:
    """What survives the ProtoJSON projection, and what must not.

    proto3 gives an empty scalar no presence, so the encoder's choice about
    defaults decides whether an assertion can see the field at all. The corpus
    asserts in both directions — `nextPageToken` must be *there* and empty
    (A2A §3.1.4), `history` and `taskId` must be *absent* — so neither
    "drop every default" nor "print every default" satisfies it.
    """

    def test_a_required_empty_scalar_is_present(self):
        """§3.1.4: `nextPageToken` is always present, empty on the last page."""
        out = _to_dict(a2a_pb2.ListTasksResponse(tasks=[], next_page_token=''))
        assert out['nextPageToken'] == ''

    def test_the_other_required_scalars_come_back_too(self):
        out = _to_dict(a2a_pb2.ListTasksResponse())
        assert out['pageSize'] == 0
        assert out['totalSize'] == 0
        assert out['tasks'] == []

    def test_an_optional_empty_repeated_field_stays_absent(self):
        """`Task.history` is not required, and `CORE-HIST-003` asserts absence."""
        out = _to_dict(a2a_pb2.Task(id='t1', status=a2a_pb2.TaskStatus()))
        assert 'history' not in out
        assert 'artifacts' not in out

    def test_an_optional_empty_scalar_stays_absent(self):
        """`Message.taskId`/`contextId` are not required; `DM-FMT-003` wants them gone."""
        out = _to_dict(
            a2a_pb2.Message(message_id='m1', role=a2a_pb2.ROLE_AGENT, parts=[])
        )
        assert 'taskId' not in out
        assert 'contextId' not in out

    def test_required_fields_are_restored_inside_nested_messages(self):
        """The walk descends: a Task nested in a response gets the same treatment."""
        out = _to_dict(
            a2a_pb2.ListTasksResponse(
                tasks=[a2a_pb2.Task(id='', status=a2a_pb2.TaskStatus())]
            )
        )
        assert out['tasks'][0]['id'] == ''
        assert 'history' not in out['tasks'][0]

    def test_a_set_value_is_untouched(self):
        out = _to_dict(a2a_pb2.ListTasksResponse(next_page_token='cursor-2'))
        assert out['nextPageToken'] == 'cursor-2'

    def test_well_known_types_keep_their_own_json_shape(self):
        """`Part.data` is a `google.protobuf.Value` — the walk must not enter it."""
        part = a2a_pb2.Part()
        part.data.struct_value['k'] = 'v'
        out = _to_dict(a2a_pb2.Message(message_id='m1', parts=[part]))
        assert out['parts'][0]['data'] == {'k': 'v'}

    def test_enums_are_names_and_fields_are_camel_case(self):
        out = _to_dict(
            a2a_pb2.Task(
                id='t1',
                context_id='c1',
                status=a2a_pb2.TaskStatus(state=a2a_pb2.TASK_STATE_COMPLETED),
            )
        )
        assert out['status']['state'] == 'TASK_STATE_COMPLETED'
        assert out['contextId'] == 'c1'


class TestDispatch:
    def test_binding_is_grpc(self):
        # Constructed inside a loop, like every other dispatcher here: the
        # constructor opens a `grpc.aio` channel, which binds to the running
        # loop and raises without one. Whether a loop happens to be current
        # otherwise depends on which test file ran first.
        async def binding():
            dispatcher = GrpcDispatcher('x:1')
            try:
                return dispatcher.binding
            finally:
                await dispatcher.aclose()

        assert asyncio.run(binding()) is TransportBinding.GRPC

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
        """gRPC has no HTTP status, but 59 binding-agnostic corpus tests
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


class TestRequestWritten:
    """`on_sent`: the caller is told when the request has been written out.

    The unary form of a gRPC call says nothing before the reply, so a call
    that asks is made in a form that does. The SUT must not be able to tell.
    """

    class Holding(FakeAgent):
        """Holds `GetTask` until released, and says when it arrived."""

        def __init__(self) -> None:
            super().__init__()
            self.arrived = asyncio.Event()
            self.release = asyncio.Event()

        async def GetTask(self, request, context):
            self.arrived.set()
            await self.release.wait()
            return await super().GetTask(request, context)

    def test_it_is_called_before_the_reply_arrives(self):
        agent = self.Holding()
        order: list[str] = []

        async def factory(dispatcher):
            call_ = asyncio.ensure_future(dispatcher.dispatch(
                Operation.GET_TASK, {'id': 't1'}, {'A2A-Version': '1.1'},
                on_sent=lambda: order.append('sent'),
            ))
            await asyncio.wait_for(agent.arrived.wait(), 10)
            # The agent has the request and is holding its reply.
            await asyncio.sleep(0.05)
            order.append('released')
            agent.release.set()
            response = await call_
            order.append('answered')
            return response

        response = call(agent, factory)
        assert order == ['sent', 'released', 'answered']
        assert response.status == 200
        assert response.payload['id'] == 't1'

    def test_the_sut_sees_the_same_call_either_way(self):
        agent = FakeAgent()

        async def factory(dispatcher):
            headers = {'A2A-Version': '1.1'}
            plain = await dispatcher.dispatch(Operation.GET_TASK, {'id': 't1'}, headers)
            asked = await dispatcher.dispatch(
                Operation.GET_TASK, {'id': 't1'}, headers, on_sent=lambda: None
            )
            return plain, asked

        plain, asked = call(agent, factory)
        assert asked == plain
        (name, request, metadata), (name2, request2, metadata2) = agent.calls
        assert (name, request) == (name2, request2)
        assert ('a2a-version', '1.1') in [tuple(m) for m in metadata2]

    def test_an_error_reads_the_same_either_way(self):
        agent = FakeAgent()
        agent.fail_with['GetTask'] = (
            grpc.StatusCode.NOT_FOUND, 'TASK_NOT_FOUND', 'no such task'
        )
        sent: list[bool] = []

        async def factory(dispatcher):
            plain = await dispatcher.dispatch(Operation.GET_TASK, {'id': 'x'})
            asked = await dispatcher.dispatch(
                Operation.GET_TASK, {'id': 'x'}, on_sent=lambda: sent.append(True)
            )
            return plain, asked

        plain, asked = call(agent, factory)
        for response in (plain, asked):
            assert response.status == 404
            assert response.error.error_type is ErrorType.TASK_NOT_FOUND
            assert response.error.status == 'NOT_FOUND'
            assert response.error.message == 'no such task'
        assert sent == [True]

    def test_an_empty_reply_is_still_a_reply(self):
        """`delete_push_config` answers with an empty message, which is not
        the same as ending the call without one."""
        async def factory(dispatcher):
            return await dispatcher.dispatch(
                Operation.DELETE_PUSH_CONFIG, {'taskId': 't1', 'id': 'c1'},
                on_sent=lambda: None,
            )

        response = call(FakeAgent(), factory)
        assert response.ok
        assert response.payload == {}

    @staticmethod
    def _with_get_task_served_by(handler, coro_factory, **dispatcher_kwargs):
        """Run against a server whose `GetTask` is ``handler``, which is
        given the form of the call and not only its one request."""
        loop = asyncio.new_event_loop()

        async def run():
            server = grpc.aio.server()
            server.add_generic_rpc_handlers((
                grpc.method_handlers_generic_handler(GRPC_SERVICE, {'GetTask': handler}),
            ))
            port = server.add_insecure_port('127.0.0.1:0')
            await server.start()
            dispatcher = GrpcDispatcher(f'127.0.0.1:{port}', **dispatcher_kwargs)
            try:
                return await coro_factory(dispatcher)
            finally:
                await dispatcher.aclose()
                await server.stop(None)

        try:
            return loop.run_until_complete(run())
        finally:
            loop.close()

    def test_the_client_side_is_closed_after_the_one_request(self):
        """A server may wait for that before it handles a unary call. This
        one does, and counts what it was sent."""
        async def read_to_the_end(requests, context):
            received = [request async for request in requests]
            return a2a_pb2.Task(id=f'{len(received)} request(s)')

        handler = grpc.stream_unary_rpc_method_handler(
            read_to_the_end,
            request_deserializer=a2a_pb2.GetTaskRequest.FromString,
            response_serializer=a2a_pb2.Task.SerializeToString,
        )
        response = self._with_get_task_served_by(
            handler,
            lambda d: d.dispatch(Operation.GET_TASK, {'id': 't1'}, on_sent=lambda: None),
            timeout=5,
        )
        assert response.ok, response.error
        assert response.payload['id'] == '1 request(s)'

    @pytest.mark.parametrize(
        ('replies', 'said'),
        [(0, 'without a response message'), (2, 'more than one response message')],
    )
    def test_a_unary_call_answered_with_another_number_of_messages(self, replies, said):
        async def answer(request, context):
            for _ in range(replies):
                yield a2a_pb2.Task(id='t1')

        handler = grpc.unary_stream_rpc_method_handler(
            answer,
            request_deserializer=a2a_pb2.GetTaskRequest.FromString,
            response_serializer=a2a_pb2.Task.SerializeToString,
        )
        with pytest.raises(MalformedResponse, match=said):
            self._with_get_task_served_by(
                handler,
                lambda d: d.dispatch(
                    Operation.GET_TASK, {'id': 't1'}, on_sent=lambda: None
                ),
            )

    def test_a_call_refused_before_the_request_is_written_reads_as_refused(self):
        """An agent may refuse a call on its headers alone: a method it does
        not serve, a credential it rejects. When that reaches the client
        before the request is written, the call is over and the write is
        refused. It is the agent's answer all the same, not a broken run."""
        sent: list[bool] = []

        def writing_only_after_the_refusal(open_call):
            def opened(*args, **kwargs):
                call = open_call(*args, **kwargs)
                write = call.write

                async def late(request):
                    while not call.done():
                        await asyncio.sleep(0.01)
                    await write(request)

                call.write = late
                return call
            return opened

        async def run():
            server = grpc.aio.server()  # Serves no method at all.
            port = server.add_insecure_port('127.0.0.1:0')
            await server.start()
            dispatcher = GrpcDispatcher(f'127.0.0.1:{port}', timeout=5)
            callable_for = dispatcher._callable
            dispatcher._callable = lambda operation, **form: (
                writing_only_after_the_refusal(callable_for(operation, **form))
            )
            try:
                return await dispatcher.dispatch(
                    Operation.GET_TASK, {'id': 't1'}, on_sent=lambda: sent.append(True)
                )
            finally:
                await dispatcher.aclose()
                await server.stop(None)

        response = asyncio.run(run())
        assert response.error.status == 'UNIMPLEMENTED'
        assert sent == []

    def test_an_agent_that_cannot_be_reached_is_not_reported_as_sent(self):
        sent: list[bool] = []

        async def run():
            dispatcher = GrpcDispatcher('127.0.0.1:1', timeout=5)
            try:
                return await dispatcher.dispatch(
                    Operation.GET_TASK, {'id': 't1'}, on_sent=lambda: sent.append(True)
                )
            finally:
                await dispatcher.aclose()

        response = asyncio.run(run())
        assert response.error.status == 'UNAVAILABLE'
        assert sent == []


class TestRequestFieldsFromA2A11:
    """The request messages gained fields in A2A 1.1. A test writes a 64-bit
    one as a number or, as a JSON binding carries it, as a string."""

    @pytest.mark.parametrize('generation', [5, '5'])
    def test_a_held_read_of_a_task(self, generation):
        agent = FakeAgent()
        call(agent, lambda d: d.dispatch(
            Operation.GET_TASK,
            {'id': 't1', 'currentGeneration': generation, 'timelineLength': 3},
        ))
        _, request, _ = agent.calls[0]
        assert request.current_generation == 5
        assert request.timeline_length == 3

    @pytest.mark.parametrize('generation', [7, '7'])
    def test_a_subscription_resumed_from_a_generation(self, generation):
        agent = FakeAgent()

        async def factory(dispatcher):
            return [
                e async for e in dispatcher.stream(
                    Operation.SUBSCRIBE_TO_TASK,
                    {'id': 't1', 'lastSeenGeneration': generation},
                )
            ]

        call(agent, factory)
        _, request, _ = agent.calls[0]
        assert request.HasField('last_seen_generation')
        assert request.last_seen_generation == 7

    def test_a_send_guarded_by_a_generation(self):
        agent = FakeAgent()
        call(agent, lambda d: d.dispatch(Operation.SEND_MESSAGE, {
            'message': {'role': 'ROLE_USER', 'messageId': 'm1', 'parts': [{'text': 'hi'}]},
            'configuration': {'ifGenerationMatch': '4'},
        }))
        _, request, _ = agent.calls[0]
        assert request.configuration.if_generation_match == 4

    def test_a_field_no_version_defines_is_still_refused(self):
        """The message is built strictly, so a misspelled field in a test is
        reported and not sent as nothing."""
        with pytest.raises(DispatchError, match='params do not fit GetTaskRequest'):
            call(FakeAgent(), lambda d: d.dispatch(
                Operation.GET_TASK, {'id': 't1', 'currentGenerations': 5}
            ))


class TestAgentCardOverHttp:
    """Fetched by the shared HTTP helper, not by a second copy of the GET."""

    def test_it_goes_to_the_well_known_path(self):
        handler = replying_http({'name': 'agent'})
        response = fetch_card(handler)
        assert handler.seen[0].url.path == '/.well-known/agent-card.json'
        assert response.payload == {'name': 'agent'}

    def test_a_missing_card_is_an_error_named_by_its_status(self):
        """Unlike the HTTP adapters, there is no envelope in the body to read:
        an HTTP reply carries no gRPC error."""
        response = fetch_card(replying_http('', status=404))
        assert response.status == 404
        assert response.error is not None
        assert response.error.message == 'HTTP 404'

    def test_the_http_client_does_not_follow_redirects(self):
        """Same reason as the HTTP adapters — a 3xx is a result to assert on."""

        async def check():
            dispatcher = GrpcDispatcher('127.0.0.1:1')
            try:
                return dispatcher._http.follow_redirects
            finally:
                await dispatcher.aclose()

        assert asyncio.run(check()) is False


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


# ---------------------------------------------------------------------------
# Live exchanges
# ---------------------------------------------------------------------------

#: The member of a `StreamRequest` that carries a message, by the name the
#: generated stub gives it.
LIVE_SEND = next(
    member.name
    for member in a2a_pb2.StreamRequest.DESCRIPTOR.oneofs_by_name['payload'].fields
    if member.message_type.name == 'SendMessageRequest'
)


def _said(message: a2a_pb2.Message) -> str:
    return ''.join(part.text for part in message.parts).strip()


class LiveAgent(a2a_pb2_grpc.A2AServiceServicer):
    """An agent that serves the live exchange over gRPC.

    It plays `tck-live-echo` and `tck-live-reject` on the exchange, and
    serves the two unary operations the live tests use beside it, with
    `tck-multi-turn` for a task outside the exchange.

    ``on_rejection`` says what it does with a later request it has to reject:
    `'reject'` reports it as a `StreamError` and keeps the exchange open,
    which is what A2A asks. `'close'` ends the exchange, and `'abort'` ends
    it with an error status. `'apply'` reports it and applies the request all
    the same.
    """

    def __init__(self, *, streaming: bool = True, on_rejection: str = 'reject') -> None:
        self.streaming = streaming
        self.on_rejection = on_rejection
        self.tasks: dict[str, a2a_pb2.Task] = {}
        #: Every request received on a live exchange, in order.
        self.received: list[a2a_pb2.StreamRequest] = []
        self.metadata: list[tuple[str, str]] = []
        #: The agent's side of an exchange is over: it ended the exchange, or
        #: the client did. A client that hangs up ends the agent's requests,
        #: or cancels the agent while it is sending an event.
        self.ended = asyncio.Event()

    # -- the task store ---------------------------------------------------------

    def _create(self) -> a2a_pb2.Task:
        task = a2a_pb2.Task(
            id=f'task-{len(self.tasks) + 1}', context_id='ctx-1', generation=1
        )
        task.status.state = a2a_pb2.TASK_STATE_SUBMITTED
        self.tasks[task.id] = task
        return task

    def _move(self, task: a2a_pb2.Task, state: int, text: str = '') -> a2a_pb2.StreamResponse:
        """Change the task's status, and return the event that reports it."""
        task.generation += 1
        task.status.Clear()
        task.status.state = state
        if text:
            task.status.message.message_id = f'{task.id}-{task.generation}'
            task.status.message.role = a2a_pb2.ROLE_AGENT
            task.status.message.parts.add(text=text)
        event = a2a_pb2.StreamResponse()
        event.status_update.task_id = task.id
        event.status_update.context_id = task.context_id
        event.status_update.status.CopyFrom(task.status)
        event.status_update.generation = task.generation
        return event

    async def _refuse(self, context, error: ErrorType, message: str) -> None:
        binding = binding_for_error(error)
        code = grpc.StatusCode[binding.grpc_status]
        context.set_trailing_metadata(error_trailers(code.value[0], binding.reason, message))
        await context.abort(code, message)

    # -- unary operations ----------------------------------------------------------

    async def SendMessage(self, request, context):
        message = request.message
        if message.task_id:
            task = self.tasks.get(message.task_id)
            if task is None:
                await self._refuse(context, ErrorType.TASK_NOT_FOUND, 'no such task')
            done = _said(message).lower() == 'done'
            self._move(
                task,
                a2a_pb2.TASK_STATE_COMPLETED if done else a2a_pb2.TASK_STATE_INPUT_REQUIRED,
            )
        else:
            task = self._create()
            self._move(task, a2a_pb2.TASK_STATE_WORKING)
            self._move(task, a2a_pb2.TASK_STATE_INPUT_REQUIRED)
        return a2a_pb2.SendMessageResponse(task=task)

    async def GetTask(self, request, context):
        task = self.tasks.get(request.id)
        if task is None:
            await self._refuse(context, ErrorType.TASK_NOT_FOUND, 'no such task')
        return task

    # -- the live exchange -----------------------------------------------------------

    async def SendLiveMessage(self, request_iterator, context):
        self.metadata = [
            (key, value) for key, value in context.invocation_metadata()
            if isinstance(value, str)
        ]
        if not self.streaming:
            await self._refuse(
                context, ErrorType.UNSUPPORTED_OPERATION, 'streaming is not supported'
            )
        try:
            async for event in self._exchange(request_iterator, context):
                yield event
        finally:
            self.ended.set()

    async def _exchange(self, request_iterator, context):
        """The events of one live exchange, in answer to its requests."""
        opening = await request_iterator.__anext__()
        self.received.append(opening)
        kind = opening.WhichOneof('payload')

        if kind == 'subscribe':
            task = self.tasks.get(opening.subscribe.id)
            if task is None:
                await self._refuse(context, ErrorType.TASK_NOT_FOUND, 'no such task')
            yield a2a_pb2.StreamResponse(task=task)
            async for later in request_iterator:
                self.received.append(later)
            return

        if kind != LIVE_SEND:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, 'not a request this agent knows')

        task = self._create()
        declines = _said(getattr(opening, LIVE_SEND).message).startswith('tck-live-reject')
        yield a2a_pb2.StreamResponse(task=task)
        yield self._move(task, a2a_pb2.TASK_STATE_WORKING)

        answered = 0
        async for later in request_iterator:
            self.received.append(later)
            rejection = self._rejection_of(later, task, declines)
            if rejection is not None:
                if self.on_rejection == 'close':
                    return
                if self.on_rejection == 'abort':
                    await context.abort(
                        grpc.StatusCode.INVALID_ARGUMENT, 'not a request for this exchange'
                    )
                yield rejection
                if self.on_rejection != 'apply':
                    continue
                if later.WhichOneof('payload') != LIVE_SEND:
                    continue
                named = self.tasks.get(getattr(later, LIVE_SEND).message.task_id)
                if named is not None and named is not task:
                    self._move(named, a2a_pb2.TASK_STATE_COMPLETED)
                    continue

            if _said(getattr(later, LIVE_SEND).message).lower() == 'done':
                yield self._move(task, a2a_pb2.TASK_STATE_COMPLETED)
                return
            answered += 1
            yield self._move(task, a2a_pb2.TASK_STATE_WORKING, f'live-echo:{answered}')

    def _rejection_of(
        self, request: a2a_pb2.StreamRequest, task: a2a_pb2.Task, declines: bool
    ) -> a2a_pb2.StreamResponse | None:
        """The `StreamError` for a later request this agent does not take."""
        rejection = a2a_pb2.StreamResponse()
        rejection.error.task_id = task.id
        kind = request.WhichOneof('payload')
        if kind is None:
            rejection.error.code = 'UNIMPLEMENTED'
            rejection.error.message = 'a request of a kind this agent does not know'
            return rejection
        if kind == 'subscribe':
            rejection.error.code = 'FAILED_PRECONDITION'
            rejection.error.message = 'subscribe is only valid as the first request'
            return rejection
        message = getattr(request, LIVE_SEND).message
        rejection.error.reference_id = message.message_id
        if message.task_id != task.id:
            rejection.error.code = 'INVALID_ARGUMENT'
            rejection.error.message = 'the message names another task'
            return rejection
        if declines and _said(message).startswith('reject'):
            rejection.error.code = 'UNAVAILABLE'
            rejection.error.message = 'the agent declined this message, as asked'
            return rejection
        return None


def live(agent: LiveAgent, coro_factory):
    """Run one coroutine against ``agent``, given a dispatcher for it."""
    return call(agent, coro_factory)


def message(text: str, **fields: Any) -> dict:
    """The JSON form of a `StreamRequest` that carries one message."""
    return stream_request('send_message', {
        'message': {
            'messageId': fields.pop('message_id', f'm-{text}'),
            'role': 'ROLE_USER',
            'parts': [{'text': text}],
            **fields,
        },
    })


class TestLiveExchange:
    """`SendLiveMessage` over gRPC: one bidirectional stream. Every request
    is a message on it, and none has a response of its own."""

    def test_requests_are_sent_while_the_downlink_is_read(self):
        """The second and third request are written only after events that
        the agent sends in answer to the ones before them have been read."""
        agent = LiveAgent()

        async def exchange_of(dispatcher):
            exchange = dispatcher.live(message('tck-live-echo'), {'A2A-Version': '1.1'})
            events = []
            async for event in exchange.events():
                events.append(event.data)
                if len(events) == 1:
                    task_id = event.data['task']['id']
                elif len(events) == 2:
                    assert await exchange.send(message('first', taskId=task_id)) is None
                elif len(events) == 3:
                    assert await exchange.send(message('done', taskId=task_id)) is None
            return events

        events = live(agent, exchange_of)
        assert [next(iter(event)) for event in events] == [
            'task', 'statusUpdate', 'statusUpdate', 'statusUpdate',
        ]
        assert events[2]['statusUpdate']['status']['message']['parts'] == [
            {'text': 'live-echo:1'}
        ]
        assert events[3]['statusUpdate']['status']['state'] == 'TASK_STATE_COMPLETED'
        assert [_said(getattr(r, LIVE_SEND).message) for r in agent.received] == [
            'tck-live-echo', 'first', 'done',
        ]
        assert ('a2a-version', '1.1') in agent.metadata

    def test_a_request_of_a_kind_no_version_defines_reaches_the_agent_unnamed(self):
        """The agent gets a `StreamRequest` with no payload it can name, and a
        field its proto does not have: what a request from a later minor
        version looks like to it."""
        agent = LiveAgent()

        async def exchange_of(dispatcher):
            exchange = dispatcher.live(message('tck-live-echo'))
            events = []
            async for event in exchange.events():
                events.append(event.data)
                if len(events) == 2:
                    await exchange.send(stream_request('unrecognized'))
                elif len(events) == 3:
                    await exchange.send(message('done', taskId=events[0]['task']['id']))
            return events

        events = live(agent, exchange_of)
        unknown = agent.received[1]
        assert unknown.WhichOneof('payload') is None
        assert [f.field_number for f in unknown_fields.UnknownFieldSet(unknown)] == [15999]
        assert events[2] == {'error': {
            'taskId': events[0]['task']['id'],
            'code': 'UNIMPLEMENTED',
            'message': 'a request of a kind this agent does not know',
        }}

    def test_an_agent_that_refuses_to_open_the_exchange(self):
        async def exchange_of(dispatcher):
            exchange = dispatcher.live(stream_request('subscribe', {'id': 'no-such-task'}))
            return [event async for event in exchange.events()]

        with pytest.raises(StreamNotOpened) as refused:
            live(LiveAgent(), exchange_of)
        error = refused.value.response.error
        assert error.error_type is ErrorType.TASK_NOT_FOUND
        assert error.status == 'NOT_FOUND'
        assert 'refused the live exchange' in str(refused.value)

    def test_closing_the_downlink_ends_the_exchange_for_the_agent(self):
        """The sending side is kept open for as long as the downlink is
        read, so the agent is still waiting for requests. Closing the
        downlink cancels the call, which is how the agent learns that the
        client is gone."""
        agent = LiveAgent()

        async def exchange_of(dispatcher):
            exchange = dispatcher.live(message('tck-live-echo'))
            downlink = exchange.events()
            first = await anext(downlink)
            await downlink.aclose()
            await asyncio.wait_for(agent.ended.wait(), 10)
            return first.data

        assert 'task' in live(agent, exchange_of)

    def test_a_request_after_the_agent_has_ended_the_exchange(self):
        agent = LiveAgent()

        async def exchange_of(dispatcher):
            exchange = dispatcher.live(message('tck-live-echo'))
            events = []
            async for event in exchange.events():
                events.append(event.data)
                if len(events) == 2:
                    await exchange.send(message('done', taskId=events[0]['task']['id']))
            # The agent completed the task and closed the stream.
            with pytest.raises(ExchangeEnded):
                await exchange.send(message('too late', taskId=events[0]['task']['id']))
            await exchange.aclose()

        live(agent, exchange_of)

    def test_a_request_before_the_exchange_is_open(self):
        async def exchange_of(dispatcher):
            exchange = dispatcher.live(message('tck-live-echo'))
            with pytest.raises(DispatchError, match='not open yet'):
                await exchange.send(message('early'))

        live(LiveAgent(), exchange_of)

    def test_a_request_that_does_not_fit_the_message_is_refused_here(self):
        async def exchange_of(dispatcher):
            exchange = dispatcher.live({'sendMesage': {}})
            return [event async for event in exchange.events()]

        with pytest.raises(DispatchError, match='does not fit StreamRequest'):
            live(LiveAgent(), exchange_of)


class TestTheLiveCorpusTestsOverGrpc:
    """The corpus tests of the live exchange, run by the runner over gRPC.

    Four of them are for gRPC only, because they send a later request that
    an HTTP binding has no way to relate to its exchange. This is where
    those run against an agent that does what they ask, and against agents
    that do not.
    """

    CORPUS = Path(__file__).resolve().parent.parent / 'scenarios' / 'acts' / 'suite.acts.yaml'

    def run(self, test_id: str, agent: LiveAgent):
        test = load_suite(self.CORPUS).by_id(test_id).test

        def run_with(dispatcher):
            runner = Runner(
                dispatcher,
                spec_version='1.1',
                agent_card={'capabilities': {'streaming': agent.streaming}},
                capabilities=[RunnerRequirement.LIVE_STREAM],
            )
            return runner.run_test(test)

        return live(agent, run_with)

    @pytest.mark.parametrize('test_id', [
        'STREAM-LIVE-001', 'STREAM-LIVE-002',
        # For gRPC only.
        'STREAM-LIVE-005', 'STREAM-LIVE-006', 'STREAM-LIVE-007', 'STREAM-LIVE-008',
        'STREAM-LIVE-010',
    ])
    def test_an_agent_that_conforms_passes(self, test_id):
        result = self.run(test_id, LiveAgent())
        assert result.result is Outcome.PASS, result.failure
        assert all(step.checks > 0 for step in result.steps)

    def test_an_agent_without_streaming_refuses_the_exchange(self):
        result = self.run('STREAM-LIVE-009', LiveAgent(streaming=False))
        assert result.result is Outcome.PASS, result.failure

    def test_an_agent_that_opens_an_exchange_it_should_refuse_is_caught(self):
        """The card says no streaming, and the agent serves the exchange."""
        agent = LiveAgent()
        test = load_suite(self.CORPUS).by_id('STREAM-LIVE-009').test

        def run_with(dispatcher):
            runner = Runner(
                dispatcher, spec_version='1.1',
                agent_card={'capabilities': {'streaming': False}},
                capabilities=[RunnerRequirement.LIVE_STREAM],
            )
            return runner.run_test(test)

        result = live(agent, run_with)
        # Its first event shows the exchange opened. The agent keeps it open,
        # and the runner does not wait for more.
        assert result.result is Outcome.FAIL
        assert result.failure.message == (
            'expected an error, but the stream opened and produced 1 event(s)'
        )

    @pytest.mark.parametrize('test_id', ['STREAM-LIVE-005', 'STREAM-LIVE-007'])
    def test_an_agent_that_ends_the_exchange_at_a_rejection_is_caught(self, test_id):
        """The request after the rejected one is held for the `StreamError`,
        which never comes: the agent closed the downlink in its place."""
        result = self.run(test_id, LiveAgent(on_rejection='close'))
        assert result.result is Outcome.FAIL
        assert result.failure.step_id == 'exchange'
        assert result.failure.assertion_path == "uplink['after-rejection']"
        assert result.failure.message == (
            "uplink request 'after-rejection' was never sent: no downlink event "
            "matched its `after_event` (2 event(s) received)"
        )

    @pytest.mark.parametrize('test_id', ['STREAM-LIVE-005', 'STREAM-LIVE-007'])
    def test_an_agent_that_fails_the_exchange_at_a_rejection_is_caught(self, test_id):
        """It answers the rejected request by ending the call with an error
        status. That is the agent's doing and not a broken run, so it fails
        the test and is not reported as an error."""
        result = self.run(test_id, LiveAgent(on_rejection='abort'))
        assert result.result is Outcome.FAIL
        assert result.failure.step_id == 'exchange'
        assert result.failure.message == (
            'the SUT ended the live exchange after 2 event(s) with '
            'INVALID_ARGUMENT: not a request for this exchange'
        )

    def test_a_connection_lost_mid_exchange_is_not_held_against_the_agent(self):
        """It reads as `UNAVAILABLE`. The run broke; the agent said nothing."""
        class Unreachable(LiveAgent):
            async def _exchange(self, request_iterator, context):
                await request_iterator.__anext__()
                yield a2a_pb2.StreamResponse(task=self._create())
                await context.abort(grpc.StatusCode.UNAVAILABLE, 'connection reset')

        async def exchange_of(dispatcher):
            exchange = dispatcher.live(message('tck-live-echo'), {'A2A-Version': '1.1'})
            with pytest.raises(DispatchError) as raised:
                [event async for event in exchange.events()]
            return raised.value

        error = live(Unreachable(), exchange_of)
        assert not isinstance(error, MalformedResponse)
        assert str(error) == 'the live exchange was lost after 1 event(s): connection reset'

    def test_a_request_written_as_the_agent_fails_the_exchange_was_not_sent(self):
        """The write is refused, with the status the agent ended the call
        with or without it, depending on what reached the client first.
        Either way the exchange was over, which is what the caller is told."""
        class FailsAfterItsFirstEvent(LiveAgent):
            async def _exchange(self, request_iterator, context):
                await request_iterator.__anext__()
                yield a2a_pb2.StreamResponse(task=self._create())
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, 'gone')

        async def exchange_of(dispatcher):
            exchange = dispatcher.live(message('tck-live-echo'), {'A2A-Version': '1.1'})
            downlink = exchange.events()
            await anext(downlink)
            try:
                with pytest.raises(ExchangeEnded, match='the SUT had ended the exchange'):
                    for _ in range(2000):
                        await exchange.send(message('more'))
            finally:
                await downlink.aclose()

        live(FailsAfterItsFirstEvent(), exchange_of)

    def test_an_agent_that_applies_a_message_for_another_task_is_caught(self):
        """It reports the rejection, and completes the other task all the
        same. The exchange itself looks right; the step after it reads the
        other task."""
        result = self.run('STREAM-LIVE-006', LiveAgent(on_rejection='apply'))
        assert result.result is Outcome.FAIL
        assert result.failure.step_id == 'other-unchanged'
        assert [(step.id, step.result) for step in result.steps] == [
            ('other', Outcome.PASS), ('exchange', Outcome.PASS),
            ('other-unchanged', Outcome.FAIL),
        ]
