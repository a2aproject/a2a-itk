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

    def params_sent_to(self, agent: MockAgent, method: str) -> list[dict[str, Any]]:
        """The params of every call of ``method`` an agent received."""
        return [
            body['params']
            for body in (
                json.loads(request.content)
                for request in self.requests
                if request.method == 'POST' and str(request.url).startswith(agent.jsonrpc_url)
            )
            if body['method'] == method
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
    hop_protocol_version: str | None = None,
    calls: dict[str, list[tuple[str, str]]] | None = None,
    build_subtests: bool = False,
    sent: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
) -> bool:
    """One traversal through `testlib.execute_itk_test`. Did it pass?

    The two things `testlib` reaches outside the process for are replaced:
    its HTTP client is one on the cluster's network, and the notification
    receiver it starts is the real application, served on that network.

    ``calls``, when given, is filled with the JSON-RPC calls each agent
    received: method and A2A version, in order. ``sent``, when given, is
    filled with the params of those calls, by agent and then by method. With
    ``build_subtests`` the scenario is run once for every subset of its
    agents, and all have to pass.
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
                hop_protocol_version=hop_protocol_version,
                build_subtests=build_subtests,
            )
        if calls is not None:
            calls.update({
                name: cluster.network.calls_to(agent) for name, agent in cluster.agents.items()
            })
        if sent is not None:
            for name, agent in cluster.agents.items():
                methods = {method for method, _ in cluster.network.calls_to(agent)}
                sent[name] = {m: cluster.network.params_sent_to(agent, m) for m in methods}
        return all(result['passed'] for result in results.values())

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
    cluster: _Cluster, agent: str, method: str, params: dict[str, Any], version: str = '1.0'
) -> dict[str, Any]:
    """One JSON-RPC call to an agent; its result."""
    reply = await cluster.network.client().post(
        cluster.agents[agent].jsonrpc_url,
        headers={VERSION_HEADER: version},
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


#: Three agents: two that speak A2A 1.1 and one that does not.
MIXED = {'current': ('1.0', '1.1'), 'python_v11': ('1.0', '1.1'), 'ts_v10': ('1.0',)}


def versions_received(calls: dict[str, list[tuple[str, str]]]) -> dict[str, set[str]]:
    return {name: {version for _, version in received} for name, received in calls.items()}


class TestTheVersionOfEachCall:
    """An agent that speaks A2A 1.1 reports the version it is called at, and
    the harness checks it. `traverse` passes only if every report is the
    version the harness expected."""

    @pytest.mark.parametrize(('behavior', 'streaming'), [
        ('send_message', False),
        ('send_message', True),
        ('push_notification', False),
        ('resubscribe', True),
    ])
    def test_a_traversal_among_agents_of_mixed_versions_passes(self, behavior, streaming):
        calls: dict[str, list[tuple[str, str]]] = {}
        assert traverse(behavior, streaming=streaming, versions=MIXED, calls=calls)
        # The two newer agents call each other at 1.1, and the harness calls
        # the entry agent at 1.1. The older agent calls and is called at 1.0.
        assert versions_received(calls) == {
            'current': {'1.0', '1.1'},
            'python_v11': {'1.0', '1.1'},
            'ts_v10': {'1.0'},
        }

    def test_the_first_request_is_sent_at_the_newest_version_of_the_entry_agent(self):
        calls: dict[str, list[tuple[str, str]]] = {}
        assert traverse('send_message', versions=MIXED, calls=calls)
        assert calls['current'][0] == ('SendMessage', '1.1')

    def test_a_forced_version_is_the_version_of_every_call_between_newer_agents(self):
        calls: dict[str, list[tuple[str, str]]] = {}
        versions = dict.fromkeys(SDKS, ('1.0', '1.1'))
        assert traverse('send_message', versions=versions, hop_protocol_version='1.0', calls=calls)
        # Only the harness still calls at 1.1: nothing forces its request.
        assert calls['current'] == [
            ('SendMessage', '1.1'), ('SendMessage', '1.0'), ('SendMessage', '1.0'),
        ]
        assert versions_received(calls) == {
            'current': {'1.0', '1.1'}, 'python_v10': {'1.0'}, 'ts_v10': {'1.0'},
        }

    def test_a_forced_version_reaches_every_subtest_of_a_scenario(self):
        calls: dict[str, list[tuple[str, str]]] = {}
        versions = dict.fromkeys(SDKS, ('1.0', '1.1'))
        assert traverse(
            'send_message', versions=versions, hop_protocol_version='1.0',
            build_subtests=True, calls=calls,
        )
        # The harness opens each of the three subtests at 1.1. No agent does.
        assert [version for _, version in calls['current']].count('1.1') == 3
        assert versions_received(calls) == {
            'current': {'1.0', '1.1'}, 'python_v10': {'1.0'}, 'ts_v10': {'1.0'},
        }

    def test_without_a_forced_version_the_same_agents_call_each_other_at_1_1(self):
        calls: dict[str, list[tuple[str, str]]] = {}
        assert traverse('send_message', versions=dict.fromkeys(SDKS, ('1.0', '1.1')), calls=calls)
        assert versions_received(calls) == dict.fromkeys(SDKS, {'1.1'})

    def test_an_agent_that_reports_another_version_is_caught(self, caplog):
        def lie(agents: dict[str, MockAgent]) -> None:
            agent = agents['python_v11']
            carry_out = agent._carry_out
            agent._carry_out = lambda instruction, _served: carry_out(instruction, '1.0')

        assert not traverse('send_message', versions=MIXED, customize=lie)
        assert "missing for traversal: ['[python_v11 -> current (jsonrpc)] served:1.1']" in (
            caplog.text
        )

    def test_an_agent_that_does_not_report_is_caught(self, caplog):
        def stay_silent(agents: dict[str, MockAgent]) -> None:
            agent = agents['python_v11']
            carry_out = agent._carry_out

            async def unreported(instruction: instruction_pb2.Instruction, served: str):
                said = await carry_out(instruction, served)
                return [text.split(' served:')[0] for text in said]

            agent._carry_out = unreported

        assert not traverse('send_message', versions=MIXED, customize=stay_silent)
        # Every token of the agent, and no token of another one.
        assert "'[python_v11 -> ts_v10 (jsonrpc)] served:1.0'" in caplog.text
        assert "'[ts_v10 -> " not in caplog.text

    def test_an_agent_that_calls_at_an_older_version_than_it_could_is_caught(self, caplog):
        """Its own reports are right. The agents it calls report the gap."""
        def fall_back(agents: dict[str, MockAgent]) -> None:
            agent = agents['current']
            interface_of = agent._interface_of

            async def at_1_0(call: instruction_pb2.CallAgent):
                url, _ = await interface_of(call)
                return url, '1.0'

            agent._interface_of = at_1_0

        assert not traverse('send_message', versions=MIXED, customize=fall_back)
        assert "missing for traversal: ['[python_v11 -> current (jsonrpc)] served:1.1']" in (
            caplog.text
        )

    def test_an_agent_that_ignores_a_forced_version_is_caught(self, caplog):
        def ignore_it(agents: dict[str, MockAgent]) -> None:
            agent = agents['python_v10']
            interface_of = agent._interface_of

            async def negotiated(call: instruction_pb2.CallAgent):
                unforced = instruction_pb2.CallAgent()
                unforced.CopyFrom(call)
                unforced.ClearField('protocol_version')
                return await interface_of(unforced)

            agent._interface_of = negotiated

        versions = dict.fromkeys(SDKS, ('1.0', '1.1'))
        assert not traverse(
            'send_message', versions=versions, hop_protocol_version='1.0', customize=ignore_it
        )
        # What the agents it calls say: they were called at 1.1.
        assert "'[ts_v10 -> current (jsonrpc)] served:1.0'" in caplog.text

    def test_an_older_agent_that_passes_the_fields_on_is_enough(self):
        """The older agent is asked nothing, and stands before the newer one."""
        versions = {'ts_v10': ('1.0',), 'python_v11': ('1.0', '1.1')}
        assert traverse('send_message', versions=versions)

    def test_an_agent_that_drops_the_fields_it_passes_on_is_caught(self, caplog):
        """An agent built without the fields loses them when it writes the
        instruction for the next agent. That agent is then never asked to
        report, and the traversal fails at its token."""
        def drop_what_it_does_not_know(agents: dict[str, MockAgent]) -> None:
            agent = agents['ts_v10']
            call_agent = agent._call_agent

            def without_version_fields(instruction: instruction_pb2.Instruction) -> None:
                step = instruction.WhichOneof('step')
                if step == 'return_response':
                    instruction.return_response.ClearField('report_protocol_version')
                elif step == 'steps':
                    for child in instruction.steps.instructions:
                        without_version_fields(child)
                elif step == 'call_agent':
                    instruction.call_agent.ClearField('protocol_version')
                    without_version_fields(instruction.call_agent.instruction)

            async def relay(call: instruction_pb2.CallAgent):
                known = instruction_pb2.CallAgent()
                known.CopyFrom(call)
                without_version_fields(known.instruction)
                return await call_agent(known)

            agent._call_agent = relay

        versions = {'ts_v10': ('1.0',), 'python_v11': ('1.0', '1.1')}
        assert not traverse('send_message', versions=versions, customize=drop_what_it_does_not_know)
        assert "missing for traversal: ['[python_v11 -> ts_v10 (jsonrpc)] served:1.0']" in (
            caplog.text
        )

    def test_a_version_the_called_agent_does_not_serve_fails_the_traversal(self):
        """The call is not made at another version in its place."""
        versions = {'current': ('1.0', '1.1'), 'python_v11': ('1.0', '1.1'), 'only_1_1': ('1.1',)}
        calls: dict[str, list[tuple[str, str]]] = {}
        assert not traverse(
            'send_message', versions=versions, hop_protocol_version='1.0', calls=calls
        )
        assert calls['only_1_1'] == []


class TestARelayChain:
    """A peer starts the traversal (`roles.entry`), and the SUT relays."""

    def test_the_harness_talks_to_the_entry_peer_at_the_version_it_speaks(self):
        calls: dict[str, list[tuple[str, str]]] = {}
        relay = {'python_v10': ('1.0',), 'current': ('1.0', '1.1'), 'go_v10': ('1.0',)}
        assert traverse(
            'send_message', versions=relay, edges=['0->1', '1->2', '2->0'], calls=calls,
        )
        assert calls['python_v10'][0] == ('SendMessage', '1.0')
        assert versions_received(calls) == {
            'python_v10': {'1.0'}, 'current': {'1.0'}, 'go_v10': {'1.0'},
        }


class TestWhatACallAt11Asks:
    """A call made at A2A 1.1 names its version for the push notification
    config, and can resume a subscription from a generation. The harness asks
    an agent for either only on such a call."""

    def test_a_push_config_names_1_1_only_where_the_call_is_made_at_1_1(self):
        sent: dict[str, dict[str, list[dict[str, Any]]]] = {}
        assert traverse('push_notification', versions=MIXED, sent=sent)

        def named(agent: str) -> list[str]:
            return sorted(
                params['configuration']['taskPushNotificationConfig'].get('protocolVersion', '')
                for params in sent[agent]['SendMessage']
            )

        # Each agent is called twice: by the harness or the other newer agent,
        # and by the older agent, whose config names no version.
        assert named('python_v11') == ['', '1.1']
        assert named('ts_v10') == ['', '']
        # The harness opens the traversal itself, and names no version.
        assert named('current') == ['', '', '1.1']

    @pytest.mark.parametrize(('behavior', 'resumed'), [
        ('resubscribe_from_generation', True),
        ('resubscribe', False),
    ])
    def test_a_subscription_is_resumed_only_when_the_behavior_asks(self, behavior, resumed):
        sent: dict[str, dict[str, list[dict[str, Any]]]] = {}
        versions = dict.fromkeys(SDKS, ('1.0', '1.1'))
        assert traverse(behavior, streaming=True, versions=versions, sent=sent)
        subscriptions = [
            params for agent in SDKS for params in sent[agent].get('SubscribeToTask', [])
        ]
        assert len(subscriptions) == 6
        assert ['lastSeenGeneration' in params for params in subscriptions] == [resumed] * 6

    def test_a_subscription_to_an_older_agent_is_not_resumed(self):
        sent: dict[str, dict[str, list[dict[str, Any]]]] = {}
        assert traverse(
            'resubscribe_from_generation', streaming=True, versions=MIXED, sent=sent
        )
        resumed = {
            agent: sorted('lastSeenGeneration' in p for p in sent[agent]['SubscribeToTask'])
            for agent in MIXED
        }
        # Each newer agent is subscribed to twice: once by the other newer
        # agent, which resumes, and once by the older one, which does not.
        assert resumed == {
            'current': [False, True], 'python_v11': [False, True], 'ts_v10': [False, False],
        }

    def test_a_resumed_subscription_may_be_answered_with_the_task_again(self):
        """An agent may ignore the generation and open with the task. The
        traversal reaches the end of every task either way."""
        def start_over(agents: dict[str, MockAgent]) -> None:
            for agent in agents.values():
                subscribe = agent.subscribe_to_task

                def from_the_start(request, version, *, subscribe=subscribe, **kwargs):
                    request.ClearField('last_seen_generation')
                    return subscribe(request, version, **kwargs)

                agent.subscribe_to_task = from_the_start

        versions = dict.fromkeys(SDKS, ('1.0', '1.1'))
        assert traverse(
            'resubscribe_from_generation', streaming=True, versions=versions,
            customize=start_over,
        )

    def test_among_older_agents_it_is_plain_resubscribe(self):
        sent: dict[str, dict[str, list[dict[str, Any]]]] = {}
        assert traverse('resubscribe_from_generation', streaming=True, sent=sent)
        assert not [
            params for agent in SDKS for params in sent[agent]['SubscribeToTask']
            if 'lastSeenGeneration' in params
        ]


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

    @pytest.mark.parametrize(('asked_at', 'reply'), [
        ('1.0', 'hello served:1.0'),
        ('1.1', 'hello served:1.1'),
    ])
    def test_a_response_reports_the_version_of_the_request_when_asked(self, asked_at, reply):
        async def scenario(cluster: _Cluster) -> dict[str, Any]:
            return (await call(
                cluster, 'current', 'SendMessage',
                carrying(saying('hello', report_protocol_version=True)), version=asked_at,
            ))['task']

        assert said(in_a_cluster(scenario, {'current': ('1.0', '1.1')})) == (
            'TASK_STATE_COMPLETED', reply
        )

    def test_every_response_of_a_series_reports_and_a_passed_on_one_does_not(self):
        """The version is that of the request the agent itself is answering.
        The agent it calls answers another request, at another version."""
        async def scenario(cluster: _Cluster) -> dict[str, Any]:
            series = instruction_pb2.Instruction()
            series.steps.instructions.extend([
                saying('first', report_protocol_version=True),
                calling(cluster.agents['older'], saying('theirs', report_protocol_version=True)),
                saying('last', report_protocol_version=True),
            ])
            return (await call(
                cluster, 'newer', 'SendMessage', carrying(series), version='1.1'
            ))['task']

        task = in_a_cluster(scenario, {'newer': ('1.0', '1.1'), 'older': ('1.0',)})
        assert said(task) == (
            'TASK_STATE_COMPLETED', 'first served:1.1\ntheirs served:1.0\nlast served:1.1'
        )

    @pytest.mark.parametrize(('forced', 'called_at'), [('', '1.1'), ('1.1', '1.1'), ('1.0', '1.0')])
    def test_a_call_is_made_at_the_version_the_instruction_asks_for(self, forced, called_at):
        async def scenario(cluster: _Cluster) -> list[tuple[str, str]]:
            other = cluster.agents['ts_v10']
            await ask(cluster, 'current', calling(other, saying('x'), protocol_version=forced))
            return cluster.network.calls_to(other)

        versions = {'current': ('1.0', '1.1'), 'ts_v10': ('1.0', '1.1')}
        assert in_a_cluster(scenario, versions) == [('SendMessage', called_at)]

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

    @pytest.mark.parametrize(('asked', 'stored'), [('1.1', '1.1'), ('1.0', '1.0'), ('', '1.1')])
    def test_a_push_config_carries_the_version_the_instruction_names(self, asked, stored):
        """With none named, the called agent takes the version of the call."""
        async def scenario(cluster: _Cluster) -> tuple[dict[str, Any], dict[str, Any]]:
            other = cluster.agents['ts_v10']
            await ask(cluster, 'current', calling(
                other, saying('x'),
                push_notification={'url': 'http://127.0.0.1:7000', 'protocol_version': asked},
            ))
            (params,) = cluster.network.params_sent_to(other, 'SendMessage')
            (task,) = (await call(cluster, 'ts_v10', 'ListTasks', {}))['tasks']
            (config,) = (await call(
                cluster, 'ts_v10', 'ListTaskPushNotificationConfigs', {'taskId': task['id']}
            ))['configs']
            return params['configuration']['taskPushNotificationConfig'], config

        sent, config = in_a_cluster(scenario, {'current': ('1.0', '1.1'), 'ts_v10': ('1.0', '1.1')})
        assert sent.get('protocolVersion', '') == asked
        assert config['protocolVersion'] == stored

    @pytest.mark.parametrize('resume', [False, True])
    def test_a_call_that_subscribes_again_resumes_when_the_instruction_asks(self, resume):
        async def scenario(cluster: _Cluster) -> tuple[dict[str, Any], dict, dict]:
            other = cluster.agents['ts_v10']
            asked = await ask(cluster, 'current', calling(
                other, saying('held', hold_task=True), streaming=True,
                resubscribe={'from_last_seen_generation': resume},
            ))
            (opened,) = cluster.network.params_sent_to(other, 'SendStreamingMessage')
            (subscription,) = cluster.network.params_sent_to(other, 'SubscribeToTask')
            (held,) = (await call(cluster, 'ts_v10', 'ListTasks', {}))['tasks']
            return asked, subscription, held

        asked, subscription, held = in_a_cluster(
            scenario, {'current': ('1.0', '1.1'), 'ts_v10': ('1.0', '1.1')}
        )
        assert said(asked) == ('TASK_STATE_COMPLETED', 'held')
        assert held['status']['state'] == 'TASK_STATE_CANCELED'
        # The generation of the task the first stream opened with. The mock
        # agent creates a task at generation 1, and the message that carries
        # the instruction is its first change.
        assert subscription == (
            {'id': held['id'], 'lastSeenGeneration': '2'} if resume else {'id': held['id']}
        )

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
            'lists no JSON-RPC interface for A2A 1.0',
        ),
        # A version the instruction asks for is not replaced by another one.
        (
            lambda agents: calling(agents['ts_v10'], saying('x'), protocol_version='1.1'),
            'lists no JSON-RPC interface for A2A 1.1',
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
