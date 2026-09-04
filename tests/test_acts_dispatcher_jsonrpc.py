"""The JSON-RPC adapter (A2A §9).

Driven against an :class:`httpx.MockTransport`, so the assertions are about
the request this adapter *builds* and the reply it *reads*, with no server in
the way. Tests are sync and call ``asyncio.run``, matching the rest of the
suite — the repo has no async pytest plugin.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import httpx
import pytest

from test_suite.acts.dispatcher import DispatchError, JsonRpcDispatcher
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
) -> JsonRpcDispatcher:
    """A dispatcher wired to an in-process handler."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JsonRpcDispatcher('http://sut.test', client=client, **kwargs)


def replying(payload: Any, *, status: int = 200, headers: dict | None = None):
    """A handler that records the request it saw and returns ``payload``."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if isinstance(payload, str):
            return httpx.Response(status, text=payload, headers=headers)
        return httpx.Response(status, json=payload, headers=headers)

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


def result(value: Any, *, id_: int = 1) -> dict:
    return {'jsonrpc': '2.0', 'id': id_, 'result': value}


def error(code: int, message: str = 'boom', data: Any = None) -> dict:
    err: dict[str, Any] = {'code': code, 'message': message}
    if data is not None:
        err['data'] = data
    return {'jsonrpc': '2.0', 'id': 1, 'error': err}


class TestRequestConstruction:
    def test_binding_is_jsonrpc(self):
        assert JsonRpcDispatcher('http://x').binding is TransportBinding.JSONRPC

    def test_posts_a_jsonrpc_envelope_to_the_rpc_path(self):
        handler = replying(result({'id': 't1'}))
        asyncio.run(
            make(handler).dispatch(Operation.GET_TASK, {'id': 't1'})
        )
        request = handler.seen[0]
        assert request.method == 'POST'
        assert str(request.url) == 'http://sut.test/'
        body = json.loads(request.content)
        assert body['jsonrpc'] == '2.0'
        assert body['method'] == 'GetTask'
        assert body['params'] == {'id': 't1'}

    def test_method_names_come_from_the_wire_map(self):
        """Notably the push-config names, where ACTS §4.1 is stale."""
        handler = replying(result({}))
        asyncio.run(
            make(handler).dispatch(
                Operation.CREATE_PUSH_CONFIG,
                {'taskId': 't1', 'pushNotificationConfig': {'url': 'http://cb'}},
            )
        )
        body = json.loads(handler.seen[0].content)
        assert body['method'] == 'CreateTaskPushNotificationConfig'

    def test_rpc_path_is_configurable(self):
        handler = replying(result({}))
        asyncio.run(
            make(handler, rpc_path='/rpc').dispatch(Operation.LIST_TASKS)
        )
        assert str(handler.seen[0].url) == 'http://sut.test/rpc'

    def test_request_ids_increment(self):
        handler = replying(result({}))
        dispatcher = make(handler)
        asyncio.run(dispatcher.dispatch(Operation.LIST_TASKS))
        asyncio.run(dispatcher.dispatch(Operation.LIST_TASKS))
        ids = [json.loads(r.content)['id'] for r in handler.seen]
        assert ids == [1, 2]

    def test_sends_the_jsonrpc_content_type(self):
        handler = replying(result({}))
        asyncio.run(make(handler).dispatch(Operation.LIST_TASKS))
        assert handler.seen[0].headers['content-type'] == 'application/json'

    def test_step_headers_override_defaults(self):
        """Several tests set a deliberately wrong Content-Type and assert the
        SUT rejects it; a default must never win over the step."""
        handler = replying(result({}))
        dispatcher = make(handler, default_headers={'A2A-Version': '1.0'})
        asyncio.run(
            dispatcher.dispatch(
                Operation.LIST_TASKS,
                headers={'Content-Type': 'text/plain', 'A2A-Version': '0.3'},
            )
        )
        headers = handler.seen[0].headers
        assert headers['content-type'] == 'text/plain'
        assert headers['a2a-version'] == '0.3'

    def test_params_are_adapted_before_sending(self):
        """`taskId` belongs on the Message, not on SendMessageRequest."""
        handler = replying(result({}))
        asyncio.run(
            make(handler).dispatch(
                Operation.SEND_MESSAGE,
                {'taskId': 't1', 'message': {'role': 'ROLE_USER'}},
            )
        )
        params = json.loads(handler.seen[0].content)['params']
        assert 'taskId' not in params
        assert params['message']['taskId'] == 't1'


class TestResponseReading:
    def test_payload_is_the_result_not_the_envelope(self):
        """`expect.body` mirrors the response message; if the envelope came
        back, every assertion would have to start with `result:`."""
        handler = replying(result({'task': {'id': 't1'}}))
        response = asyncio.run(make(handler).dispatch(Operation.SEND_MESSAGE))
        assert response.payload == {'task': {'id': 't1'}}
        assert response.ok

    def test_status_and_headers_are_exposed(self):
        handler = replying(result({}), headers={'X-Trace': 'abc'})
        response = asyncio.run(make(handler).dispatch(Operation.LIST_TASKS))
        assert response.status == 200
        assert response.headers['x-trace'] == 'abc'

    def test_raw_body_is_preserved(self):
        handler = replying(result({'a': 1}))
        response = asyncio.run(make(handler).dispatch(Operation.LIST_TASKS))
        assert json.loads(response.raw_body)['result'] == {'a': 1}


class TestErrorReading:
    def test_error_maps_to_the_abstract_name(self):
        handler = replying(error(-32001, 'Task not found'))
        response = asyncio.run(make(handler).dispatch(Operation.GET_TASK, {'id': 'x'}))
        assert not response.ok
        assert response.error.error_type is ErrorType.TASK_NOT_FOUND
        assert response.error.code == -32001
        assert response.error.message == 'Task not found'

    def test_a_jsonrpc_error_rides_http_200(self):
        """Which is why `expect.status` beside `expect_error` is coherent."""
        handler = replying(error(-32001))
        response = asyncio.run(make(handler).dispatch(Operation.GET_TASK, {'id': 'x'}))
        assert response.status == 200
        assert response.error is not None

    def test_unknown_code_is_reported_unnamed(self):
        """A code this suite cannot name is a finding, not something to map
        onto the nearest plausible error."""
        handler = replying(error(-31999))
        response = asyncio.run(make(handler).dispatch(Operation.LIST_TASKS))
        assert response.error.code == -31999
        assert response.error.error_type is None

    def test_error_info_reason_is_extracted_from_data(self):
        handler = replying(
            error(
                -32001,
                data=[
                    {
                        '@type': 'type.googleapis.com/google.rpc.ErrorInfo',
                        'reason': 'TASK_NOT_FOUND',
                        'domain': 'a2a-protocol.org',
                    }
                ],
            )
        )
        response = asyncio.run(make(handler).dispatch(Operation.LIST_TASKS))
        assert response.error.reason == 'TASK_NOT_FOUND'
        assert len(response.error.details) == 1

    def test_http_error_without_an_envelope_is_still_reported(self):
        """A SUT answering 500 with an HTML page has failed visibly."""
        handler = replying('<html>oops</html>', status=500)
        response = asyncio.run(make(handler).dispatch(Operation.LIST_TASKS))
        assert not response.ok
        assert '500' in response.error.message
        assert response.status == 500

    def test_success_is_not_mistaken_for_an_error(self):
        handler = replying(result({'tasks': []}))
        response = asyncio.run(make(handler).dispatch(Operation.LIST_TASKS))
        assert response.error is None


class TestAgentCard:
    def test_card_is_fetched_from_the_well_known_path(self):
        """Not an RPC on any binding — the card is what names the bindings."""
        handler = replying({'name': 'agent'})
        response = asyncio.run(make(handler).dispatch(Operation.GET_AGENT_CARD))
        request = handler.seen[0]
        assert request.method == 'GET'
        assert str(request.url) == 'http://sut.test/.well-known/agent-card.json'
        assert response.payload == {'name': 'agent'}

    def test_card_request_is_not_a_jsonrpc_envelope(self):
        handler = replying({'name': 'agent'})
        asyncio.run(make(handler).dispatch(Operation.GET_AGENT_CARD))
        assert not handler.seen[0].content


class TestDispatchRaw:
    def test_body_is_sent_verbatim(self):
        handler = replying(error(-32601))
        raw = RawBlock(
            method=HttpMethod.POST,
            path='/',
            headers={'Content-Type': 'application/json'},
            body={'jsonrpc': '2.0', 'id': 1, 'method': 'DoesNotExist', 'params': {}},
        )
        asyncio.run(make(handler).dispatch_raw(raw))
        sent = json.loads(handler.seen[0].content)
        assert sent['method'] == 'DoesNotExist'

    def test_invalid_json_is_sent_unparsed(self):
        """The ParseError tests depend on this reaching the SUT intact."""
        handler = replying(error(-32700))
        raw = RawBlock(
            method=HttpMethod.POST,
            path='/',
            body_raw='{this is not valid json',
        )
        asyncio.run(make(handler).dispatch_raw(raw))
        assert handler.seen[0].content == b'{this is not valid json'

    def test_payload_is_the_whole_envelope(self):
        """A raw test asserts on the envelope, down to `error.code`."""
        handler = replying(error(-32601))
        raw = RawBlock(method=HttpMethod.POST, path='/', body={})
        response = asyncio.run(make(handler).dispatch_raw(raw))
        assert response.payload['error']['code'] == -32601
        assert response.payload['jsonrpc'] == '2.0'

    def test_error_is_also_surfaced(self):
        """§4.4 permits `expect_error` on a raw step."""
        handler = replying(error(-32601))
        raw = RawBlock(method=HttpMethod.POST, path='/', body={})
        response = asyncio.run(make(handler).dispatch_raw(raw))
        assert response.error.error_type is ErrorType.METHOD_NOT_FOUND

    def test_unparseable_reply_leaves_payload_none_and_keeps_raw(self):
        handler = replying('not json at all')
        raw = RawBlock(method=HttpMethod.POST, path='/', body={})
        response = asyncio.run(make(handler).dispatch_raw(raw))
        assert response.payload is None
        assert response.raw_body == 'not json at all'

    def test_raw_headers_reach_the_wire(self):
        handler = replying(result({}))
        raw = RawBlock(
            method=HttpMethod.GET,
            path='/.well-known/agent-card.json',
            headers={'A2A-Version': '99.0'},
        )
        asyncio.run(make(handler).dispatch_raw(raw))
        assert handler.seen[0].headers['a2a-version'] == '99.0'


def sse(*events: dict) -> str:
    return ''.join(f'data: {json.dumps(e)}\n\n' for e in events)


class TestStreaming:
    @staticmethod
    def _collect(dispatcher, operation, params=None):
        async def run():
            return [e async for e in dispatcher.stream(operation, params)]

        return asyncio.run(run())

    def test_events_are_yielded_in_order_and_unwrapped(self):
        """Each SSE event is its own JSON-RPC envelope; without unwrapping,
        streaming assertions would need a `result:` prefix on this binding
        only."""
        body = sse(
            result({'task': {'id': 't1'}}),
            result({'statusUpdate': {'state': 'TASK_STATE_WORKING'}}),
        )

        def handler(request):
            return httpx.Response(
                200, text=body, headers={'Content-Type': 'text/event-stream'}
            )

        events = self._collect(
            make(handler), Operation.SEND_STREAMING_MESSAGE, {'message': {}}
        )
        assert [e.index for e in events] == [0, 1]
        assert events[0].data == {'task': {'id': 't1'}}
        assert events[1].data == {'statusUpdate': {'state': 'TASK_STATE_WORKING'}}

    def test_raw_event_text_is_kept(self):
        body = sse(result({'a': 1}))

        def handler(request):
            return httpx.Response(
                200, text=body, headers={'Content-Type': 'text/event-stream'}
            )

        events = self._collect(
            make(handler), Operation.SEND_STREAMING_MESSAGE, {'message': {}}
        )
        assert json.loads(events[0].raw)['result'] == {'a': 1}

    def test_accept_header_requests_an_event_stream(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(
                200, text=sse(result({})), headers={'Content-Type': 'text/event-stream'}
            )

        self._collect(make(handler), Operation.SUBSCRIBE_TO_TASK, {'id': 't1'})
        assert seen[0].headers['accept'] == 'text/event-stream'

    def test_non_streaming_operation_is_rejected(self):
        handler = replying(result({}))
        with pytest.raises(DispatchError, match='not a streaming operation'):
            self._collect(make(handler), Operation.GET_TASK, {'id': 't1'})

    def test_the_observed_http_status_rides_every_event(self):
        """A step may assert `expect.status` beside `expect_stream`; the value
        has to be observed rather than inferred from the stream working."""
        def handler(request):
            return httpx.Response(
                200, text=sse(result({}), result({})),
                headers={'Content-Type': 'text/event-stream'},
            )

        events = self._collect(
            make(handler), Operation.SEND_STREAMING_MESSAGE, {'message': {}}
        )
        assert [e.status for e in events] == [200, 200]


class TestStreamRaw:
    """§4.4 plus §7 — `JSONRPC-SSE-001` needs both at once."""

    @staticmethod
    def _collect(dispatcher, raw, headers=None):
        async def run():
            return [e async for e in dispatcher.stream_raw(raw, headers)]

        return asyncio.run(run())

    def _handler(self, seen):
        def handler(request):
            seen.append(request)
            return httpx.Response(
                200,
                text=sse(result({'task': {'id': 't1'}}), result({'task': {'id': 't1'}})),
                headers={'Content-Type': 'text/event-stream'},
            )

        return handler

    def test_events_are_not_unwrapped(self):
        """A raw streaming test asserts on the envelope — `each_event:
        {jsonrpc: "2.0"}` — so unwrapping would delete the thing under test."""
        seen = []
        raw = RawBlock(method=HttpMethod.POST, path='/', body={'jsonrpc': '2.0'})
        events = self._collect(make(self._handler(seen)), raw)
        assert events[0].data['jsonrpc'] == '2.0'
        assert 'result' in events[0].data

    def test_the_request_goes_as_written(self):
        seen = []
        raw = RawBlock(
            method=HttpMethod.POST,
            path='/',
            headers={'A2A-Version': '99.0'},
            body={'jsonrpc': '2.0', 'method': 'SendStreamingMessage'},
        )
        self._collect(make(self._handler(seen)), raw)
        assert seen[0].headers['a2a-version'] == '99.0'
        assert json.loads(seen[0].content)['method'] == 'SendStreamingMessage'

    def test_body_raw_is_sent_unparsed(self):
        seen = []
        raw = RawBlock(method=HttpMethod.POST, path='/', body_raw='{not json')
        self._collect(make(self._handler(seen)), raw)
        assert seen[0].content == b'{not json'

    def test_the_status_is_observed(self):
        seen = []
        raw = RawBlock(method=HttpMethod.POST, path='/', body={'jsonrpc': '2.0'})
        events = self._collect(make(self._handler(seen)), raw)
        assert events[0].status == 200


class TestTransportFailure:
    def test_connection_failure_raises_rather_than_returning_an_error(self):
        """A failure to reach the SUT is a broken run, not a test result."""

        def handler(request):
            raise httpx.ConnectError('refused')

        with pytest.raises(DispatchError, match='ConnectError'):
            asyncio.run(make(handler).dispatch(Operation.LIST_TASKS))
