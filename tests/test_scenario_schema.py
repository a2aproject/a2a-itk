"""traversal/v1 model validation, and the legacy shape it coexists with."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from test_suite.scenarios.schema import (
    Behavior,
    LegacyScenario,
    PeerRef,
    Tier,
    Transport,
    TraversalScenarioV1,
    is_traversal_v1,
)
from test_suite.scenarios.topology import Topology


def _scenario(**overrides):
    base = {
        'schema': 'traversal/v1',
        'name': 'example',
        'roles': {'sut': 'current', 'peers': [{'sdk': 'go', 'line': 'v10'}]},
        'transports': ['jsonrpc'],
        'behavior': 'send_message',
    }
    base.update(overrides)
    return base


class TestDiscriminator:
    def test_new_format_is_recognised(self):
        assert is_traversal_v1({'schema': 'traversal/v1'}) is True

    def test_legacy_has_no_schema_key(self):
        assert is_traversal_v1({'name': 'x', 'sdks': []}) is False

    def test_a_future_schema_is_not_traversal_v1(self):
        """A future schema rides the same envelope; it must not be
        mistaken for traversal scenarios."""
        assert is_traversal_v1({'schema': 'acts/v1'}) is False


class TestDefaults:
    def test_minimal_scenario(self):
        s = TraversalScenarioV1.model_validate(_scenario())
        assert s.topology is Topology.STAR
        assert s.tier is Tier.NIGHTLY
        assert s.build_subtests is False
        assert s.edges is None
        assert s.roles.include_sut is True

    def test_normalised_accessors_wrap_singulars(self):
        s = TraversalScenarioV1.model_validate(_scenario())
        assert s.behavior_variants() == [Behavior.SEND_MESSAGE]
        assert s.transport_variants() == [[Transport.JSONRPC]]
        assert s.streaming_options() == [False]


class TestPlurals:
    def test_plural_forms_are_returned_as_given(self):
        s = TraversalScenarioV1.model_validate(_scenario(
            behavior=None,
            behaviors=['send_message', 'resubscribe'],
            transports=None,
            transport_sets=[['jsonrpc'], ['grpc', 'http_json']],
            streaming_variants=[False, True],
        ))
        assert s.behavior_variants() == [
            Behavior.SEND_MESSAGE, Behavior.RESUBSCRIBE,
        ]
        assert s.transport_variants() == [
            [Transport.JSONRPC], [Transport.GRPC, Transport.HTTP_JSON],
        ]
        assert s.streaming_options() == [False, True]

    @pytest.mark.parametrize(('singular', 'plural', 'sv', 'pv'), [
        ('behavior', 'behaviors', 'send_message', ['send_message']),
        ('transports', 'transport_sets', ['jsonrpc'], [['jsonrpc']]),
        ('streaming', 'streaming_variants', True, [True]),
    ])
    def test_both_forms_at_once_is_rejected(self, singular, plural, sv, pv):
        """Preferring one silently would make coverage depend on an invisible
        rule."""
        with pytest.raises(ValidationError, match='not both'):
            TraversalScenarioV1.model_validate(
                _scenario(**{singular: sv, plural: pv})
            )

    @pytest.mark.parametrize('field', [
        'behaviors', 'transport_sets', 'streaming_variants',
    ])
    def test_empty_plural_is_rejected(self, field):
        """An empty list expands to zero scenarios — coverage lost in silence."""
        kwargs = {field: []}
        if field == 'behaviors':
            kwargs['behavior'] = None
        if field == 'transport_sets':
            kwargs['transports'] = None
        with pytest.raises(ValidationError, match='must not be empty'):
            TraversalScenarioV1.model_validate(_scenario(**kwargs))


class TestRequiredFields:
    def test_behavior_is_required(self):
        with pytest.raises(ValidationError, match='`behavior` or `behaviors`'):
            TraversalScenarioV1.model_validate(_scenario(behavior=None))

    def test_transports_are_required(self):
        with pytest.raises(ValidationError, match='`transports` or `transport_sets`'):
            TraversalScenarioV1.model_validate(_scenario(transports=None))

    def test_empty_transports_rejected(self):
        with pytest.raises(ValidationError, match='must not be empty'):
            TraversalScenarioV1.model_validate(_scenario(transports=[]))

    def test_unknown_field_is_rejected(self):
        """A typo'd key would otherwise be ignored and quietly drop coverage."""
        with pytest.raises(ValidationError):
            TraversalScenarioV1.model_validate(_scenario(protocols=['jsonrpc']))

    def test_unknown_transport_is_rejected(self):
        with pytest.raises(ValidationError):
            TraversalScenarioV1.model_validate(_scenario(transports=['carrier_pigeon']))


class TestPeerRef:
    def test_agent_id_joins_sdk_and_line(self):
        assert PeerRef(sdk='go', line='v03').agent_id() == 'go_v03'

    def test_instance_suffix(self):
        assert PeerRef(sdk='python', line='v10', instance=2).agent_id() == (
            'python_v10_2'
        )

    def test_instance_one_is_rejected(self):
        """Instance 1 is the unsuffixed id; allowing both spellings would give
        one agent two names."""
        with pytest.raises(ValidationError):
            PeerRef(sdk='python', line='v10', instance=1)

    def test_is_hashable(self):
        """Peers get de-duplicated during resolution."""
        assert len({PeerRef(sdk='go', line='v10'), PeerRef(sdk='go', line='v10')}) == 1


class TestRoles:
    def test_all_macro(self):
        s = TraversalScenarioV1.model_validate(
            _scenario(roles={'sut': 'current', 'peers': 'all'})
        )
        assert s.roles.peers == 'all'

    def test_sut_only_accepts_current(self):
        with pytest.raises(ValidationError):
            TraversalScenarioV1.model_validate(
                _scenario(roles={'sut': 'python_v10', 'peers': []})
            )

    def test_peer_only_scenario(self):
        s = TraversalScenarioV1.model_validate(_scenario(
            roles={
                'peers': [
                    {'sdk': 'python', 'line': 'v10'},
                    {'sdk': 'go', 'line': 'v10'},
                ],
                'include_sut': False,
            },
        ))
        assert s.roles.include_sut is False


class TestTestWhen:
    def test_parses(self):
        s = TraversalScenarioV1.model_validate(
            _scenario(test_when={'sut_sdk': ['python', 'go']})
        )
        assert s.test_when.sut_sdk == ['python', 'go']

    def test_absent_by_default(self):
        assert TraversalScenarioV1.model_validate(_scenario()).test_when is None


class TestLegacyScenario:
    def test_parses_a_real_shipping_scenario(self):
        s = LegacyScenario.model_validate({
            'name': 'Star Topology (Full) - JSONRPC & GRPC',
            'sdks': ['current', 'python_v10', 'python_v03', 'go_v10', 'go_v03'],
            'edges': ['0->1', '0->2', '0->3', '0->4',
                      '1->0', '2->0', '3->0', '4->0'],
            'protocols': ['jsonrpc', 'grpc'],
            'behavior': 'send_message',
        })
        assert s.sdks[0] == 'current'
        assert s.streaming is False

    def test_ignores_unknown_keys(self):
        """`traversal` appears in a2a-python's nightly file and has never been
        read by anything; rejecting it would break a shipping file."""
        s = LegacyScenario.model_validate({
            'name': 'x', 'sdks': ['current', 'go_v10'],
            'behavior': 'send_message', 'traversal': 'euler',
        })
        assert s.name == 'x'
