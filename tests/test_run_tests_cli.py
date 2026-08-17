"""run_tests.py — argument handling, scenario loading, and pre-flight checks.

The pipeline itself is covered by test_itk_service_v2.py (both front ends
call the same :mod:`itk_runner`), so this module only covers what the CLI
adds: parsing a scenario file, ``--sdks`` filtering, and refusing to start
work that can't succeed.

Nothing here touches the network or spawns an agent.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

import itk_runner

import run_tests
from itk_runner import Scenario


def _write(tmp_path, payload):
    p = tmp_path / 'scenarios.json'
    p.write_text(json.dumps(payload), encoding='utf-8')
    return p


_ONE = {
    'tests': [
        {
            'name': 'peer only',
            'sdks': ['python_v10', 'go_v10'],
            'behavior': 'send_message',
            'protocols': ['jsonrpc'],
        }
    ]
}


# ---------------------------------------------------------------------------
# Scenario file parsing — same schema as each SDK's scenarios.json
# ---------------------------------------------------------------------------


class TestLoadScenarios:
    def test_parses_all_fields(self, tmp_path):
        path = _write(tmp_path, {
            'tests': [{
                'name': 'full',
                'sdks': ['current', 'python_v10'],
                'behavior': 'push_notification',
                'edges': ['0->1', '1->0'],
                'protocols': ['grpc'],
                'streaming': True,
                'build_subtests': True,
            }],
        })
        (s,) = run_tests.load_scenarios(path)
        assert s == Scenario(
            name='full',
            sdks=['current', 'python_v10'],
            behavior='push_notification',
            edges=['0->1', '1->0'],
            protocols=['grpc'],
            streaming=True,
            build_subtests=True,
        )

    def test_optional_fields_default(self, tmp_path):
        (s,) = run_tests.load_scenarios(_write(tmp_path, _ONE))
        assert s.edges is None
        assert s.streaming is False
        assert s.build_subtests is False

    def test_a_real_sdk_scenario_file_parses(self, tmp_path):
        """The whole point of reusing the schema — an SDK's file runs as-is."""
        path = _write(tmp_path, {
            'tests': [{
                'name': 'Star Topology (Full) - JSONRPC & GRPC',
                'sdks': ['current', 'python_v10', 'python_v03', 'go_v10', 'go_v03'],
                'edges': ['0->1', '0->2', '0->3', '0->4',
                          '1->0', '2->0', '3->0', '4->0'],
                'protocols': ['jsonrpc', 'grpc'],
                'behavior': 'send_message',
            }],
        })
        (s,) = run_tests.load_scenarios(path)
        assert len(s.sdks) == 5
        assert len(s.edges) == 8

    def test_missing_file_exits(self, tmp_path):
        with pytest.raises(SystemExit, match='not found'):
            run_tests.load_scenarios(tmp_path / 'nope.json')

    def test_invalid_json_exits(self, tmp_path):
        p = tmp_path / 'scenarios.json'
        p.write_text('{not json', encoding='utf-8')
        with pytest.raises(SystemExit, match='invalid JSON'):
            run_tests.load_scenarios(p)

    def test_missing_tests_array_exits(self, tmp_path):
        with pytest.raises(SystemExit, match='"tests" array'):
            run_tests.load_scenarios(_write(tmp_path, {'scenarios': []}))

    def test_missing_required_field_names_it(self, tmp_path):
        path = _write(tmp_path, {'tests': [{'name': 'x', 'sdks': ['go_v10']}]})
        with pytest.raises(SystemExit, match='behavior'):
            run_tests.load_scenarios(path)


# ---------------------------------------------------------------------------
# --sdks filtering
# ---------------------------------------------------------------------------


class TestFilterBySdks:
    SCENARIOS = [
        Scenario(name='py+go', sdks=['python_v10', 'go_v10'], behavior='send_message'),
        Scenario(name='py only', sdks=['python_v10'], behavior='send_message'),
        Scenario(name='with rust', sdks=['python_v10', 'rust_v10'], behavior='send_message'),
    ]

    def test_none_keeps_everything(self):
        assert run_tests.filter_by_sdks(self.SCENARIOS, None) == self.SCENARIOS

    def test_drops_scenarios_naming_an_excluded_peer(self):
        kept = run_tests.filter_by_sdks(self.SCENARIOS, {'python_v10', 'go_v10'})
        assert [s.name for s in kept] == ['py+go', 'py only']

    def test_all_or_nothing_never_trims_a_scenario(self):
        # A scenario is kept whole or dropped whole — running 'with rust'
        # minus rust would silently test something else.
        kept = run_tests.filter_by_sdks(self.SCENARIOS, {'python_v10'})
        assert [s.name for s in kept] == ['py only']


# ---------------------------------------------------------------------------
# Pre-flight: fail before any network or build work
# ---------------------------------------------------------------------------


class TestMountPreflight:
    @staticmethod
    def _run(argv, monkeypatch):
        """Drive main_async, asserting the pipeline is never reached."""
        async def must_not_run(*_a, **_kw):
            raise AssertionError('run_scenarios must not be reached')

        monkeypatch.setattr(itk_runner, 'run_scenarios', must_not_run)
        return asyncio.run(run_tests.main_async(run_tests.parse_args(argv)))

    def test_current_without_mount_fails_fast(self, tmp_path, monkeypatch):
        path = _write(tmp_path, {
            'tests': [{'name': 'sut', 'sdks': ['current', 'python_v10'],
                       'behavior': 'send_message'}],
        })
        assert self._run(['--scenarios', str(path)], monkeypatch) == 1

    def test_mount_pointing_at_a_nonexistent_dir_fails_fast(self, tmp_path, monkeypatch):
        path = _write(tmp_path, {
            'tests': [{'name': 'sut', 'sdks': ['current'], 'behavior': 'send_message'}],
        })
        rc = self._run(
            ['--scenarios', str(path), '--mount', str(tmp_path / 'nope')], monkeypatch,
        )
        assert rc == 1

    def test_mount_sets_the_env_var_the_launcher_reads(self, tmp_path, monkeypatch):
        # --mount reaches the launcher purely through ITK_MOUNT_DIR, which
        # launcher.config.mount_dir() reads. If that wiring breaks, the SUT
        # silently resolves to the container path instead.
        sut = tmp_path / 'itk'
        sut.mkdir()
        path = _write(tmp_path, {
            'tests': [{'name': 'sut', 'sdks': ['current'], 'behavior': 'send_message'}],
        })
        rc = self._run(
            ['--scenarios', str(path), '--mount', str(sut), '--dry-run'], monkeypatch,
        )
        assert rc == 0
        assert os.environ['ITK_MOUNT_DIR'] == str(sut.resolve())

    def test_empty_after_filtering_is_an_error(self, tmp_path, monkeypatch):
        path = _write(tmp_path, _ONE)
        rc = self._run(
            ['--scenarios', str(path), '--sdks', 'rust_v10'], monkeypatch,
        )
        assert rc == 1


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_reports_plan_without_running(self, tmp_path, monkeypatch, capsys):
        async def must_not_run(*_a, **_kw):
            raise AssertionError('run_scenarios must not be reached')

        monkeypatch.setattr(itk_runner, 'run_scenarios', must_not_run)
        path = _write(tmp_path, _ONE)
        rc = asyncio.run(
            run_tests.main_async(run_tests.parse_args(['--scenarios', str(path), '--dry-run']))
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert 'python_v10' in out
        assert 'go_v10' in out
        assert 'peer only' in out


# ---------------------------------------------------------------------------
# The bundled default scenario set
# ---------------------------------------------------------------------------


class TestBundledSmokeSet:
    def test_parses(self):
        scenarios = run_tests.load_scenarios(run_tests._DEFAULT_SCENARIOS)  # noqa: SLF001
        assert scenarios

    def test_needs_no_sdk_checkout(self):
        """It must run with nothing but this repo — no --mount required."""
        scenarios = run_tests.load_scenarios(run_tests._DEFAULT_SCENARIOS)  # noqa: SLF001
        named = {sdk for s in scenarios for sdk in s.sdks}
        assert 'current' not in named, (
            'the default set must not reference the SUT, or `uv run '
            'run_tests.py` fails for anyone without an SDK checked out'
        )

    def test_every_agent_resolves_through_the_matrix(self):
        """Guards against the default set drifting away from matrix.yaml."""
        matrix = itk_runner.get_matrix()
        scenarios = run_tests.load_scenarios(run_tests._DEFAULT_SCENARIOS)  # noqa: SLF001
        for sdk in {sdk for s in scenarios for sdk in s.sdks}:
            assert sdk in matrix, f'{sdk} is not in matrix.yaml'
