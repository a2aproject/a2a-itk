"""Reshaping abstract step params into A2A request messages.

Two operations need it, the same way on all three bindings. These tests pin
that each reshaping is *mechanical* — determined by the target message's
shape — and check the claim the module rests on: that the corpus's own
assertions already speak the reshaped form.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from google.protobuf.json_format import ParseDict

from pyproto import a2a_pb2

from test_suite.acts import load_suite
from test_suite.acts.dispatcher.params import adapt
from test_suite.acts.schema import Operation


CORPUS = Path('scenarios/acts/suite.acts.yaml')


class TestSendMessage:
    def test_task_id_folds_onto_the_message(self):
        out = adapt(
            Operation.SEND_MESSAGE,
            {'taskId': 't1', 'message': {'role': 'ROLE_USER'}},
        )
        assert out == {'message': {'role': 'ROLE_USER', 'taskId': 't1'}}

    def test_context_id_folds_too(self):
        out = adapt(
            Operation.SEND_MESSAGE,
            {'contextId': 'c1', 'message': {'role': 'ROLE_USER'}},
        )
        assert out['message']['contextId'] == 'c1'

    def test_other_params_are_left_alone(self):
        out = adapt(
            Operation.SEND_MESSAGE,
            {'message': {'role': 'ROLE_USER'}, 'configuration': {'historyLength': 2}},
        )
        assert out['configuration'] == {'historyLength': 2}

    def test_a_value_already_on_the_message_wins(self):
        """The test said it twice; the inner one is the more specific."""
        out = adapt(
            Operation.SEND_MESSAGE,
            {'taskId': 'outer', 'message': {'taskId': 'inner'}},
        )
        assert out['message']['taskId'] == 'inner'
        assert 'taskId' not in out

    def test_streaming_send_is_reshaped_the_same_way(self):
        out = adapt(
            Operation.SEND_STREAMING_MESSAGE,
            {'taskId': 't1', 'message': {'role': 'ROLE_USER'}},
        )
        assert out['message']['taskId'] == 't1'

    def test_a_missing_message_is_left_for_the_sut_to_reject(self):
        """A send_message with no message is a test asserting exactly that."""
        out = adapt(Operation.SEND_MESSAGE, {'taskId': 't1'})
        assert out == {'taskId': 't1'}


class TestCreatePushConfig:
    def test_nested_config_is_flattened(self):
        out = adapt(
            Operation.CREATE_PUSH_CONFIG,
            {'taskId': 't1', 'pushNotificationConfig': {'url': 'http://cb'}},
        )
        assert out == {'taskId': 't1', 'url': 'http://cb'}

    def test_every_nested_field_is_lifted(self):
        out = adapt(
            Operation.CREATE_PUSH_CONFIG,
            {
                'taskId': 't1',
                'pushNotificationConfig': {
                    'url': 'http://cb',
                    'token': 'tok',
                    'id': 'c1',
                },
            },
        )
        assert out == {
            'taskId': 't1',
            'url': 'http://cb',
            'token': 'tok',
            'id': 'c1',
        }

    def test_an_already_flat_param_wins(self):
        out = adapt(
            Operation.CREATE_PUSH_CONFIG,
            {'taskId': 't1', 'url': 'outer', 'pushNotificationConfig': {'url': 'in'}},
        )
        assert out['url'] == 'outer'

    def test_a_flat_call_is_unchanged(self):
        params = {'taskId': 't1', 'url': 'http://cb'}
        assert adapt(Operation.CREATE_PUSH_CONFIG, params) == params


class TestUntouchedOperations:
    @pytest.mark.parametrize(
        'operation',
        [
            Operation.GET_TASK,
            Operation.CANCEL_TASK,
            Operation.LIST_TASKS,
            Operation.DELETE_PUSH_CONFIG,
            Operation.GET_PUSH_CONFIG,
            Operation.LIST_PUSH_CONFIGS,
            Operation.SUBSCRIBE_TO_TASK,
            Operation.GET_AGENT_CARD,
            Operation.GET_EXTENDED_AGENT_CARD,
        ],
    )
    def test_params_pass_through(self, operation):
        params = {'id': 't1', 'taskId': 'x', 'other': 1}
        assert adapt(operation, params) == params

    def test_none_becomes_an_empty_mapping(self):
        assert adapt(Operation.LIST_TASKS, None) == {}


class TestPurity:
    def test_the_step_params_are_not_mutated(self):
        """A retry, or a second binding running the same test, must see what
        the test wrote."""
        params = {'taskId': 't1', 'message': {'role': 'ROLE_USER'}}
        adapt(Operation.SEND_MESSAGE, params)
        assert params == {'taskId': 't1', 'message': {'role': 'ROLE_USER'}}

    def test_the_nested_message_is_not_mutated(self):
        message = {'role': 'ROLE_USER'}
        adapt(Operation.SEND_MESSAGE, {'taskId': 't1', 'message': message})
        assert message == {'role': 'ROLE_USER'}

    def test_the_nested_push_config_is_not_mutated(self):
        config = {'url': 'http://cb'}
        adapt(Operation.CREATE_PUSH_CONFIG, {'taskId': 't1', 'pushNotificationConfig': config})
        assert config == {'url': 'http://cb'}


class TestAgainstTheRealMessages:
    """The reshaped form has to be what the protobuf actually accepts."""

    REQUEST = {
        Operation.SEND_MESSAGE: 'SendMessageRequest',
        Operation.SEND_STREAMING_MESSAGE: 'SendMessageRequest',
        Operation.GET_TASK: 'GetTaskRequest',
        Operation.LIST_TASKS: 'ListTasksRequest',
        Operation.CANCEL_TASK: 'CancelTaskRequest',
        Operation.SUBSCRIBE_TO_TASK: 'SubscribeToTaskRequest',
        Operation.CREATE_PUSH_CONFIG: 'TaskPushNotificationConfig',
        Operation.GET_PUSH_CONFIG: 'GetTaskPushNotificationConfigRequest',
        Operation.LIST_PUSH_CONFIGS: 'ListTaskPushNotificationConfigsRequest',
        Operation.DELETE_PUSH_CONFIG: 'DeleteTaskPushNotificationConfigRequest',
    }

    def test_every_corpus_step_parses_into_its_request_message(self):
        """The end-to-end claim: with adaptation, every operation step in the
        corpus produces a valid A2A request. Path params are dropped first,
        since REST puts them in the URL and the proto keeps them as fields —
        both spellings are checked by simply not removing them here.
        """
        failures = []
        for loaded in load_suite(CORPUS).tests:
            for step in loaded.test.steps:
                if step.operation is None or step.operation not in self.REQUEST:
                    continue
                payload = adapt(step.operation, step.params)
                message = getattr(a2a_pb2, self.REQUEST[step.operation])()
                try:
                    ParseDict(_without_templates(payload), message)
                except Exception as exc:  # noqa: BLE001 - collected and reported
                    failures.append(
                        f'{loaded.test.id}/{step.id} ({step.operation.value}): {exc}'
                    )
        assert not failures, '\n'.join(failures)

    def test_unadapted_send_message_would_not_parse(self):
        """Why the adaptation exists rather than being cosmetic."""
        with pytest.raises(Exception, match='taskId'):
            ParseDict(
                {'taskId': 't1', 'message': {'role': 'ROLE_USER'}},
                a2a_pb2.SendMessageRequest(),
            )

    def test_unadapted_create_push_config_would_not_parse(self):
        with pytest.raises(Exception, match='pushNotificationConfig'):
            ParseDict(
                {'taskId': 't1', 'pushNotificationConfig': {'url': 'http://cb'}},
                a2a_pb2.TaskPushNotificationConfig(),
            )


def _without_templates(value):
    """Replace ``{{...}}`` placeholders with a plain string.

    Variable substitution is the runner's job (story 4.3); here the point is
    the *shape*, and an unsubstituted template is still a string.
    """
    if isinstance(value, dict):
        return {k: _without_templates(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_without_templates(v) for v in value]
    if isinstance(value, str) and '{{' in value:
        return 'placeholder'
    return value
