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
        """ACTS §4.1 says GET; A2A §11.3.2 says POST."""
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
