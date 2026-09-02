"""The wire map is the single source of method names, paths and error codes.

These tests pin it against three independent authorities — the A2A
specification, the generated gRPC service, and the corpus that has to run
through it — so that a change to any row has to be deliberate.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from test_suite.acts import load_suite
from test_suite.acts.schema import ErrorType, HttpMethod, Operation
from test_suite.acts.wire_map import (
    ERRORS,
    GRPC_SERVICE,
    GRPC_STATUS_TO_HTTP,
    OPERATIONS,
    binding_for_error,
    binding_for_operation,
    error_for_jsonrpc_code,
    error_for_reason,
    errors_sharing_http_status,
    http_status_for_grpc,
)


CORPUS = Path('scenarios/acts/suite.acts.yaml')


class TestCoverage:
    """Every abstract name the schema admits has to bind to something."""

    def test_every_operation_is_bound(self):
        assert set(OPERATIONS) == set(Operation)

    def test_every_error_is_bound(self):
        assert set(ERRORS) == set(ErrorType)

    def test_lookups_are_total(self):
        for operation in Operation:
            assert binding_for_operation(operation) is OPERATIONS[operation]
        for error in ErrorType:
            assert binding_for_error(error) is ERRORS[error]


class TestOperationsMatchA2ASpec:
    """A2A §5.3, which ACTS §6.2's footnote makes authoritative over §4.1."""

    #: Transcribed from docs/specification.md §5.3 at dbcabfba.
    SPEC_5_3 = {
        Operation.SEND_MESSAGE: ('SendMessage', 'POST', '/message:send'),
        Operation.SEND_STREAMING_MESSAGE: (
            'SendStreamingMessage', 'POST', '/message:stream',
        ),
        Operation.GET_TASK: ('GetTask', 'GET', '/tasks/{id}'),
        Operation.LIST_TASKS: ('ListTasks', 'GET', '/tasks'),
        Operation.CANCEL_TASK: ('CancelTask', 'POST', '/tasks/{id}:cancel'),
        Operation.SUBSCRIBE_TO_TASK: (
            'SubscribeToTask', 'POST', '/tasks/{id}:subscribe',
        ),
        Operation.GET_EXTENDED_AGENT_CARD: (
            'GetExtendedAgentCard', 'GET', '/extendedAgentCard',
        ),
    }

    @pytest.mark.parametrize('operation', sorted(SPEC_5_3, key=lambda o: o.value))
    def test_row_matches_spec(self, operation):
        method, http_method, path = self.SPEC_5_3[operation]
        binding = binding_for_operation(operation)
        assert binding.jsonrpc_method == method
        assert binding.grpc_method == method
        assert binding.rest_method == HttpMethod(http_method)
        assert binding.rest_path == path

    def test_subscribe_is_post_not_get(self):
        """ACTS §4.1 says GET; A2A §5.3 and §11.3.2 both say POST."""
        assert binding_for_operation(
            Operation.SUBSCRIBE_TO_TASK
        ).rest_method is HttpMethod.POST

    @pytest.mark.parametrize(
        'operation',
        [
            Operation.CREATE_PUSH_CONFIG,
            Operation.GET_PUSH_CONFIG,
            Operation.LIST_PUSH_CONFIGS,
            Operation.DELETE_PUSH_CONFIG,
        ],
    )
    def test_push_config_uses_the_a2a_names(self, operation):
        """ACTS §4.1 drops the `Task` infix and writes `pushNotifications`.

        Following it would 404 every push-config call, so these rows track
        A2A §5.3 instead.
        """
        binding = binding_for_operation(operation)
        assert binding.jsonrpc_method.endswith('TaskPushNotificationConfig') or (
            binding.jsonrpc_method.endswith('TaskPushNotificationConfigs')
        )
        assert 'Task' in binding.jsonrpc_method
        assert '/pushNotificationConfigs' in binding.rest_path
        assert '/pushNotifications/' not in binding.rest_path

    def test_agent_card_is_http_on_every_binding(self):
        binding = binding_for_operation(Operation.GET_AGENT_CARD)
        assert binding.http_only
        assert binding.rest_path == '/.well-known/agent-card.json'
        assert binding.jsonrpc_method == ''
        assert binding.grpc_method == ''

    def test_only_the_two_streaming_operations_stream(self):
        streaming = {o for o, b in OPERATIONS.items() if b.streaming}
        assert streaming == {
            Operation.SEND_STREAMING_MESSAGE,
            Operation.SUBSCRIBE_TO_TASK,
        }


class TestOperationsMatchGeneratedService:
    """The gRPC column has to name RPCs that actually exist."""

    @staticmethod
    def _stub_rpcs() -> dict[str, tuple[str, str]]:
        import inspect

        from pyproto import a2a_pb2_grpc

        source = inspect.getsource(a2a_pb2_grpc.A2AServiceStub.__init__)
        pattern = re.compile(
            r"self\.(\w+) = channel\.\w+\(\s*'([^']+)',\s*"
            r'request_serializer=[\w.]*?(\w+)\.SerializeToString,\s*'
            r'response_deserializer=[\w.]*?(\w+)\.FromString',
        )
        return {
            m.group(1): (m.group(2), m.group(3), m.group(4))
            for m in pattern.finditer(source)
        }

    def test_service_name_matches_the_generated_descriptor(self):
        rpcs = self._stub_rpcs()
        assert rpcs, 'could not parse the generated stub'
        for full_path, _, _ in rpcs.values():
            assert full_path.startswith(f'/{GRPC_SERVICE}/')

    def test_every_rpc_operation_exists_on_the_service(self):
        rpcs = self._stub_rpcs()
        for operation, binding in OPERATIONS.items():
            if binding.http_only:
                continue
            assert binding.grpc_method in rpcs, (
                f'{operation.value} names a missing RPC'
            )

    def test_message_types_match_the_generated_stub(self):
        rpcs = self._stub_rpcs()
        for operation, binding in OPERATIONS.items():
            if binding.http_only:
                continue
            _, request, response = rpcs[binding.grpc_method]
            assert binding.grpc_request.split('.')[-1] == request, operation.value
            assert binding.grpc_response.split('.')[-1] == response, operation.value

    def test_there_is_no_get_agent_card_rpc(self):
        """Why `get_agent_card` is `http_only` rather than an oversight."""
        assert 'GetAgentCard' not in self._stub_rpcs()

    def test_message_types_resolve(self):
        from google.protobuf import empty_pb2

        from pyproto import a2a_pb2

        for binding in OPERATIONS.values():
            for name in (binding.grpc_request, binding.grpc_response):
                if not name:
                    continue
                if name == 'google.protobuf.Empty':
                    assert empty_pb2.Empty is not None
                else:
                    assert hasattr(a2a_pb2, name), name


class TestPathTemplates:
    def test_path_params_are_discovered_from_the_template(self):
        assert binding_for_operation(Operation.GET_TASK).path_params == ('id',)
        assert binding_for_operation(
            Operation.GET_PUSH_CONFIG
        ).path_params == ('taskId', 'id')
        assert binding_for_operation(Operation.LIST_TASKS).path_params == ()

    def test_format_path_substitutes_and_returns_the_remainder(self):
        binding = binding_for_operation(Operation.GET_TASK)
        path, rest = binding.format_path({'id': 'abc', 'historyLength': 0})
        assert path == '/tasks/abc'
        assert rest == {'historyLength': 0}

    def test_format_path_consumes_every_placeholder(self):
        binding = binding_for_operation(Operation.DELETE_PUSH_CONFIG)
        path, rest = binding.format_path({'taskId': 't1', 'id': 'c1'})
        assert path == '/tasks/t1/pushNotificationConfigs/c1'
        assert rest == {}
        assert '{' not in path

    def test_missing_path_param_names_itself(self):
        binding = binding_for_operation(Operation.GET_PUSH_CONFIG)
        with pytest.raises(KeyError, match='taskId'):
            binding.format_path({'id': 'c1'})

    def test_format_path_does_not_mutate_its_input(self):
        params = {'id': 'abc'}
        binding_for_operation(Operation.GET_TASK).format_path(params)
        assert params == {'id': 'abc'}

    def test_every_corpus_step_can_be_addressed_on_rest(self):
        """The templates name `taskId`/`id`, not the spec's prose `configId`.

        Checked per step, not per operation: a placeholder that only *some*
        steps supply still breaks the others, and aggregating would hide it.
        """
        from test_suite.acts.dispatcher.params import adapt

        failures = []
        for loaded in load_suite(CORPUS).tests:
            for step in loaded.test.steps:
                if step.operation is None:
                    continue
                binding = binding_for_operation(step.operation)
                if not binding.path_params:
                    continue
                try:
                    path, _ = binding.format_path(adapt(step.operation, step.params))
                except KeyError as exc:
                    failures.append(f'{loaded.test.id}/{step.id}: {exc}')
                    continue
                # A `{{...}}` left in the path is a *runner* variable, which
                # story 4.3 substitutes before dispatch — not a placeholder
                # this layer failed to fill. Only the latter is a bug here.
                for name in binding.path_params:
                    assert '{' + name + '}' not in path, (
                        f'{loaded.test.id}/{step.id}: {name} unfilled in {path}'
                    )
        assert not failures, '\n'.join(failures)


class TestErrorsMatchA2ASpec:
    """A2A §5.4, which ACTS §6.2's own footnote makes authoritative over it."""

    #: Transcribed from docs/specification.md §5.4 at dbcabfba.
    SPEC_5_4 = {
        ErrorType.TASK_NOT_FOUND: (-32001, 'NOT_FOUND', 404),
        ErrorType.TASK_NOT_CANCELABLE: (-32002, 'FAILED_PRECONDITION', 400),
        ErrorType.PUSH_NOTIFICATION_NOT_SUPPORTED: (
            -32003, 'FAILED_PRECONDITION', 400,
        ),
        ErrorType.UNSUPPORTED_OPERATION: (-32004, 'FAILED_PRECONDITION', 400),
        ErrorType.CONTENT_TYPE_NOT_SUPPORTED: (-32005, 'INVALID_ARGUMENT', 400),
        ErrorType.EXTENDED_CARD_NOT_SUPPORTED: (
            -32007, 'FAILED_PRECONDITION', 400,
        ),
        ErrorType.EXTENSION_SUPPORT_REQUIRED: (
            -32008, 'FAILED_PRECONDITION', 400,
        ),
        ErrorType.VERSION_NOT_SUPPORTED: (-32009, 'FAILED_PRECONDITION', 400),
    }

    @pytest.mark.parametrize('error', sorted(SPEC_5_4, key=lambda e: e.value))
    def test_row_matches_spec(self, error):
        code, grpc_status, http_status = self.SPEC_5_4[error]
        binding = binding_for_error(error)
        assert binding.jsonrpc_code == code
        assert binding.grpc_status == grpc_status
        assert binding.http_status == http_status

    def test_version_not_supported_is_32009_not_32006(self):
        """The review bot on #1882 asks for -32006; A2A §5.4 says -32009.

        -32006 is `InvalidAgentResponseError`. `version-negotiation.acts.yaml`
        already asserts -32009 on the wire, so the bot's suggestion would make
        a correct test incorrect.
        """
        assert binding_for_error(ErrorType.VERSION_NOT_SUPPORTED).jsonrpc_code == -32009

    def test_standard_jsonrpc_errors_carry_only_a_code(self):
        """A2A §9.5 defines these; it never maps them onto gRPC or REST.

        `None` says "the spec is silent", which the runner can report. A
        plausible-looking guess here would be indistinguishable from a real
        mapping.
        """
        for error, code in (
            (ErrorType.JSON_PARSE, -32700),
            (ErrorType.METHOD_NOT_FOUND, -32601),
            (ErrorType.INVALID_PARAMS, -32602),
            (ErrorType.INTERNAL, -32603),
        ):
            binding = binding_for_error(error)
            assert binding.jsonrpc_code == code
            assert binding.grpc_status is None
            assert binding.http_status is None
            assert binding.reason is None

    #: ACTS names that are a different spelling of an A2A error. The §11.6
    #: reason derives from the *A2A* name, so these cannot be checked against
    #: their own. Kept explicit rather than skipped, so a new divergence has
    #: to be added here deliberately.
    ACTS_RENAMES = {
        ErrorType.EXTENDED_CARD_NOT_SUPPORTED: (
            'ExtendedAgentCardNotConfiguredError'
        ),
    }

    def test_reason_is_upper_snake_of_the_name(self):
        """A2A §11.6's derivation rule, checked rather than assumed."""
        for error, binding in ERRORS.items():
            if binding.reason is None or binding.aliases is not None:
                continue
            a2a_name = self.ACTS_RENAMES.get(error, error.value)
            expected = re.sub(
                r'(?<!^)(?=[A-Z])', '_', a2a_name.removesuffix('Error')
            ).upper()
            assert binding.reason == expected, error.value

    def test_extended_card_binds_to_the_a2a_spelling(self):
        """ACTS says `ExtendedCardNotSupportedError`; A2A says
        `ExtendedAgentCardNotConfiguredError`. Same error, same code."""
        binding = binding_for_error(ErrorType.EXTENDED_CARD_NOT_SUPPORTED)
        assert binding.jsonrpc_code == -32007
        assert binding.reason == 'EXTENDED_AGENT_CARD_NOT_CONFIGURED'

    def test_streaming_not_supported_aliases_unsupported_operation(self):
        """ACTS invents this error; A2A §3.3.2 answers it with
        `UnsupportedOperationError`, so that is what the wire carries.

        ACTS §6.2 assigns it -32007, which belongs to
        `ExtendedAgentCardNotConfigured` — following that would make two
        distinct errors indistinguishable.
        """
        binding = binding_for_error(ErrorType.STREAMING_NOT_SUPPORTED)
        assert binding.aliases is ErrorType.UNSUPPORTED_OPERATION
        assert binding.jsonrpc_code == -32004
        assert binding.reason == 'UNSUPPORTED_OPERATION'


class TestReverseLookups:
    def test_jsonrpc_code_round_trips(self):
        for error, binding in ERRORS.items():
            if binding.aliases is not None:
                continue
            assert error_for_jsonrpc_code(binding.jsonrpc_code) is error

    def test_reason_round_trips(self):
        for error, binding in ERRORS.items():
            if binding.aliases is not None or binding.reason is None:
                continue
            assert error_for_reason(binding.reason) is error

    def test_an_alias_never_wins_a_reverse_lookup(self):
        """Two ACTS names share -32004. A wire error is reported under the
        A2A name, and `StreamingNotSupportedError` is not one."""
        assert error_for_jsonrpc_code(-32004) is ErrorType.UNSUPPORTED_OPERATION
        assert error_for_reason('UNSUPPORTED_OPERATION') is (
            ErrorType.UNSUPPORTED_OPERATION
        )

    def test_unknown_values_return_none_rather_than_guessing(self):
        assert error_for_jsonrpc_code(-1) is None
        assert error_for_reason('NO_SUCH_REASON') is None

    def test_http_status_is_not_injective(self):
        """Why the runner must match on reason, not status (A2A §11.6)."""
        sharing_400 = errors_sharing_http_status(400)
        assert ErrorType.TASK_NOT_CANCELABLE in sharing_400
        assert ErrorType.PUSH_NOTIFICATION_NOT_SUPPORTED in sharing_400
        assert len(sharing_400) > 1

    def test_task_not_found_is_the_only_404(self):
        assert errors_sharing_http_status(404) == (ErrorType.TASK_NOT_FOUND,)


class TestGrpcStatusTranscoding:
    def test_ok_is_200(self):
        assert http_status_for_grpc('OK') == 200

    def test_spec_http_column_is_the_transcoding_of_its_grpc_column(self):
        """A2A §5.4's HTTP column is exactly this table applied to its gRPC
        column — which is why deriving a status for gRPC is not an invention.
        """
        for error, binding in ERRORS.items():
            if binding.grpc_status is None:
                continue
            assert binding.http_status == GRPC_STATUS_TO_HTTP[binding.grpc_status], (
                error.value
            )

    def test_unknown_status_falls_back_to_500(self):
        assert http_status_for_grpc('NOT_A_STATUS') == 500

    def test_every_grpc_status_code_is_covered(self):
        import grpc

        for code in grpc.StatusCode:
            assert code.name in GRPC_STATUS_TO_HTTP


class TestCorpusRunsThroughTheMap:
    """Whatever the corpus names, the map has to bind."""

    def test_every_operation_the_corpus_uses_is_bound(self):
        suite = load_suite(CORPUS)
        used = {
            step.operation
            for loaded in suite.tests
            for step in loaded.test.steps
            if step.operation
        }
        assert used, 'corpus loaded no operation steps'
        assert used <= set(OPERATIONS)

    def test_every_literal_error_the_corpus_expects_is_bound(self):
        suite = load_suite(CORPUS)
        used = {
            step.expect_error.literal_error_type()
            for loaded in suite.tests
            for step in loaded.test.steps
            if step.expect_error
        }
        used.discard(None)
        assert used, 'corpus loaded no literal expect_error'
        assert used <= set(ERRORS)

    def test_streaming_steps_only_name_streaming_operations(self):
        """A step with `expect_stream` must reach `stream()`, not `dispatch()`."""
        for loaded in load_suite(CORPUS).tests:
            for step in loaded.test.steps:
                if step.expect_stream is None or step.operation is None:
                    continue
                assert binding_for_operation(step.operation).streaming, (
                    f'{loaded.test.id}/{step.id} streams a non-streaming '
                    f'operation {step.operation.value}'
                )
