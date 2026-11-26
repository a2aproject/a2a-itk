"""The instruction contract every SDK's ITK agent is built against.

An agent reads an instruction with stubs generated from
`protos/instruction.proto`, or with a parser of its own that goes by field
number and type. An agent that is already built therefore reads a field
wrongly once its number, its type or its place in a oneof changes. A new field
takes a new number, and this file is where the contract is written down.
"""

from __future__ import annotations

from google.protobuf.descriptor import FieldDescriptor

from pyproto import instruction_pb2


#: Every message, with its fields as the proto declares them, in number order.
MESSAGES = {
    'Instruction': [
        'oneof step: CallAgent call_agent = 1',
        'oneof step: ReturnResponse return_response = 2',
        'oneof step: SeriesOfSteps steps = 3',
    ],
    'CallAgent': [
        'string transport = 1',
        'string agent_card_uri = 2',
        'Instruction instruction = 3',
        'bool streaming = 4',
        'oneof behavior: SendMessageBehavior send_message = 5',
        'oneof behavior: PushNotificationBehavior push_notification = 6',
        'oneof behavior: ResubscribeBehavior resubscribe = 7',
        'oneof behavior: LiveMessageBehavior live_message = 8',
        'oneof behavior: UserArtifactsBehavior user_artifacts = 9',
        'oneof behavior: MultiTurnBehavior multi_turn = 10',
        'oneof behavior: LongPollBehavior long_poll = 11',
        # Applies to every behavior, so it is not one of them.
        'string protocol_version = 12',
    ],
    'ReturnResponse': [
        'string response = 1',
        'bool hold_task = 2',
        'bool report_protocol_version = 3',
        'int32 hold_ms = 4',
        'bool await_input = 5',
    ],
    'SeriesOfSteps': [
        'repeated Instruction instructions = 1',
        'ResponseGenerator response_generator = 2',
    ],
    'SendMessageBehavior': [],
    'PushNotificationBehavior': ['string url = 1', 'string protocol_version = 2'],
    'ResubscribeBehavior': ['bool from_last_seen_generation = 1'],
    'LiveMessageBehavior': ['int32 follow_ups = 1', 'bool foreign_task_request = 2'],
    'UserArtifactsBehavior': ['string text = 1'],
    'MultiTurnBehavior': ['bool stale_attempt = 1'],
    'LongPollBehavior': [],
}

RESPONSE_GENERATORS = {
    'RESPONSE_GENERATOR_UNSPECIFIED': 0,
    'RESPONSE_GENERATOR_CONCAT': 1,
}

_SCALARS = {
    FieldDescriptor.TYPE_STRING: 'string',
    FieldDescriptor.TYPE_BOOL: 'bool',
    FieldDescriptor.TYPE_INT32: 'int32',
}


def _declaration(field: FieldDescriptor) -> str:
    """A field as the proto declares it, with the oneof it belongs to."""
    named = field.message_type or field.enum_type
    words = [named.name if named else _SCALARS[field.type], field.name]
    if field.is_repeated:
        words.insert(0, 'repeated')
    if field.containing_oneof:
        words.insert(0, f'oneof {field.containing_oneof.name}:')
    return f'{" ".join(words)} = {field.number}'


def test_every_field_is_declared_as_agents_were_built_to_read_it():
    messages = instruction_pb2.DESCRIPTOR.message_types_by_name
    assert {
        name: [
            _declaration(field)
            for field in sorted(message.fields, key=lambda field: field.number)
        ]
        for name, message in messages.items()
    } == MESSAGES


def test_every_response_generator_keeps_its_value():
    generator = instruction_pb2.SeriesOfSteps.ResponseGenerator.DESCRIPTOR
    assert {value.name: value.number for value in generator.values} == RESPONSE_GENERATORS
