"""What the traversal compiler puts into an instruction about A2A versions.

Only an agent that speaks A2A 1.1 reads the fields of an instruction that are
about the version of a call. The compiler therefore sets them only for such an
agent, and a traversal among older agents is the instruction it was before the
fields existed.
"""

from __future__ import annotations

import hashlib
import re

import pytest

import test_suite
from pyproto import instruction_pb2
from test_suite.agent_table import AgentEndpoint, AgentTable


V11 = ('1.1', '1.0', '0.3')
V10 = ('1.0', '0.3')
V03 = ('0.3',)


def table(**versions: tuple[str, ...]) -> AgentTable:
    return AgentTable({
        name: AgentEndpoint(8001 + i, 9001 + i, served)
        for i, (name, served) in enumerate(versions.items())
    })


def build(agents: AgentTable, **kwargs) -> tuple[instruction_pb2.Instruction, list[str]]:
    """One circuit over JSON-RPC: the instruction for it, and its tokens."""
    kwargs.setdefault('protocols', ['jsonrpc'])
    whole, tokens = test_suite.create_test_suite(list(agents), agents, **kwargs)
    (circuit,) = whole.steps.instructions
    return circuit, tokens


def hops(instruction: instruction_pb2.Instruction) -> list[dict]:
    """Every hop of a circuit, outermost first.

    For each: the agent that calls and the one it calls, the version the call
    is forced to, and whether the calling agent is asked to report the version
    it was itself called at. The last entry is the end of the traversal.
    """
    found = []
    while instruction.WhichOneof('step') == 'steps':
        said, call = instruction.steps.instructions
        caller, callee = re.fullmatch(
            r'\[(\S+) -> (\S+) \(\w+\)\]', said.return_response.response
        ).groups()
        found.append({
            'hop': f'{caller}->{callee}',
            'forced': call.call_agent.protocol_version,
            'reports': said.return_response.report_protocol_version,
        })
        instruction = call.call_agent.instruction
    found.append({'end': True, 'reports': instruction.return_response.report_protocol_version})
    return found


class TestAgentsThatSpeakNothingNewer:
    """What an agent that speaks A2A 1.0 or 0.3 is sent does not change."""

    #: sha256 of the instruction compiled for each scenario, taken before the
    #: compiler knew about versions.
    BEFORE = {
        'send_message': '40762650a6abb6d1891a55022ea88047c5db7710ca9344c6720b3d318fc0429b',
        'send_message_streaming': (
            '27faaa83d720ddbac16949dfac551d3abf14c449f0cc463df7cc7fc2d22427d3'
        ),
        'send_message_star': 'd75dbb2a3f4fe6dcbc54b6b90fd76c986aee007003cdf2e7a43d551af123d165',
        'push_notification': '2f5c1a54b26ca1b6e38b3657fce4793ea5f4ef611bf2a1385c43391158cf1f1f',
        'resubscribe': '2955279c3b1e11f63de07bee71f48256564323d450f53e3150e42335ce065e0c',
    }
    # Resuming needs A2A 1.1. Among older agents it is `resubscribe`.
    BEFORE['resubscribe_from_generation'] = BEFORE['resubscribe']
    ALL = ['jsonrpc', 'grpc', 'http_json']
    SCENARIOS = {
        'send_message': {'protocols': ALL},
        'send_message_streaming': {'protocols': ALL, 'streaming': True},
        'send_message_star': {'protocols': ['jsonrpc'], 'edges': ['0->1', '1->0', '0->2', '2->0']},
        'push_notification': {
            'protocols': ALL,
            'behavior': 'push_notification',
            'notification_server_url': 'http://127.0.0.1:7000',
        },
        'resubscribe': {'protocols': ALL, 'behavior': 'resubscribe', 'streaming': True},
        'resubscribe_from_generation': {
            'protocols': ALL,
            'behavior': 'resubscribe_from_generation',
            'streaming': True,
        },
    }

    @pytest.mark.parametrize('forced', [None, '1.0'])
    @pytest.mark.parametrize('scenario', sorted(SCENARIOS))
    def test_the_instruction_is_byte_for_byte_what_it_was(self, scenario, forced):
        """Forcing a version changes nothing either: none of them reads it."""
        agents = table(current=V10, python_v10=V10, go_v03=V03)
        instruction, _ = test_suite.create_test_suite(
            list(agents), agents, hop_protocol_version=forced, **self.SCENARIOS[scenario]
        )
        compiled = instruction.SerializeToString(deterministic=True)
        assert hashlib.sha256(compiled).hexdigest() == self.BEFORE[scenario]

    def test_no_token_names_a_version(self):
        _, tokens = build(table(current=V10, python_v10=V10, go_v03=V03))
        assert not [token for token in tokens if 'served:' in token]

    def test_beside_a_newer_agent_they_are_asked_nothing_and_pass_its_fields_on(self):
        """The instruction an older agent is handed holds what the agents
        after it are asked. Its own steps have no field about versions."""
        instruction, _ = build(table(current=V10, python_v11=V11), hop_protocol_version='1.0')
        first, second, end = hops(instruction)
        assert first == {'hop': 'current->python_v11', 'forced': '', 'reports': False}
        assert second == {'hop': 'python_v11->current', 'forced': '1.0', 'reports': True}
        assert end == {'end': True, 'reports': False}


class TestReportingTheVersion:
    def test_an_agent_that_speaks_1_1_reports_the_version_it_is_called_at(self):
        """The newest version both ends of the call speak."""
        agents = table(current=V10, python_v11=V11, go_v03=V03)
        instruction, tokens = build(agents, edges=['0->1', '1->0', '1->2', '2->1'])
        assert [h for h in hops(instruction) if h['reports']] == [
            # What python_v11 says when it is called: once by `current`, at
            # 1.0, and once by go_v03, at 0.3.
            {'hop': 'python_v11->go_v03', 'forced': '', 'reports': True},
            {'hop': 'python_v11->current', 'forced': '', 'reports': True},
        ]
        assert sorted(tokens) == sorted([
            '[current -> python_v11 (jsonrpc)]',
            '[python_v11 -> go_v03 (jsonrpc)] served:1.0',
            '[go_v03 -> python_v11 (jsonrpc)]',
            '[python_v11 -> current (jsonrpc)] served:0.3',
            'traversal-completed:jsonrpc',
        ])

    def test_a_token_carries_the_version_its_speaker_was_called_at(self):
        """Not the version of the call the token names. In the circuit
        current, a, b, current the token `[a -> b]` is said by `a`, which
        `current` called at 1.0, though `a` then calls `b` at 1.1."""
        _, tokens = build(
            table(current=V10, a=V11, b=V11), edges=['0->1', '1->2', '2->0']
        )
        assert tokens == [
            '[b -> current (jsonrpc)] served:1.1',
            '[a -> b (jsonrpc)] served:1.0',
            '[current -> a (jsonrpc)]',
            'traversal-completed:jsonrpc',
        ]

    def test_the_agent_the_harness_calls_is_not_asked(self):
        """The harness sends that request itself, and knows its version."""
        agents = table(python_v11=V11, go_v11=V11)
        instruction, tokens = build(agents)
        first, second, end = hops(instruction)
        assert first == {'hop': 'python_v11->go_v11', 'forced': '', 'reports': False}
        assert second == {'hop': 'go_v11->python_v11', 'forced': '', 'reports': True}
        # The traversal ends at the first agent again, called by go_v11.
        assert end == {'end': True, 'reports': True}
        assert tokens == [
            '[go_v11 -> python_v11 (jsonrpc)] served:1.1',
            '[python_v11 -> go_v11 (jsonrpc)]',
            'traversal-completed:jsonrpc served:1.1',
        ]

    def test_the_tokens_keep_their_order(self):
        """Innermost hop first, then the end: the push check relies on it."""
        with_versions = build(table(a=V11, b=V11, c=V11))[1]
        without = build(table(a=V10, b=V10, c=V10))[1]
        assert [token.split(' served:')[0] for token in with_versions] == without

    def test_every_transport_gets_its_own_tokens(self):
        agents = table(a=V11, b=V11)
        _, tokens = test_suite.create_test_suite(
            list(agents), agents, protocols=['jsonrpc', 'grpc']
        )
        assert tokens == [
            '[b -> a (jsonrpc)] served:1.1',
            '[a -> b (jsonrpc)]',
            'traversal-completed:jsonrpc served:1.1',
            '[b -> a (grpc)] served:1.1',
            '[a -> b (grpc)]',
            'traversal-completed:grpc served:1.1',
        ]

    def test_agents_with_no_version_in_common_are_not_given_a_version(self):
        """The call cannot be made. The traversal fails at it, not here."""
        instruction, tokens = build(table(a=('1.1',), b=V03))
        assert [h['reports'] for h in hops(instruction)] == [False, False, False]
        assert not [token for token in tokens if 'served:' in token]


def calls(instruction: instruction_pb2.Instruction) -> dict[str, instruction_pb2.CallAgent]:
    """The call of every hop of a circuit, by `caller->callee`."""
    found = {}
    while instruction.WhichOneof('step') == 'steps':
        said, call = instruction.steps.instructions
        caller, callee = re.fullmatch(
            r'\[(\S+) -> (\S+) \(\w+\)\]', said.return_response.response
        ).groups()
        found[f'{caller}->{callee}'] = call.call_agent
        instruction = call.call_agent.instruction
    return found


class TestWhatACallAt11Asks:
    """Two fields ask the calling agent for something A2A 1.1 added to a
    request. Only a call made at 1.1 can carry it."""

    #: `current` calls `a` at 1.0, `a` calls `b` at 1.1, `b` calls `current` at 1.0.
    AGENTS = {'current': V10, 'a': V11, 'b': V11}
    RING = ['0->1', '1->2', '2->0']

    def test_a_push_config_is_asked_to_name_the_version_of_the_call(self):
        instruction, _ = build(
            table(**self.AGENTS), edges=self.RING,
            behavior='push_notification', notification_server_url='http://127.0.0.1:7000',
        )
        assert {
            hop: call.push_notification.protocol_version
            for hop, call in calls(instruction).items()
        } == {'current->a': '', 'a->b': '1.1', 'b->current': ''}

    def test_the_version_a_push_config_names_is_the_one_of_its_call(self):
        """Not 1.1 as such: two agents that speak a later version call each
        other at it, and the config is for that version."""
        later = ('1.2', '1.1', '1.0')
        instruction, _ = build(
            table(a=later, b=later, c=V11), edges=['0->1', '1->2', '2->0'],
            behavior='push_notification', notification_server_url='http://127.0.0.1:7000',
        )
        assert {
            hop: call.push_notification.protocol_version
            for hop, call in calls(instruction).items()
        } == {'a->b': '1.2', 'b->c': '1.1', 'c->a': '1.1'}

    def test_a_forced_version_is_the_one_a_push_config_names(self):
        """Two agents that would call each other at 1.2 are forced to 1.1."""
        later = ('1.2', '1.1', '1.0')
        instruction, _ = build(
            table(a=later, b=later), hop_protocol_version='1.1',
            behavior='push_notification', notification_server_url='http://127.0.0.1:7000',
        )
        assert {
            hop: (call.protocol_version, call.push_notification.protocol_version)
            for hop, call in calls(instruction).items()
        } == {'a->b': ('1.1', '1.1'), 'b->a': ('1.1', '1.1')}

    @pytest.mark.parametrize('behavior', ['send_message', 'resubscribe'])
    def test_a_forced_version_is_not_read_where_no_field_needs_it(self, behavior):
        """It is passed on as it is. Checking it is the job of whoever sets it."""
        instruction, _ = build(
            table(a=V11, b=V11), hop_protocol_version='latest',
            behavior=behavior, streaming=True,
        )
        assert {call.protocol_version for call in calls(instruction).values()} == {'latest'}

    def test_a_subscription_is_resumed_when_the_behavior_asks_for_it(self):
        instruction, _ = build(
            table(**self.AGENTS), edges=self.RING,
            behavior='resubscribe_from_generation', streaming=True,
        )
        found = calls(instruction)
        assert {
            hop: call.resubscribe.from_last_seen_generation for hop, call in found.items()
        } == {'current->a': False, 'a->b': True, 'b->current': False}
        # Every call subscribes again, resumed or not.
        assert all(call.WhichOneof('behavior') == 'resubscribe' for call in found.values())

    def test_plain_resubscribe_resumes_nothing(self):
        instruction, _ = build(
            table(a=V11, b=V11), behavior='resubscribe', streaming=True
        )
        assert not [
            hop for hop, call in calls(instruction).items()
            if call.resubscribe.from_last_seen_generation
        ]

    @pytest.mark.parametrize(('behavior', 'extra'), [
        ('push_notification', {'notification_server_url': 'http://127.0.0.1:7000'}),
        ('resubscribe_from_generation', {'streaming': True}),
    ])
    def test_a_call_forced_to_an_older_version_asks_for_neither(self, behavior, extra):
        instruction, _ = build(
            table(a=V11, b=V11), behavior=behavior, hop_protocol_version='1.0', **extra
        )
        for call in calls(instruction).values():
            assert call.protocol_version == '1.0'
            assert call.push_notification.protocol_version == ''
            assert not call.resubscribe.from_last_seen_generation


class TestForcingTheVersion:
    def test_a_call_made_by_an_agent_that_speaks_1_1_is_forced(self):
        agents = table(current=V10, python_v11=V11, go_v11=V11)
        instruction, tokens = build(
            agents, hop_protocol_version='1.0', edges=['0->1', '1->2', '2->0']
        )
        assert hops(instruction) == [
            # `current` does not read the field, and negotiates 1.0 anyway.
            {'hop': 'current->python_v11', 'forced': '', 'reports': False},
            {'hop': 'python_v11->go_v11', 'forced': '1.0', 'reports': True},
            {'hop': 'go_v11->current', 'forced': '1.0', 'reports': True},
            {'end': True, 'reports': False},
        ]
        assert tokens == [
            '[go_v11 -> current (jsonrpc)] served:1.0',
            '[python_v11 -> go_v11 (jsonrpc)] served:1.0',
            '[current -> python_v11 (jsonrpc)]',
            'traversal-completed:jsonrpc',
        ]

    def test_a_call_made_by_an_older_agent_keeps_the_version_it_negotiates(self):
        """Forcing 0.3 does not reach `current`, which calls at 1.0."""
        agents = table(python_v11=V11, current=V10)
        instruction, tokens = build(agents, hop_protocol_version='0.3')
        assert [h.get('forced') for h in hops(instruction)] == ['0.3', '', None]
        assert tokens == [
            '[current -> python_v11 (jsonrpc)]',
            '[python_v11 -> current (jsonrpc)]',
            'traversal-completed:jsonrpc served:1.0',
        ]


class TestAnAgentThatIsNotRunning:
    def test_is_still_a_runtime_error(self):
        """Not a ``ValueError``, which the subtest expansion takes for a
        graph that cannot be traversed."""
        agents = table(current=V10)
        with pytest.raises(RuntimeError, match="No running agent 'ghost_v11'") as raised:
            test_suite.create_test_suite(['current', 'ghost_v11'], agents)
        assert not isinstance(raised.value, ValueError)
