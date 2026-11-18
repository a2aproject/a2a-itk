"""The HTTP+JSON/REST adapter (A2A §11).

The interesting behaviour here is addressing — params split between the path,
the query string and the body — and error identification, which must come from
``ErrorInfo.reason`` rather than the HTTP status because the status is not
injective (A2A §11.6).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import httpx
import pytest

from test_suite.acts.dispatcher import DispatchError, RestDispatcher
from test_suite.acts.dispatcher.base import MalformedResponse, StreamNotOpened
from test_suite.acts.schema import (
    ErrorType,
    HttpMethod,
    Operation,
    RawBlock,
    TransportBinding,
)


def make(
    handler: Callable[[httpx.Request], httpx.Response],
    **kwargs: Any,
) -> RestDispatcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RestDispatcher('http://sut.test', client=client, **kwargs)


def replying(payload: Any, *, status: int = 200, headers: dict | None = None):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if isinstance(payload, str):
            return httpx.Response(status, text=payload, headers=headers)
        return httpx.Response(status, json=payload, headers=headers)

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


def status_error(
    code: int,
    status: str,
    message: str = 'boom',
    reason: str | None = None,
) -> dict:
    """A ``google.rpc.Status`` error document (A2A §11.6)."""
    details = []
    if reason:
        details.append(
            {
                '@type': 'type.googleapis.com/google.rpc.ErrorInfo',
                'reason': reason,
                'domain': 'a2a-protocol.org',
            }
        )
    return {
        'error': {
            'code': code,
            'status': status,
            'message': message,
            'details': details,
        }
    }


class TestAddressing:
    def test_binding_is_rest(self):
        assert RestDispatcher('http://x').binding is TransportBinding.REST

    def test_path_params_go_in_the_path(self):
        handler = replying({'id': 't1'})
        asyncio.run(make(handler).dispatch(Operation.GET_TASK, {'id': 't1'}))
        request = handler.seen[0]
        assert request.method == 'GET'
        assert request.url.path == '/tasks/t1'

    def test_remaining_params_of_a_get_go_in_the_query(self):
        handler = replying({'id': 't1'})
        asyncio.run(
            make(handler).dispatch(
                Operation.GET_TASK, {'id': 't1', 'historyLength': 0}
            )
        )
        request = handler.seen[0]
        assert request.url.path == '/tasks/t1'
        assert request.url.params['historyLength'] == '0'

    def test_remaining_params_of_a_post_go_in_the_body(self):
        handler = replying({'task': {}})
        asyncio.run(
            make(handler).dispatch(
                Operation.SEND_MESSAGE, {'message': {'role': 'ROLE_USER'}}
            )
        )
        request = handler.seen[0]
        assert request.url.path == '/message:send'
        assert json.loads(request.content)['message'] == {'role': 'ROLE_USER'}

    def test_booleans_are_rendered_the_way_http_spells_them(self):
        handler = replying({'tasks': []})
        asyncio.run(
            make(handler).dispatch(Operation.LIST_TASKS, {'includeArtifacts': True})
        )
        assert handler.seen[0].url.params['includeArtifacts'] == 'true'

    def test_push_config_paths_use_the_a2a_collection_name(self):
        """ACTS §4.1 writes `/pushNotifications`, which 404s."""
        handler = replying({'id': 'c1'})
        asyncio.run(
            make(handler).dispatch(
                Operation.GET_PUSH_CONFIG, {'taskId': 't1', 'id': 'c1'}
            )
        )
        assert handler.seen[0].url.path == '/tasks/t1/pushNotificationConfigs/c1'

    def test_delete_addresses_by_path_and_sends_no_body(self):
        handler = replying('', status=204)
        asyncio.run(
            make(handler).dispatch(
                Operation.DELETE_PUSH_CONFIG, {'taskId': 't1', 'id': 'c1'}
            )
        )
        request = handler.seen[0]
        assert request.method == 'DELETE'
        assert request.url.path == '/tasks/t1/pushNotificationConfigs/c1'
        assert not request.content

    def test_create_push_config_splits_path_and_flattened_body(self):
        """`taskId` addresses the resource; the nested config flattens into
        the body, which is the shape `TaskPushNotificationConfig` has."""
        handler = replying({'id': 'c1', 'url': 'http://cb'})
        asyncio.run(
            make(handler).dispatch(
                Operation.CREATE_PUSH_CONFIG,
                {'taskId': 't1', 'pushNotificationConfig': {'url': 'http://cb'}},
            )
        )
        request = handler.seen[0]
        assert request.url.path == '/tasks/t1/pushNotificationConfigs'
        body = json.loads(request.content)
        assert body == {'url': 'http://cb'}
        assert 'pushNotificationConfig' not in body

    def test_missing_path_param_fails_loudly(self):
        handler = replying({})
        with pytest.raises(KeyError, match='taskId'):
            asyncio.run(
                make(handler).dispatch(Operation.GET_PUSH_CONFIG, {'id': 'c1'})
            )

    def test_sends_the_rest_content_type(self):
        handler = replying({'task': {}})
        asyncio.run(make(handler).dispatch(Operation.SEND_MESSAGE, {'message': {}}))
        assert handler.seen[0].headers['content-type'] == 'application/a2a+json'


class TestResponseReading:
    def test_payload_is_the_body_with_no_envelope_to_unwrap(self):
        """Which is why `expect.body` reads the same here as over JSON-RPC."""
        handler = replying({'id': 't1', 'status': {'state': 'TASK_STATE_COMPLETED'}})
        response = asyncio.run(make(handler).dispatch(Operation.GET_TASK, {'id': 't1'}))
        assert response.payload['status']['state'] == 'TASK_STATE_COMPLETED'
        assert response.ok

    def test_status_is_the_http_status(self):
        handler = replying({'id': 't1'}, status=200)
        response = asyncio.run(make(handler).dispatch(Operation.GET_TASK, {'id': 't1'}))
        assert response.status == 200

    def test_empty_body_is_not_an_error(self):
        handler = replying('', status=204)
        response = asyncio.run(
            make(handler).dispatch(
                Operation.DELETE_PUSH_CONFIG, {'taskId': 't1', 'id': 'c1'}
            )
        )
        assert response.ok
        assert response.payload is None


class TestErrorReading:
    def test_error_name_comes_from_error_info_reason(self):
        handler = replying(
            status_error(404, 'NOT_FOUND', reason='TASK_NOT_FOUND'), status=404
        )
        response = asyncio.run(make(handler).dispatch(Operation.GET_TASK, {'id': 'x'}))
        assert response.error.error_type is ErrorType.TASK_NOT_FOUND
        assert response.error.reason == 'TASK_NOT_FOUND'
        assert response.error.status == 'NOT_FOUND'

    def test_two_errors_sharing_a_status_stay_distinguishable(self):
        """A2A §11.6's whole reason for mandating ErrorInfo: both of these
        are HTTP 400, so only the reason tells them apart."""
        cancelable = replying(
            status_error(400, 'FAILED_PRECONDITION', reason='TASK_NOT_CANCELABLE'),
            status=400,
        )
        push = replying(
            status_error(
                400, 'FAILED_PRECONDITION', reason='PUSH_NOTIFICATION_NOT_SUPPORTED'
            ),
            status=400,
        )
        first = asyncio.run(
            make(cancelable).dispatch(Operation.CANCEL_TASK, {'id': 'x'})
        )
        second = asyncio.run(
            make(push).dispatch(
                Operation.CREATE_PUSH_CONFIG,
                {'taskId': 't', 'pushNotificationConfig': {}},
            )
        )
        assert first.error.error_type is ErrorType.TASK_NOT_CANCELABLE
        assert second.error.error_type is ErrorType.PUSH_NOTIFICATION_NOT_SUPPORTED
        assert first.status == second.status == 400

    def test_error_without_error_info_is_reported_unnamed(self):
        """A SUT omitting the mandated ErrorInfo is a finding; the status
        cannot be used to guess which 400 this was."""
        handler = replying(status_error(400, 'FAILED_PRECONDITION'), status=400)
        response = asyncio.run(make(handler).dispatch(Operation.CANCEL_TASK, {'id': 'x'}))
        assert response.error is not None
        assert response.error.error_type is None
        assert response.error.reason is None

    def test_non_json_error_body_is_still_reported(self):
        handler = replying('<html>502</html>', status=502)
        response = asyncio.run(make(handler).dispatch(Operation.GET_TASK, {'id': 'x'}))
        assert not response.ok
        assert 'no google.rpc.Status body' in response.error.message
        assert response.raw_body == '<html>502</html>'

    def test_2xx_is_never_read_as_an_error(self):
        handler = replying({'id': 't1'}, status=200)
        response = asyncio.run(make(handler).dispatch(Operation.GET_TASK, {'id': 't1'}))
        assert response.error is None

    def test_unknown_reason_is_reported_unnamed(self):
        handler = replying(
            status_error(400, 'INVALID_ARGUMENT', reason='SOMETHING_NEW'), status=400
        )
        response = asyncio.run(make(handler).dispatch(Operation.GET_TASK, {'id': 'x'}))
        assert response.error.reason == 'SOMETHING_NEW'
        assert response.error.error_type is None


class TestAgentCard:
    def test_card_is_fetched_from_the_well_known_path(self):
        handler = replying({'name': 'agent'})
        asyncio.run(make(handler).dispatch(Operation.GET_AGENT_CARD))
        assert handler.seen[0].url.path == '/.well-known/agent-card.json'

    def test_extended_card_is_a_normal_rest_call(self):
        handler = replying({'name': 'agent'})
        asyncio.run(make(handler).dispatch(Operation.GET_EXTENDED_AGENT_CARD))
        request = handler.seen[0]
        assert request.method == 'GET'
        assert request.url.path == '/extendedAgentCard'


class TestDispatchRaw:
    def test_request_is_sent_as_written(self):
        handler = replying({'error': {'code': 404}}, status=404)
        raw = RawBlock(
            method=HttpMethod.GET,
            path='/tasks/nonexistent-id',
            headers={'Accept': 'application/a2a+json'},
        )
        response = asyncio.run(make(handler).dispatch_raw(raw))
        assert handler.seen[0].url.path == '/tasks/nonexistent-id'
        assert response.status == 404

    def test_payload_is_the_whole_body(self):
        handler = replying({'error': {'code': 404, 'status': 'NOT_FOUND'}}, status=404)
        raw = RawBlock(method=HttpMethod.GET, path='/tasks/x')
        response = asyncio.run(make(handler).dispatch_raw(raw))
        assert response.payload['error']['status'] == 'NOT_FOUND'


class TestStreaming:
    @staticmethod
    def _collect(dispatcher, operation, params=None):
        async def run():
            return [e async for e in dispatcher.stream(operation, params)]

        return asyncio.run(run())

    def test_events_are_yielded_unwrapped(self):
        """No envelope on this binding, so the event is the payload."""
        body = (
            'data: {"task": {"id": "t1"}}\n\n'
            'data: {"statusUpdate": {"state": "TASK_STATE_COMPLETED"}}\n\n'
        )
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(
                200, text=body, headers={'Content-Type': 'text/event-stream'}
            )

        events = self._collect(
            make(handler), Operation.SEND_STREAMING_MESSAGE, {'message': {}}
        )
        assert [e.data for e in events] == [
            {'task': {'id': 't1'}},
            {'statusUpdate': {'state': 'TASK_STATE_COMPLETED'}},
        ]
        assert seen[0].url.path == '/message:stream'

    def test_subscribe_streams_by_post_to_the_task_path(self):
        """As A2A 1.0 has it, and for a request that declares no version."""
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(
                200,
                text='data: {"task": {}}\n\n',
                headers={'Content-Type': 'text/event-stream'},
            )

        self._collect(make(handler), Operation.SUBSCRIBE_TO_TASK, {'id': 't1'})
        assert seen[0].method == 'POST'
        assert seen[0].url.path == '/tasks/t1:subscribe'

    def test_non_streaming_operation_is_rejected(self):
        handler = replying({})
        with pytest.raises(DispatchError, match='not a streaming operation'):
            self._collect(make(handler), Operation.GET_TASK, {'id': 't1'})

    def test_a_refusal_arrives_as_a_status_document_not_a_406(self):
        """`CORE-CAP-002` and `STREAM-SUB-003`: the conformant answer is a
        REST error, not a stream."""
        def handler(request):
            accepted = [t.split(';')[0].strip() for t in request.headers['accept'].split(',')]
            if 'application/a2a+json' not in accepted:
                return httpx.Response(406)
            return httpx.Response(
                400,
                json=status_error(400, 'FAILED_PRECONDITION', reason='UNSUPPORTED_OPERATION'),
                headers={'Content-Type': 'application/a2a+json'},
            )

        with pytest.raises(StreamNotOpened) as refused:
            self._collect(make(handler), Operation.SUBSCRIBE_TO_TASK, {'id': 't1'})
        assert refused.value.response.error.error_type is ErrorType.UNSUPPORTED_OPERATION


class TestRequestWritten:
    """`on_sent`: the caller is told when the request has been written out.

    A step that stays open while later steps run starts the next step then,
    and not when the reply arrives.
    """

    def test_it_is_called_before_the_reply_arrives(self):
        order: list[str] = []

        class Connection(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                # What the HTTP client does on a real connection: it reports
                # each stage of the exchange to the request's `trace` hook.
                trace = request.extensions['trace']
                await trace('http11.send_request_headers.complete', {})
                order.append('headers written')
                await trace('http11.send_request_body.complete', {})
                order.append('reply')
                return httpx.Response(200, json={'id': 't1'})

        dispatcher = RestDispatcher(
            'http://sut.test', client=httpx.AsyncClient(transport=Connection())
        )
        response = asyncio.run(dispatcher.dispatch(
            Operation.GET_TASK, {'id': 't1'}, on_sent=lambda: order.append('sent')
        ))
        assert order == ['headers written', 'sent', 'reply']
        assert response.payload == {'id': 't1'}

    def test_a_request_that_never_left_is_not_reported_as_sent(self):
        def handler(request):
            raise httpx.ConnectError('refused', request=request)

        sent: list[bool] = []
        with pytest.raises(DispatchError):
            asyncio.run(make(handler).dispatch(
                Operation.GET_TASK, {'id': 't1'}, on_sent=lambda: sent.append(True)
            ))
        assert sent == []

    def test_a_call_that_does_not_ask_is_sent_as_before(self):
        handler = replying({'id': 't1'})
        asyncio.run(make(handler).dispatch(Operation.GET_TASK, {'id': 't1'}))
        assert 'trace' not in handler.seen[0].extensions


class TestSubscribeVerbByVersion:
    """`subscribe_to_task` is POST in A2A 1.0 and GET from A2A 1.1. The
    request is written for the version its `A2A-Version` header declares."""

    @staticmethod
    def _subscribe(params, headers):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(
                200,
                text='data: {"task": {}}\n\n',
                headers={'Content-Type': 'text/event-stream'},
            )

        async def run():
            dispatcher = make(handler)
            return [
                e async for e in
                dispatcher.stream(Operation.SUBSCRIBE_TO_TASK, params, headers)
            ]

        events = asyncio.run(run())
        assert [e.data for e in events] == [{'task': {}}]
        (request,) = seen
        return request

    def test_at_1_1_it_is_a_get(self):
        request = self._subscribe({'id': 't1'}, {'A2A-Version': '1.1'})
        assert request.method == 'GET'
        assert request.url.path == '/tasks/t1:subscribe'
        assert request.url.query == b''
        assert request.content == b''

    def test_at_1_1_the_other_fields_travel_in_the_query(self):
        """A GET has no body. The task id stays in the path."""
        request = self._subscribe(
            {'id': 't1', 'lastSeenGeneration': 7}, {'A2A-Version': '1.1'}
        )
        assert request.method == 'GET'
        assert dict(request.url.params) == {'lastSeenGeneration': '7'}
        assert request.content == b''

    def test_at_1_0_it_is_a_post_with_the_fields_in_the_body(self):
        request = self._subscribe(
            {'id': 't1', 'lastSeenGeneration': 7}, {'A2A-Version': '1.0'}
        )
        assert request.method == 'POST'
        assert request.url.query == b''
        assert json.loads(request.content) == {'lastSeenGeneration': 7}

    def test_a_version_the_dispatcher_adds_decides_too(self):
        """The request goes out with that header, so it is written for it."""
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(
                200, text='data: {"task": {}}\n\n',
                headers={'Content-Type': 'text/event-stream'},
            )

        async def run():
            dispatcher = make(handler, default_headers={'A2A-Version': '1.1'})
            subscribed = dispatcher.stream(Operation.SUBSCRIBE_TO_TASK, {'id': 't1'})
            as_1_0 = dispatcher.stream(
                Operation.SUBSCRIBE_TO_TASK, {'id': 't1'}, {'A2A-Version': '1.0'}
            )
            return [e async for e in subscribed], [e async for e in as_1_0]

        asyncio.run(run())
        assert [(r.method, r.headers['A2A-Version']) for r in seen] == [
            ('GET', '1.1'), ('POST', '1.0'),
        ]

    @pytest.mark.parametrize('from_the_dispatcher', [False, True])
    def test_a_call_answered_with_one_response_is_written_the_same_way(
        self, from_the_dispatcher
    ):
        """The runner streams a subscription. A caller that dispatches one
        gets the same request, whichever of the two names the version."""
        handler = replying({})
        version = {'A2A-Version': '1.1'}

        async def run():
            dispatcher = make(handler, default_headers=version if from_the_dispatcher else None)
            await dispatcher.dispatch(
                Operation.SUBSCRIBE_TO_TASK, {'id': 't1', 'lastSeenGeneration': 2},
                None if from_the_dispatcher else version,
            )

        asyncio.run(run())
        (request,) = handler.seen
        assert (request.method, request.url.query) == ('GET', b'lastSeenGeneration=2')

    def test_the_header_decides_and_not_the_dispatcher(self):
        """An agent may serve two versions at one URL, and a step may be sent
        as a client of either. Both then go through one dispatcher."""
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(
                200, text='data: {"task": {}}\n\n',
                headers={'Content-Type': 'text/event-stream'},
            )

        async def run():
            dispatcher = make(handler)
            for version in ('1.1', '1.0', '1.1'):
                async for _ in dispatcher.stream(
                    Operation.SUBSCRIBE_TO_TASK, {'id': 't1'}, {'a2a-version': version}
                ):
                    pass

        asyncio.run(run())
        assert [(r.method, r.headers['a2a-version']) for r in seen] == [
            ('GET', '1.1'), ('POST', '1.0'), ('GET', '1.1'),
        ]

    def test_the_other_streaming_operation_is_a_post_at_every_version(self):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(
                200, text='data: {"task": {}}\n\n',
                headers={'Content-Type': 'text/event-stream'},
            )

        async def run():
            async for _ in make(handler).stream(
                Operation.SEND_STREAMING_MESSAGE, {'message': {'role': 'ROLE_USER'}},
                {'A2A-Version': '1.1'},
            ):
                pass

        asyncio.run(run())
        assert seen[0].method == 'POST'
        assert json.loads(seen[0].content) == {'message': {'role': 'ROLE_USER'}}

    def test_a_refusal_at_1_1_is_still_read_as_an_error(self):
        def handler(request):
            assert request.method == 'GET'
            return httpx.Response(
                404,
                json=status_error(404, 'NOT_FOUND', reason='TASK_NOT_FOUND'),
                headers={'Content-Type': 'application/a2a+json'},
            )

        async def run():
            async for _ in make(handler).stream(
                Operation.SUBSCRIBE_TO_TASK, {'id': 'gone'}, {'A2A-Version': '1.1'}
            ):
                pass

        with pytest.raises(StreamNotOpened) as refused:
            asyncio.run(run())
        assert refused.value.response.error.error_type is ErrorType.TASK_NOT_FOUND


class _MustNotBeRead(httpx.AsyncByteStream):
    """A response body that fails the test if any of it is read."""

    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self):
        raise AssertionError('the event stream was read')
        yield b''  # pragma: no cover - makes this an async generator

    async def aclose(self) -> None:
        self.closed = True


class TestLiveExchange:
    """`SendLiveMessage` over REST: `POST /message:live` opens the exchange
    and is answered with the event stream that stays open, and every later
    request is one more `POST /message:live`, answered with the task."""

    OPENING = {'sendMessage': {'message': {'messageId': 'm1', 'role': 'ROLE_USER'}}}
    LATER = {'sendMessage': {'message': {'messageId': 'm2', 'taskId': 'T1'}}}
    TASK = {'id': 'T1', 'status': {'state': 'TASK_STATE_WORKING'}}

    @staticmethod
    def serving(later: Callable[[httpx.Request], httpx.Response]):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            body = json.loads(request.content)
            if 'taskId' in body.get('sendMessage', {}).get('message', {}):
                return later(request)
            return httpx.Response(
                200,
                text='data: {"task": {"id": "T1"}}\n\ndata: {"statusUpdate": {"taskId": "T1"}}\n\n',
                headers={'Content-Type': 'text/event-stream'},
            )

        handler.seen = seen  # type: ignore[attr-defined]
        return handler

    def acknowledging(self):
        return self.serving(lambda request: httpx.Response(200, json=self.TASK))

    def test_nothing_is_sent_until_the_downlink_is_read(self):
        handler = self.acknowledging()
        make(handler).live(self.OPENING, {'A2A-Version': '1.1'})
        assert handler.seen == []

    def test_the_opening_request_is_a_post_answered_with_the_events(self):
        handler = self.acknowledging()

        async def run():
            exchange = make(handler).live(self.OPENING, {'A2A-Version': '1.1'})
            return [event async for event in exchange.events()]

        events = asyncio.run(run())
        (request,) = handler.seen
        assert request.method == 'POST'
        assert request.url.path == '/message:live'
        # The body is the request itself; this binding has no envelope.
        assert json.loads(request.content) == self.OPENING
        assert request.headers['a2a-version'] == '1.1'
        assert request.headers['accept'].startswith('text/event-stream')
        assert [event.data for event in events] == [
            {'task': {'id': 'T1'}}, {'statusUpdate': {'taskId': 'T1'}},
        ]

    def test_a_later_request_is_a_post_of_its_own_answered_with_the_task(self):
        handler = self.acknowledging()

        async def run():
            dispatcher = make(handler, default_headers={'Authorization': 'Bearer t'})
            exchange = dispatcher.live(self.OPENING, {'A2A-Version': '1.1'})
            return await exchange.send(self.LATER)

        answer = asyncio.run(run())
        (request,) = handler.seen
        assert request.headers['authorization'] == 'Bearer t'
        assert request.method == 'POST'
        assert request.url.path == '/message:live'
        assert json.loads(request.content) == self.LATER
        assert request.headers['a2a-version'] == '1.1'
        assert request.headers['accept'].startswith('text/event-stream')
        assert answer.status == 200
        assert answer.error is None
        assert answer.payload == self.TASK

    def test_a_later_request_answered_with_an_event_stream_is_refused_unread(self):
        body = _MustNotBeRead()
        handler = self.serving(
            lambda request: httpx.Response(
                200, headers={'Content-Type': 'text/event-stream'}, stream=body
            )
        )

        async def run():
            return await make(handler).live(self.OPENING).send(self.LATER)

        with pytest.raises(MalformedResponse, match='with an event stream'):
            asyncio.run(run())
        assert body.closed

    def test_a_later_request_answered_with_an_error_is_returned_with_it(self):
        handler = self.serving(
            lambda request: httpx.Response(
                404,
                json=status_error(404, 'NOT_FOUND', 'no such task', reason='TASK_NOT_FOUND'),
            )
        )

        async def run():
            return await make(handler).live(self.OPENING).send(self.LATER)

        answer = asyncio.run(run())
        assert answer.status == 404
        assert answer.error.error_type is ErrorType.TASK_NOT_FOUND

    def test_an_agent_that_refuses_to_open_the_exchange(self):
        handler = replying(
            status_error(400, 'FAILED_PRECONDITION', reason='UNSUPPORTED_OPERATION'),
            status=400,
        )

        async def run():
            exchange = make(handler).live(self.OPENING)
            return [event async for event in exchange.events()]

        with pytest.raises(StreamNotOpened) as refused:
            asyncio.run(run())
        assert refused.value.response.error.error_type is ErrorType.UNSUPPORTED_OPERATION
