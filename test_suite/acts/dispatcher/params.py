"""Reshape a step's abstract params into an A2A request message.

An ACTS step's ``params`` is free-form (``{ * text => any }`` in the CDDL) and
names things the way a test author thinks about them. The A2A request messages
sometimes disagree about where a value lives. Two operations need bridging,
and the same bridge serves all three bindings — JSON-RPC, REST and gRPC all
carry the identical request messages, so this is not transport-specific and
must not be duplicated into the adapters.

This is a sibling of :mod:`test_suite.acts.compat`, and follows the same rule:
**every reshaping here is mechanical**, determined by the target message's own
shape, never a guess about what a test meant. The corpus corroborates both of
them — each writes its ``expect.body`` against the reshaped form already, so
the assertions and the request would otherwise disagree with each other.

Anything needing judgement belongs upstream as a spec question, not here.
"""

from __future__ import annotations

from typing import Any, Mapping

from test_suite.acts.schema import Operation


#: ``taskId`` and ``contextId`` are fields of ``Message``, not of
#: ``SendMessageRequest`` — the request carries only ``message``,
#: ``configuration``, ``metadata`` and ``tenant``. Twelve corpus steps pass
#: them beside ``message`` to mean "continue this task", so they are folded
#: into the message where the schema puts them.
MESSAGE_SCOPED_PARAMS = ('taskId', 'contextId')

#: ``create_push_config`` sends ``TaskPushNotificationConfig`` *as* its request
#: message, with ``url`` and ``token`` at the top level beside ``taskId``. The
#: corpus nests those under ``pushNotificationConfig``. That the flattened
#: shape is the intended one is not an inference: the same tests assert
#: ``expect.body: {id: ..., url: ...}`` unnested.
NESTED_PUSH_CONFIG_PARAM = 'pushNotificationConfig'


def adapt(operation: Operation, params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return ``params`` shaped as the operation's A2A request message.

    Pure and non-mutating: the step's own params are left untouched so a
    retry, or a second binding running the same test, sees what the test
    wrote.
    """
    out = dict(params or {})
    if operation in (Operation.SEND_MESSAGE, Operation.SEND_STREAMING_MESSAGE):
        return _fold_into_message(out)
    if operation is Operation.CREATE_PUSH_CONFIG:
        return _flatten_push_config(out)
    return out


def _fold_into_message(params: dict[str, Any]) -> dict[str, Any]:
    """Move ``taskId`` / ``contextId`` onto the message they qualify."""
    message = params.get('message')
    if not isinstance(message, dict):
        # No message to fold into. Leave it alone and let the SUT reject it:
        # a send_message without a message is a test asserting exactly that.
        return params

    folded = dict(message)
    for name in MESSAGE_SCOPED_PARAMS:
        if name in params:
            # An explicit value on the message wins — the test said it twice
            # and the inner one is the more specific statement.
            folded.setdefault(name, params.pop(name))
    params['message'] = folded
    return params


def _flatten_push_config(params: dict[str, Any]) -> dict[str, Any]:
    """Lift a nested ``pushNotificationConfig`` to the top level."""
    nested = params.pop(NESTED_PUSH_CONFIG_PARAM, None)
    if not isinstance(nested, dict):
        return params
    for key, value in nested.items():
        params.setdefault(key, value)
    return params
