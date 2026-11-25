"""Traversals run from end to end, against agents in this process.

The instruction is built by the traversal compiler, sent and checked by
`testlib`, and carried out by mock agents that call each other over JSON-RPC.
Nothing is started and no socket is opened: every request goes through
:class:`_Network`, and time is virtual.

A traversal that passes here shows that the harness can build an instruction,
that an agent can carry it out, and that the harness tells a traversal that
worked from one that did not. It says nothing about an SDK.
"""

from __future__ import annotations

import asyncio
import functools
import json
import types
import uuid
from collections.abc import Callable
from typing import Any
from unittest import mock

import httpx
import pytest
from google.protobuf.json_format import MessageToDict

import testlib
from notifications_app import create_notifications_app
from pyproto import a2a_pb2 as pb
from pyproto import instruction_pb2
from test_suite.agent_table import AgentEndpoint, AgentTable
from tests.mock_agent import (
    INSTRUCTION_MEDIA_TYPE,
    TASK_FINISHED,
    VERSION_HEADER,
    MockAgent,
    run_virtual,
)


SDKS = ('current', 'python_v10', 'ts_v10')


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


class _Network(httpx.AsyncBaseTransport):
    """Every host of one run, by port."""

    def __init__(self) -> None:
        self.hosts: dict[int, httpx.AsyncBaseTransport] = {}
        #: Every request sent, in order.
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = self.hosts.get(request.url.port)
        if host is None:
            raise httpx.ConnectError('nothing listens there', request=request)
        if request.method == 'GET' and isinstance(host, httpx.ASGITransport):
            # The harness reads the notifications once, as soon as it has its
            # answer. A second on the virtual clock lets every notification
            # that is on its way arrive first.
            await asyncio.sleep(1)
        return await host.handle_async_request(request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self)

    def calls_to(self, agent: MockAgent) -> list[tuple[str, str]]:
        """The JSON-RPC calls an agent received: method and A2A version."""
        return [
            (json.loads(request.content)['method'], request.headers[VERSION_HEADER])
            for request in self.requests
            if request.method == 'POST' and str(request.url).startswith(agent.jsonrpc_url)
        ]


class _Cluster:
    """Mock agents on one network, and the table the harness finds them in."""

    def __init__(self, versions: dict[str, tuple[str, ...]]) -> None:
        self.network = _Network()
        self._client = self.network.client()
        self.table = AgentTable({
            name: AgentEndpoint(8001 + i, 9001 + i, tuple(sorted(served, reverse=True)))
            for i, (name, served) in enumerate(versions.items())
        })
        self.agents = {
            name: MockAgent(
                base_url=self.table.card_uri(name),
                versions=versions[name],
                push_sender=self._push,
                client=self._client,
            )
            for name in versions
        }
        for name, agent in self.agents.items():
            self.network.hosts[self.table[name].http_port] = httpx.MockTransport(agent.handle)

    async def _push(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> None:
        await self._client.post(url, headers=headers, content=json.dumps(body))

    async def aclose(self) -> None:
        for agent in self.agents.values():
            await agent.aclose()
        await self._client.aclose()


def in_a_cluster(
    scenario: Callable[[_Cluster], Any],
    versions: dict[str, tuple[str, ...]] | None = None,
    customize: Callable[[dict[str, MockAgent]], None] | None = None,
) -> Any:
    """Run ``scenario`` against a fresh cluster, on virtual time.

    ``customize`` is handed the agents before the run, to break one.
    """

    async def go() -> Any:
        cluster = _Cluster(versions or dict.fromkeys(SDKS, ('1.0',)))
        if customize is not None:
            customize(cluster.agents)
        try:
            return await scenario(cluster)
        finally:
            await cluster.aclose()

    return run_virtual(go())


def traverse(
    behavior: str,
    *,
    streaming: bool = False,
    protocols: tuple[str, ...] = ('jsonrpc',),
    edges: list[str] | None = None,
    versions: dict[str, tuple[str, ...]] | None = None,
    customize: Callable[[dict[str, MockAgent]], None] | None = None,
) -> bool:
    """One traversal through `testlib.execute_itk_test`. Did it pass?

    The two things `testlib` reaches outside the process for are replaced:
    its HTTP client is one on the cluster's network, and the notification
    receiver it starts is the real application, served on that network.
    """

    async def scenario(cluster: _Cluster) -> bool:
        async def start_receiver(port: int, _name: str) -> mock.Mock:
            cluster.network.hosts[port] = httpx.ASGITransport(app=create_notifications_app())
            return mock.Mock()  # stands in for the process

        on_the_network = types.SimpleNamespace(
            AsyncClient=functools.partial(httpx.AsyncClient, transport=cluster.network),
            Timeout=httpx.Timeout,
        )
        with (
            mock.patch.object(testlib, 'httpx', on_the_network),
            mock.patch.object(testlib, 'start_notification_server', start_receiver),
        ):
            results = await testlib.execute_itk_test(
                sdks=list(cluster.agents),
                behavior=behavior,
                agents=cluster.table,
                edges=edges,
                scenario_name='traversal',
                protocols=list(protocols),
                streaming=streaming,
            )
        return results['traversal']['passed']

    return in_a_cluster(scenario, versions, customize)


def carrying(instruction: instruction_pb2.Instruction) -> dict[str, Any]:
    """The params of a send whose message carries ``instruction``."""
    request = pb.SendMessageRequest()
    request.message.message_id = str(uuid.uuid4())
    request.message.role = pb.ROLE_USER
    request.message.parts.add(
        raw=instruction.SerializeToString(), media_type=INSTRUCTION_MEDIA_TYPE
    )
    return MessageToDict(request)


def saying(text: str, **fields: Any) -> instruction_pb2.Instruction:
    return instruction_pb2.Instruction(
        return_response=instruction_pb2.ReturnResponse(response=text, **fields)
    )


def calling(
    agent: MockAgent, nested: instruction_pb2.Instruction, **fields: Any
) -> instruction_pb2.Instruction:
    return instruction_pb2.Instruction(
        call_agent=instruction_pb2.CallAgent(
            transport='jsonrpc', agent_card_uri=agent.base_url, instruction=nested, **fields
        )
    )


async def call(
    cluster: _Cluster, agent: str, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    """One JSON-RPC call to an agent at A2A 1.0; its result."""
    reply = await cluster.network.client().post(
        cluster.agents[agent].jsonrpc_url,
        headers={VERSION_HEADER: '1.0'},
        json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params},
    )
    return reply.json()['result']


async def ask(
    cluster: _Cluster, agent: str, instruction: instruction_pb2.Instruction
) -> dict[str, Any]:
    """Send ``instruction`` to an agent; the task it answers with."""
    return (await call(cluster, agent, 'SendMessage', carrying(instruction)))['task']


def said(task: dict[str, Any]) -> tuple[str, str]:
    """A task's state and the text of its status message."""
    return task['status']['state'], task['status']['message']['parts'][0]['text']


# ---------------------------------------------------------------------------
# Whole traversals
# ---------------------------------------------------------------------------


class TestATraversalPasses:
    @pytest.mark.parametrize(('behavior', 'streaming'), [
        ('send_message', False),
        ('send_message', True),
        ('push_notification', False),
        ('push_notification', True),
        ('resubscribe', True),
    ])
    def test_over_every_edge_between_three_agents(self, behavior, streaming):
        assert traverse(behavior, streaming=streaming)

    def test_over_edges_a_scenario_names(self):
        assert traverse('send_message', edges=['0->1', '1->0', '0->2', '2->0'])

    def test_between_agents_that_speak_different_versions(self):
        """Each call is made at the newest version both ends speak."""
        seen: dict[str, set[str]] = {}

        async def scenario(cluster: _Cluster) -> None:
            newer, older = cluster.agents['newer'], cluster.agents['older']
            there_and_back = calling(older, calling(newer, saying('done')))
            task = await ask(cluster, 'newer', calling(newer, there_and_back))
            assert said(task) == ('TASK_STATE_COMPLETED', 'done')
            seen.update({
                name: {version for _, version in cluster.network.calls_to(cluster.agents[name])}
                for name in ('newer', 'older')
            })

        in_a_cluster(scenario, {'newer': ('1.0', '1.1'), 'older': ('1.0',)})
        # The test asks at 1.0, the newer agent calls itself at 1.1, and the
        # two agents call each other at 1.0.
        assert seen == {'newer': {'1.0', '1.1'}, 'older': {'1.0'}}


class TestATraversalThatDidNotWorkFails:
    """Each agent here breaks one thing. The harness has to notice."""

    def test_an_agent_that_calls_nobody(self, caplog):
        def break_one(agents: dict[str, MockAgent]) -> None:
            async def call_nobody(_call: instruction_pb2.CallAgent) -> list[str]:
                return []

            agents['python_v10']._call_agent = call_nobody

        assert not traverse('send_message', customize=break_one)
        assert 'Verification tokens missing' in caplog.text

    def test_an_entry_agent_that_pushes_nothing(self, caplog):
        """The reply is complete. Only the notifications show the gap."""
        def break_one(agents: dict[str, MockAgent]) -> None:
            async def into_the_void(*_delivery: Any) -> None:
                pass

            agents['current']._push_sender = into_the_void

        assert not traverse('push_notification', customize=break_one)
        assert 'Missing intermediate states' in caplog.text

    def test_an_agent_that_never_finishes_its_reply(self, caplog):
        """The agent that called it waits, and the traversal runs out of time."""
        def break_one(agents: dict[str, MockAgent]) -> None:
            agent = agents['ts_v10']

            async def never_finish(record: Any, _request: pb.SendMessageRequest) -> None:
                agent._set_status(record, pb.TASK_STATE_WORKING, 'still working')
                await asyncio.Event().wait()

            agent._follow_instruction = never_finish

        assert not traverse('resubscribe', streaming=True, customize=break_one)
        assert 'did not finish within 60s' in caplog.text

    def test_a_transport_the_agents_do_not_call_over(self, caplog):
        assert not traverse('send_message', protocols=('grpc',))
        assert 'Verification tokens missing' in caplog.text


# ---------------------------------------------------------------------------
# One instruction
# ---------------------------------------------------------------------------


class TestAnInstruction:
    def test_a_response_is_the_reply(self):
        async def scenario(cluster: _Cluster) -> dict[str, Any]:
            return await ask(cluster, 'current', saying('hello'))

        assert said(in_a_cluster(scenario)) == ('TASK_STATE_COMPLETED', 'hello')

    def test_steps_are_carried_out_in_order_and_joined_by_newlines(self):
        async def scenario(cluster: _Cluster) -> dict[str, Any]:
            series = instruction_pb2.Instruction()
            series.steps.instructions.extend([
                saying('first'),
                calling(cluster.agents['ts_v10'], saying('from the other agent')),
                saying('last'),
            ])
            return await ask(cluster, 'current', series)

        assert said(in_a_cluster(scenario)) == (
            'TASK_STATE_COMPLETED', 'first\nfrom the other agent\nlast'
        )

    def test_a_held_task_ends_its_reply_and_stays_open_until_canceled(self):
        async def scenario(cluster: _Cluster) -> tuple[dict[str, Any], dict[str, Any]]:
            params = carrying(saying('held', hold_task=True))
            params['configuration'] = {'returnImmediately': True}
            task = (await call(cluster, 'current', 'SendMessage', params))['task']
            await asyncio.sleep(60)
            held = await call(cluster, 'current', 'GetTask', {'id': task['id']})
            return held, await call(cluster, 'current', 'CancelTask', {'id': task['id']})

        held, canceled = in_a_cluster(scenario)
        assert said(held) == ('TASK_STATE_WORKING', f'held\n{TASK_FINISHED}')
        assert canceled['status']['state'] == 'TASK_STATE_CANCELED'

    @pytest.mark.parametrize(('streaming', 'method'), [
        (False, 'SendMessage'),
        (True, 'SendStreamingMessage'),
    ])
    def test_a_call_is_a_stream_when_the_instruction_asks_for_one(self, streaming, method):
        async def scenario(cluster: _Cluster) -> tuple[dict[str, Any], list]:
            other = cluster.agents['ts_v10']
            asked = await ask(cluster, 'current', calling(other, saying('x'), streaming=streaming))
            return asked, cluster.network.calls_to(other)

        asked, calls = in_a_cluster(scenario)
        assert said(asked) == ('TASK_STATE_COMPLETED', 'x')
        assert calls == [(method, '1.0')]

    @pytest.mark.parametrize(('streaming', 'why'), [
        (False, 'SendMessage was refused: A bearer token is required.'),
        (
            True,
            'SendStreamingMessage was not answered with a stream: A bearer token is required.',
        ),
    ])
    def test_a_call_the_other_agent_refuses_fails_the_task(self, streaming, why):
        def lock_one(agents: dict[str, MockAgent]) -> None:
            agents['ts_v10'].require_auth = True

        async def scenario(cluster: _Cluster) -> dict[str, Any]:
            return await ask(cluster, 'current', calling(
                cluster.agents['ts_v10'], saying('x'), streaming=streaming
            ))

        state, text = said(in_a_cluster(scenario, customize=lock_one))
        assert state == 'TASK_STATE_FAILED'
        assert why in text

    def test_a_call_that_subscribes_again_hangs_up_reads_the_held_reply_and_cancels(self):
        async def scenario(cluster: _Cluster) -> tuple[dict[str, Any], list, list]:
            other = cluster.agents['ts_v10']
            asked = await ask(cluster, 'current', calling(
                other, saying('held', hold_task=True), streaming=True, resubscribe={}
            ))
            listed = await call(cluster, 'ts_v10', 'ListTasks', {})
            return asked, cluster.network.calls_to(other)[:3], listed['tasks']

        asked, calls, (held,) = in_a_cluster(scenario)
        # The reply without its end mark. The calling agent holds nothing
        # itself: only what it says counts, not what it passes on.
        assert said(asked) == ('TASK_STATE_COMPLETED', 'held')
        assert [method for method, _ in calls] == [
            'SendStreamingMessage', 'SubscribeToTask', 'CancelTask',
        ]
        assert held['status']['state'] == 'TASK_STATE_CANCELED'

    def test_a_call_with_push_notification_registers_a_config_on_the_called_agent(self):
        async def scenario(cluster: _Cluster) -> list[dict[str, Any]]:
            await ask(cluster, 'current', calling(
                cluster.agents['ts_v10'], saying('x'),
                push_notification={'url': 'http://127.0.0.1:7000'},
            ))
            (task,) = (await call(cluster, 'ts_v10', 'ListTasks', {}))['tasks']
            listed = await call(
                cluster, 'ts_v10', 'ListTaskPushNotificationConfigs', {'taskId': task['id']}
            )
            return listed['configs']

        (config,) = in_a_cluster(scenario)
        assert config['url'] == 'http://127.0.0.1:7000/notifications'
        assert config['token'] == 'itk-token'

    def test_an_agent_given_no_client_fails_a_call(self):
        def take_the_client(agents: dict[str, MockAgent]) -> None:
            agents['current']._client = None

        async def scenario(cluster: _Cluster) -> dict[str, Any]:
            return await ask(cluster, 'current', calling(cluster.agents['ts_v10'], saying('x')))

        state, text = said(in_a_cluster(scenario, customize=take_the_client))
        assert state == 'TASK_STATE_FAILED'
        assert 'was given nothing to call other agents with' in text

    @pytest.mark.parametrize(('build', 'why'), [
        (lambda agents: instruction_pb2.Instruction(), 'names no step'),
        (
            lambda agents: calling(agents['ts_v10'], saying('x'), live_message={}),
            'does not implement live_message',
        ),
        (
            lambda agents: instruction_pb2.Instruction(
                call_agent=instruction_pb2.CallAgent(
                    transport='grpc', agent_card_uri=agents['ts_v10'].base_url
                )
            ),
            'calls over jsonrpc only, not over grpc',
        ),
        (
            lambda agents: calling(agents['only_1_1'], saying('x')),
            'lists no JSON-RPC interface for a version this agent speaks',
        ),
        (
            lambda agents: calling(agents['ts_v10'], saying('x'), resubscribe={}),
            'subscribing again needs a streaming call',
        ),
        (
            lambda agents: calling(agents['ts_v10'], saying('x'), push_notification={}),
            'push_notification names no url',
        ),
        # Nothing serves a card there: the reply is not one.
        (
            lambda agents: instruction_pb2.Instruction(
                call_agent=instruction_pb2.CallAgent(
                    transport='jsonrpc', agent_card_uri=f'{agents["ts_v10"].base_url}/nowhere'
                )
            ),
            'supportedInterfaces',
        ),
    ])
    def test_one_that_cannot_be_carried_out_fails_the_task_and_says_why(self, build, why):
        async def scenario(cluster: _Cluster) -> dict[str, Any]:
            return await ask(cluster, 'current', build(cluster.agents))

        state, text = said(in_a_cluster(
            scenario, {'current': ('1.0',), 'ts_v10': ('1.0',), 'only_1_1': ('1.1',)}
        ))
        assert state == 'TASK_STATE_FAILED'
        assert why in text

    def test_a_part_that_is_not_an_instruction_fails_the_task(self):
        async def scenario(cluster: _Cluster) -> dict[str, Any]:
            params = carrying(saying('x'))
            params['message']['parts'][0]['raw'] = 'AAEC'  # three bytes of nothing
            return (await call(cluster, 'current', 'SendMessage', params))['task']

        state, text = said(in_a_cluster(scenario))
        assert state == 'TASK_STATE_FAILED'
        assert text.startswith('the instruction failed: ')

    def test_a_call_to_an_agent_that_is_not_there_fails_the_task(self):
        async def scenario(cluster: _Cluster) -> dict[str, Any]:
            nobody = MockAgent(base_url='http://127.0.0.1:1')
            return await ask(cluster, 'current', calling(nobody, saying('x')))

        state, text = said(in_a_cluster(scenario))
        assert state == 'TASK_STATE_FAILED'
        assert 'nothing listens there' in text

    def test_a_failed_call_is_reported_by_the_agent_that_made_it(self):
        """What the called agent says travels back like any reply."""
        async def scenario(cluster: _Cluster) -> dict[str, Any]:
            return await ask(
                cluster, 'current',
                calling(cluster.agents['ts_v10'], instruction_pb2.Instruction()),
            )

        state, text = said(in_a_cluster(scenario))
        assert state == 'TASK_STATE_COMPLETED'
        assert 'the instruction names no step' in text

    def test_an_acts_behavior_named_in_the_message_is_played_instead(self):
        """One agent serves both suites, and ACTS goes first."""
        async def scenario(cluster: _Cluster) -> dict[str, Any]:
            params = carrying(saying('from the instruction'))
            params['message']['parts'].insert(0, {'text': 'tck-complete-task'})
            return (await call(cluster, 'current', 'SendMessage', params))['task']

        assert said(in_a_cluster(scenario)) == (
            'TASK_STATE_COMPLETED', 'tck-complete-task completed'
        )
