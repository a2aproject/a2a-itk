import asyncio  # noqa: I001
import base64
import json
import logging
import socket
from typing import Any
import subprocess
import uuid
import os

import httpx
from httpx_sse import aconnect_sse

import test_suite


from pyproto import instruction_pb2
from test_suite.agent_table import AgentTable


logger = logging.getLogger(__name__)


def _get_free_port() -> int:
    """Finds an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


def _clean_ports(*ports: int) -> None:
    """Forcefully kills processes on host ports to ensure fresh startup.

    Args:
        *ports: Variable length argument list of port numbers (as integers) to clean up.
    """
    for port in ports:
        subprocess.run(  # noqa: S603
            ['fuser', '-k', f'{port}/tcp'],  # noqa: S607
            capture_output=True,
            check=False,
        )


async def start_notification_server(
    port: int, test_name: str
) -> subprocess.Popen:
    """Starts the mock notification server and waits for readiness."""
    _clean_ports(port)
    logger.info('Starting notification server on port %s...', port)

    cwd = os.path.dirname(os.path.abspath(__file__))

    log_level = os.environ.get('ITK_LOG_LEVEL', 'INFO').upper()
    stdout_target = subprocess.PIPE
    stderr_target = subprocess.PIPE

    if log_level == 'DEBUG':
        logs_dir = os.path.join(cwd, 'logs')
        os.makedirs(logs_dir, exist_ok=True)
        log_file = os.path.join(logs_dir, f'{test_name}_notifications.log')
        stdout_target = open(log_file, 'w')
        stderr_target = subprocess.STDOUT
        logger.info('Notification server logging to %s', log_file)

    proc = subprocess.Popen(
        [
            'uv',
            'run',
            'uvicorn',
            'notifications_app:create_notifications_app',
            '--factory',
            '--host',
            '127.0.0.1',
            '--port',
            str(port),
        ],
        cwd=cwd,
        stdout=stdout_target,
        stderr=stderr_target,
        text=True,
    )

    url = f'http://127.0.0.1:{port}'
    async with httpx.AsyncClient() as client:
        for _ in range(10):
            try:
                resp = await client.get(f'{url}/health')
                if resp.status_code == 200:
                    logger.info('Notification server is ready.')
                    break
            except Exception:
                await asyncio.sleep(0.5)
        else:
            proc.terminate()
            stdout, stderr = proc.communicate()
            logger.error(
                'Notification server failed to start. Stdout:\n%s\nStderr:\n%s',
                stdout,
                stderr,
            )
            raise RuntimeError('Notification server failed to start.')

    return proc


def _create_payload(
    is_v0: bool,
    test_instruction: instruction_pb2.Instruction,
    streaming: bool = False,
) -> dict:
    """Creates the JSON-RPC payload for the test instruction."""
    inst_bytes = test_instruction.SerializeToString()
    b64_inst = base64.b64encode(inst_bytes).decode('utf-8')

    if is_v0:
        method = 'message/stream' if streaming else 'message/send'
        params = {
            'message': {
                'role': 'user',
                'messageId': str(uuid.uuid4()),
                'parts': [
                    {
                        'kind': 'file',
                        'file': {
                            'bytes': b64_inst,
                            'mimeType': 'application/x-protobuf',
                            'name': 'instruction.bin',
                        },
                    }
                ],
                'metadata': {'a2a/protocol_version': '0.3'},
            }
        }
    else:
        method = 'SendStreamingMessage' if streaming else 'SendMessage'
        params = {
            'message': {
                'role': 'ROLE_USER',
                'messageId': str(uuid.uuid4()),
                'parts': [
                    {
                        'raw': b64_inst,
                        'mediaType': 'application/x-protobuf',
                        'filename': 'instruction.bin',
                    }
                ],
            }
        }

    return {
        'jsonrpc': '2.0',
        'method': method,
        'params': params,
        'id': str(uuid.uuid4()),
    }


def _extract_response_text(result: dict) -> str:
    """Extracts the response text from the JSON-RPC result."""
    responses = []

    def extract_from_parts(msg_data):
        if 'parts' in msg_data:
            for part in msg_data['parts']:
                if 'text' in part and part['text']:
                    responses.append(part['text'])

    message_data = None
    if 'parts' in result:
        message_data = result
    elif 'message' in result:
        message_data = result['message']
    elif 'status' in result and 'message' in result['status']:
        message_data = result['status']['message']
    elif (
        'task' in result
        and 'status' in result['task']
        and 'message' in result['task']['status']
    ):
        message_data = result['task']['status']['message']

    if message_data:
        extract_from_parts(message_data)

    # Extract from history if present
    history = []
    if 'history' in result:
        history = result['history']
    elif 'task' in result and 'history' in result['task']:
        history = result['task']['history']

    for msg in history:
        if msg.get('role') in ('agent', 'ROLE_AGENT'):
            extract_from_parts(msg)

    return ''.join(responses).strip()


def _extract_text_from_parts(message_data: dict) -> list[str]:
    texts = []
    if 'parts' in message_data:
        for part in message_data['parts']:
            if 'text' in part and part['text']:
                texts.append(part['text'])
    return texts


def _read_v10_notif(event: dict) -> list[str]:
    # v1.0 notifications with agent messages should be in 'statusUpdate'
    update = event.get('statusUpdate') or event.get('status_update')
    if update and isinstance(update, dict) and 'status' in update:
        status = update['status']
        if isinstance(status, dict) and 'message' in status:
            message_data = status['message']
            if message_data and message_data.get('role') == 'ROLE_AGENT':
                return _extract_text_from_parts(message_data)
    return []


def _read_v03_notif(event: dict) -> list[str]:
    # v0.3 notifications have flat structure, agent messages in 'status.message'
    status_obj = event.get('status')
    if status_obj and isinstance(status_obj, dict) and 'message' in status_obj:
        message_data = status_obj['message']
        if message_data and message_data.get('role') == 'agent':
            return _extract_text_from_parts(message_data)
    return []


async def read_push_notifications(
    notification_server_url: str,
) -> list[str]:
    """Reads all push notifications from the mock notification server."""
    url = f'{notification_server_url}/notifications'
    responses = []

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url)
            if response.status_code == 200:
                data = response.json()
                notifications = data.get('notifications', [])
                for notif in notifications:
                    event = notif.get('event', {})
                    texts = _read_v10_notif(event)
                    if not texts:
                        texts = _read_v03_notif(event)
                    responses.extend(texts)
        except Exception as e:
            logger.debug('Error reading notifications: %s', e)

    return responses


def _verify_send_message(
    result: dict,
    expected_end_tokens: list[str],
    label: str,
) -> bool:
    full_response = _extract_response_text(result)

    logger.info('Test Result for %s: %s', label, full_response)

    if all(token in full_response for token in expected_end_tokens):
        logger.info('--- INTEGRATION TEST PASSED: %s ---', label)
        return True

    logger.error(
        '--- INTEGRATION TEST FAILED: Verification tokens missing for %s ---',
        label,
    )
    return False


async def _verify_push_notification(
    notification_texts: list[str],
    expected_end_tokens: list[str],
    label: str,
) -> bool:
    full_response = ''.join(notification_texts)

    # Verify intermediate states to ensure every hop pushed
    # Split expected_end_tokens into chains by terminal token
    chains = []
    current_chain = []
    for token in expected_end_tokens:
        current_chain.append(token)
        if token.startswith('traversal-completed:'):
            chains.append(current_chain)
            current_chain = []
    if current_chain:
        chains.append(current_chain)

    expected_states = []
    for chain in chains:
        if not chain:
            continue
        terminal_token = chain[-1]
        expected_states.append(terminal_token)
        current_state = terminal_token
        for token in chain[:-1]:
            current_state = f'{token}\n{current_state}'
            expected_states.append(current_state)

    logger.info('Expected intermediate states: %s', expected_states)

    remaining_states = list(expected_states)
    for text in notification_texts:
        for state in list(remaining_states):
            if state in text:
                remaining_states.remove(state)

    if remaining_states:
        logger.error(
            '--- INTEGRATION TEST FAILED: Missing intermediate states: %s ---',
            remaining_states,
        )
        return False

    logger.info('Test Result for %s: %s', label, full_response)

    if all(token in full_response for token in expected_end_tokens):
        logger.info('--- INTEGRATION TEST PASSED: %s ---', label)
        return True

    logger.error(
        '--- INTEGRATION TEST FAILED: Verification tokens missing for %s ---',
        label,
    )
    return False


async def _read_stream_response(
    http_client: httpx.AsyncClient,
    target_url: str,
    json_rpc_request: dict,
    headers: dict,
    is_v0: bool,
    expected_end_tokens: list[str],
) -> dict:
    """Reads a streaming response using SSE and aggregates results."""
    logger.info('Starting streaming request to agent...')
    logger.info('POST %s payload=%s headers=%s', target_url, json.dumps(json_rpc_request, indent=2), headers)
    collected_text = []
    async with aconnect_sse(
        http_client, 'POST', target_url, json=json_rpc_request, headers=headers
    ) as event_source:
        logger.info(
            'SSE response: status=%s, content-type=%s, headers=%s',
            event_source.response.status_code,
            event_source.response.headers.get('content-type'),
            dict(event_source.response.headers),
        )
        async for sse in event_source.aiter_sse():
            logger.info('SSE Event: %s', sse.data)
            try:
                event_data = json.loads(sse.data)
                if 'result' in event_data:
                    res = event_data['result']
                    if is_v0:
                        text = _extract_response_text(res)
                    else:
                        texts = _read_v10_notif(res)
                        text = '\n'.join(texts)

                    if text:
                        collected_text.append(text)

                    # Check if traversal completed!
                    joined_text = '\n'.join(collected_text)
                    if all(
                        token in joined_text for token in expected_end_tokens
                    ):
                        logger.info(
                            'Found all expected tokens in stream, breaking.'
                        )
                        break
            except Exception:
                logger.debug('Failed to parse SSE data', exc_info=True)

    return {
        'status': {'message': {'parts': [{'text': '\n'.join(collected_text)}]}}
    }


async def _read_sync_response(
    http_client: httpx.AsyncClient,
    target_url: str,
    json_rpc_request: dict,
    headers: dict,
) -> dict:
    """Reads a synchronous JSON-RPC response."""
    logger.info('POST %s payload=%s headers=%s', target_url, json.dumps(json_rpc_request, indent=2), headers)
    response = await http_client.post(
        target_url, json=json_rpc_request, headers=headers
    )
    response.raise_for_status()
    response_json = response.json()

    logger.info('!!!!!!!!!!!!Received response: %s!!!!!!!!!!!!!', response_json)

    if 'error' in response_json:
        raise RuntimeError(f'JSON-RPC Error: {response_json["error"]}')

    return response_json.get('result', {})


async def _execute_single_itk_test(  # noqa: PLR0913
    sdks: list[str],
    behavior: str,
    agents: AgentTable,
    edges: list[str] | None = None,
    scenario_name: str | None = None,
    protocols: list[str] | None = None,
    streaming: bool = False,
) -> bool:
    """Executes a traversal test against an ALREADY RUNNING cluster.

    Args:
        sdks: List of SDK identifiers to include in the test.
        behavior: The behavior to test ('send_message' or 'push_notification').
        agents: Where this run's agents are listening.
        edges: Optional custom edges.
        scenario_name: Optional label for logging.
        protocols: Optional list of protocols to test.
        streaming: Whether to use streaming.
    """
    label = scenario_name or 'euler'

    notif_server_process = None
    notif_port = None
    notification_server_url = ''
    if behavior == 'push_notification':
        notif_port = _get_free_port()
        notification_server_url = f'http://127.0.0.1:{notif_port}'
        notif_server_process = await start_notification_server(
            notif_port, label
        )
        logger.info(
            'Started dedicated notification server on port %s for test %s',
            notif_port,
            label,
        )
    test_result = False
    try:
        (
            test_instruction,
            expected_end_tokens,
        ) = test_suite.create_test_suite(
            sdks,
            agents,
            edges=edges,
            protocols=protocols,
            streaming=streaming,
            behavior=behavior,
            notification_server_url=notification_server_url,
        )

        logger.info('Executing %s traversal test...', label)
        logger.info('Test instruction: %s', test_instruction)
        first_sdk = sdks[0]
        is_v0 = 'v03' in first_sdk

        base_uri = agents.card_uri(first_sdk)
        target_url = f'{base_uri.rstrip("/")}/jsonrpc'
        is_go_env = os.path.exists('/app/agents/repo/itk/go.mod') or os.path.exists(
            'agents/repo/itk/go.mod'
        )
        is_rust_env = os.path.exists('/app/agents/repo/itk/Cargo.toml') or os.path.exists(
            'agents/repo/itk/Cargo.toml'
        )
        if (
            'go' in first_sdk
            or first_sdk.startswith('rust_')
            or (first_sdk == 'current' and (is_go_env or is_rust_env))
        ):
            target_url = target_url.rstrip('/')
        else:
            target_url = target_url.rstrip('/') + '/'

        json_rpc_request = _create_payload(is_v0, test_instruction, streaming)

        test_token = str(uuid.uuid4())

        if behavior == 'push_notification' and notification_server_url:
            config = {
                'url': f'{notification_server_url}/notifications',
                'token': test_token,
            }
            logger.info(
                'SETTING CONFIG: is_v0=%s, first_sdk=%s', is_v0, first_sdk
            )
            if is_v0:
                json_rpc_request['params']['configuration'] = {
                    'pushNotificationConfig': config
                }
            else:
                json_rpc_request['params']['configuration'] = {
                    'taskPushNotificationConfig': config
                }
            logger.info(
                'PAYLOAD CONFIG: %s',
                json_rpc_request['params']['configuration'],
            )

        headers = {}
        if is_v0:
            headers['A2A-Version'] = '0.3'
        else:
            headers['A2A-Version'] = '1.0'

        async with httpx.AsyncClient(timeout=httpx.Timeout(15, read=120)) as http_client:
            if streaming:
                result = await _read_stream_response(
                    http_client,
                    target_url,
                    json_rpc_request,
                    headers,
                    is_v0,
                    expected_end_tokens,
                )
            else:
                result = await _read_sync_response(
                    http_client, target_url, json_rpc_request, headers
                )

            if behavior == 'push_notification':
                notification_texts = await read_push_notifications(
                    notification_server_url
                )
                test_result = await _verify_push_notification(
                    notification_texts, expected_end_tokens, label
                )
            elif behavior == 'send_message':
                test_result = _verify_send_message(
                    result, expected_end_tokens, label
                )
            elif behavior == 'resubscribe':
                test_result = _verify_send_message(
                    result,
                    expected_end_tokens,
                    label,
                )
            else:
                raise ValueError(f'Unsupported behavior: {behavior}')
    finally:
        if notif_server_process and notif_port:
            logger.info('Stopping notification server for test %s', label)
            notif_server_process.terminate()
            try:
                notif_server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                notif_server_process.kill()
                notif_server_process.wait(timeout=5)
            if getattr(notif_server_process, 'stdout', None) not in (
                None,
                subprocess.PIPE,
                subprocess.DEVNULL,
            ):
                try:
                    notif_server_process.stdout.close()
                except Exception:
                    pass
            _clean_ports(notif_port)

    return test_result


async def execute_itk_test(  # noqa: PLR0913
    sdks: list[str],
    behavior: str,
    agents: AgentTable,
    edges: list[str] | None = None,
    scenario_name: str | None = None,
    protocols: list[str] | None = None,
    streaming: bool = False,
    build_subtests: bool = False,
) -> dict[str, dict[str, Any]]:
    """Executes a traversal test against an ALREADY RUNNING cluster, optionally expanding subtests."""
    label = scenario_name or 'euler'

    if not build_subtests:
        try:
            res = await _execute_single_itk_test(
                sdks=sdks,
                behavior=behavior,
                agents=agents,
                edges=edges,
                scenario_name=label,
                protocols=protocols,
                streaming=streaming,
            )
        except Exception as e:
            logger.exception('Test %s failed with exception: %s', label, e)
            res = False
        return {label: {'passed': res, 'sdks': sdks, 'edges': edges}}

    from test_suite import _get_valid_subgraphs

    subgraphs = _get_valid_subgraphs(
        sdks=sdks,
        edges=edges,
        behavior=behavior,
        agents=agents,
        protocols=protocols,
        streaming=streaming,
    )

    results = []
    subtest_names = []
    subtest_sdks = []
    subtest_edges = []

    logger.info('Running %d subtests for scenario %s sequentially...', len(subgraphs), label)
    for subgraph in subgraphs:
        sub_sdks = subgraph['sdks']
        sub_edges = subgraph['edges']

        if len(sub_sdks) == len(sdks):
            sub_name = label
        else:
            sub_name = f"{label}-sub-{'-'.join(sub_sdks)}"

        subtest_names.append(sub_name)
        subtest_sdks.append(sub_sdks)
        subtest_edges.append(sub_edges)

        try:
            passed = await _execute_single_itk_test(
                sdks=sub_sdks,
                behavior=behavior,
                agents=agents,
                edges=sub_edges,
                scenario_name=sub_name,
                protocols=protocols,
                streaming=streaming,
            )
        except Exception as e:
            logger.exception('Subtest %s failed with exception: %s', sub_name, e)
            passed = False
        results.append(passed)

    res_map = {}
    for name, passed, s_sdks, s_edges in zip(subtest_names, results, subtest_sdks, subtest_edges, strict=True):
        res_map[name] = {
            'passed': passed,
            'sdks': s_sdks,
            'edges': s_edges,
        }
    return res_map
