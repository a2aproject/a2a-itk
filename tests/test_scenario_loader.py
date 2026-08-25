"""Loading scenario files: both formats, both encodings, and mixtures.

The compatibility requirement is that an SDK still on legacy JSON
and one already on traversal/v1 YAML both work, against the same service, at
the same time. These tests are that requirement.
"""

from __future__ import annotations

import json

import pytest

from test_suite.scenarios.loader import (
    ScenarioFileError,
    load_file,
    load_files,
    parse_tests,
)
from test_suite.scenarios.schema import LegacyScenario, TraversalScenarioV1


LEGACY_JSON = {
    'tests': [
        {
            'name': 'Current vs Go v10 - Send Message',
            'sdks': ['current', 'go_v10'],
            'edges': ['0->1', '1->0'],
            'protocols': ['jsonrpc', 'grpc'],
            'behavior': 'send_message',
        }
    ]
}

TRAVERSAL_YAML = """
schema: traversal/v1
name: Star - send message
tier: pr
roles:
  sut: current
  peers:
    - {sdk: go, line: v10}
    - {sdk: python, line: v03}
transports: [jsonrpc, grpc]
behavior: send_message
topology: star
"""


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(
        content if isinstance(content, str) else json.dumps(content),
        encoding='utf-8',
    )
    return p


class TestLegacyFormat:
    def test_loads_unchanged(self, tmp_path):
        scenarios = load_file(_write(tmp_path, 'scenarios.json', LEGACY_JSON))
        assert len(scenarios) == 1
        assert isinstance(scenarios[0], LegacyScenario)
        assert scenarios[0].sdks == ['current', 'go_v10']

    def test_every_shipping_file_shape_parses(self, tmp_path):
        """Fields seen across the five repos, including ones nothing reads."""
        payload = {'tests': [{
            'name': 'x', 'sdks': ['current', 'ts_v03'], 'traversal': 'euler',
            'edges': ['0->1', '1->0'], 'protocols': ['jsonrpc'],
            'streaming': True, 'behavior': 'resubscribe',
        }]}
        s = load_file(_write(tmp_path, 's.json', payload))[0]
        assert s.streaming is True
        assert s.behavior == 'resubscribe'


class TestTraversalFormat:
    def test_loads_from_yaml(self, tmp_path):
        scenarios = load_file(_write(tmp_path, 's.yaml', TRAVERSAL_YAML))
        assert len(scenarios) == 1
        s = scenarios[0]
        assert isinstance(s, TraversalScenarioV1)
        assert [p.agent_id() for p in s.roles.peers] == ['go_v10', 'python_v03']

    def test_bare_scenario_needs_no_tests_wrapper(self, tmp_path):
        """A one-scenario YAML file reads naturally without the wrapper."""
        assert len(load_file(_write(tmp_path, 's.yaml', TRAVERSAL_YAML))) == 1

    def test_tests_wrapper_also_works(self, tmp_path):
        content = """
tests:
  - schema: traversal/v1
    name: One
    roles:
      peers: [{sdk: go, line: v10}]
    transports: [jsonrpc]
    behavior: send_message
  - schema: traversal/v1
    name: Two
    roles:
      peers: [{sdk: python, line: v10}]
    transports: [grpc]
    behavior: send_message
"""
        scenarios = load_file(_write(tmp_path, 's.yaml', content))
        assert [s.name for s in scenarios] == ['One', 'Two']

    def test_traversal_v1_in_a_json_file(self, tmp_path):
        """The schema key selects the parser, not the file extension."""
        payload = {
            'schema': 'traversal/v1', 'name': 'x',
            'roles': {'peers': [{'sdk': 'go', 'line': 'v10'}]},
            'transports': ['jsonrpc'], 'behavior': 'send_message',
        }
        s = load_file(_write(tmp_path, 's.json', payload))[0]
        assert isinstance(s, TraversalScenarioV1)


class TestMixedFormats:
    def test_one_file_may_hold_both(self, tmp_path):
        """This is what lets a repo migrate one scenario at a time."""
        payload = {'tests': [
            LEGACY_JSON['tests'][0],
            {
                'schema': 'traversal/v1', 'name': 'new one',
                'roles': {'peers': [{'sdk': 'go', 'line': 'v10'}]},
                'transports': ['jsonrpc'], 'behavior': 'send_message',
            },
        ]}
        scenarios = load_file(_write(tmp_path, 's.json', payload))
        assert [type(s).__name__ for s in scenarios] == [
            'LegacyScenario', 'TraversalScenarioV1',
        ]

    def test_load_files_concatenates_in_order(self, tmp_path):
        a = _write(tmp_path, 'a.json', LEGACY_JSON)
        b = _write(tmp_path, 'b.yaml', TRAVERSAL_YAML)
        scenarios = load_files([a, b])
        assert len(scenarios) == 2
        assert isinstance(scenarios[0], LegacyScenario)
        assert isinstance(scenarios[1], TraversalScenarioV1)


class TestErrors:
    def test_missing_file(self, tmp_path):
        with pytest.raises(ScenarioFileError, match='not found'):
            load_file(tmp_path / 'nope.json')

    def test_empty_file(self, tmp_path):
        with pytest.raises(ScenarioFileError, match='empty'):
            load_file(_write(tmp_path, 's.yaml', ''))

    def test_malformed_yaml(self, tmp_path):
        with pytest.raises(ScenarioFileError, match='could not parse'):
            load_file(_write(tmp_path, 's.yaml', 'tests: [\n  unclosed'))

    def test_missing_tests_array(self, tmp_path):
        with pytest.raises(ScenarioFileError, match='"tests" array'):
            load_file(_write(tmp_path, 's.json', {'scenarios': []}))

    def test_unknown_schema_is_named(self, tmp_path):
        """A future or misspelled schema must fail loudly, not fall back to
        the legacy parser and drop most of the file's meaning."""
        payload = {'tests': [{'schema': 'traversal/v2', 'name': 'x'}]}
        with pytest.raises(ScenarioFileError, match="unknown schema 'traversal/v2'"):
            load_file(_write(tmp_path, 's.json', payload))

    def test_validation_error_names_the_entry(self, tmp_path):
        payload = {'tests': [
            LEGACY_JSON['tests'][0],
            {'schema': 'traversal/v1', 'name': 'bad'},
        ]}
        with pytest.raises(ScenarioFileError, match=r'tests\[1\]'):
            load_file(_write(tmp_path, 's.json', payload))

    def test_error_message_includes_the_path(self, tmp_path):
        path = _write(tmp_path, 'broken.json', {'nope': 1})
        with pytest.raises(ScenarioFileError, match='broken.json'):
            load_file(path)

    def test_non_mapping_entry(self):
        with pytest.raises(ScenarioFileError, match='must be a mapping'):
            parse_tests({'tests': ['just a string']})

    def test_top_level_scalar(self):
        with pytest.raises(ScenarioFileError, match='expected a mapping or a list'):
            parse_tests('nope')


class TestBundledFiles:
    """The scenario files shipped in this repo must always be loadable."""

    def test_smoke_json(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        scenarios = load_file(root / 'scenarios' / 'smoke.json')
        assert scenarios
        assert all(isinstance(s, LegacyScenario) for s in scenarios)

    def test_smoke_yaml(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        scenarios = load_file(root / 'scenarios' / 'traversal' / 'smoke.yaml')
        assert scenarios
        assert all(isinstance(s, TraversalScenarioV1) for s in scenarios)
