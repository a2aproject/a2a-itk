"""testlib.execute_itk_test — scenario-level exception handling.

A hop error (_read_sync_response RuntimeError, httpx failure after
readiness) must become a failed result, not an uncaught exception.
SDK scenarios omit build_subtests, so the default path has to match
the subtest path: log and return passed=False.
"""

from __future__ import annotations

import asyncio
import logging
import time

import test_suite
import testlib
from notifications_app import _extract_task_id_v03
from test_suite.agent_table import AgentTable
from test_suite.launcher import config


class TestExtractTaskIdV03:
    def test_status_update_uses_task_id(self):
        assert (
            _extract_task_id_v03(
                {
                    'kind': 'status-update',
                    'taskId': 't1',
                    'status': {'message': {'role': 'agent'}},
                }
            )
            == 't1'
        )

    def test_task_kind_still_uses_id(self):
        assert _extract_task_id_v03({'kind': 'task', 'id': 't2'}) == 't2'


class TestExecuteItkTestExceptions:
    def test_non_subtest_exception_is_a_failed_result(self, monkeypatch):
        async def raising_single(**_kw):
            raise RuntimeError("JSON-RPC error: bad")

        monkeypatch.setattr(testlib, '_execute_single_itk_test', raising_single)

        result = asyncio.run(
            testlib.execute_itk_test(
                sdks=['current', 'python_v10'],
                behavior='send_message',
                agents=AgentTable({}),
                scenario_name='t',
                build_subtests=False,
            )
        )

        assert result == {
            't': {
                'passed': False,
                'sdks': ['current', 'python_v10'],
                'edges': None,
            }
        }


class TestScenarioDeadline:
    """A traversal that never finishes must fail, not hold the run open."""

    @staticmethod
    def _short_deadline(monkeypatch):
        monkeypatch.setattr(testlib.itk_config, 'scenario_timeout', lambda: 0.05)

    def test_hung_scenario_is_a_failed_result(self, monkeypatch):
        async def hanging(**_kw):
            await asyncio.sleep(30)
            return True

        monkeypatch.setattr(testlib, '_execute_single_itk_test', hanging)
        self._short_deadline(monkeypatch)

        start = time.monotonic()
        result = asyncio.run(
            testlib.execute_itk_test(
                sdks=['current', 'dotnet_v10'],
                behavior='resubscribe',
                agents=AgentTable({}),
                scenario_name='t',
            )
        )

        assert time.monotonic() - start < 5
        assert result['t']['passed'] is False

    def test_each_subtest_gets_its_own_deadline(self, monkeypatch):
        async def single(**kw):
            if 'dotnet_v10' in kw['sdks']:
                await asyncio.sleep(30)
            return True

        monkeypatch.setattr(testlib, '_execute_single_itk_test', single)
        monkeypatch.setattr(
            test_suite,
            '_get_valid_subgraphs',
            lambda **_kw: [
                {'sdks': ['current', 'dotnet_v10'], 'edges': None},
                {'sdks': ['current', 'go_v10'], 'edges': None},
            ],
        )
        self._short_deadline(monkeypatch)

        result = asyncio.run(
            testlib.execute_itk_test(
                sdks=['current', 'dotnet_v10', 'go_v10'],
                behavior='send_message',
                agents=AgentTable({}),
                scenario_name='t',
                build_subtests=True,
            )
        )

        assert result['t-sub-current-dotnet_v10']['passed'] is False
        assert result['t-sub-current-go_v10']['passed'] is True

    def test_a_timeout_inside_the_traversal_is_not_blamed_on_the_deadline(
        self, monkeypatch, caplog,
    ):
        async def raising(**_kw):
            raise TimeoutError('peer socket timed out')

        monkeypatch.setattr(testlib, '_execute_single_itk_test', raising)

        with caplog.at_level(logging.ERROR, logger='testlib'):
            result = asyncio.run(
                testlib.execute_itk_test(
                    sdks=['current', 'go_v10'],
                    behavior='send_message',
                    agents=AgentTable({}),
                    scenario_name='t',
                )
            )

        assert result['t']['passed'] is False
        assert 'ITK_SCENARIO_TIMEOUT' not in caplog.text
        assert 'peer socket timed out' in caplog.text

    def test_env_overrides_the_default(self, monkeypatch):
        monkeypatch.delenv('ITK_SCENARIO_TIMEOUT', raising=False)
        assert config.scenario_timeout() == 60
        monkeypatch.setenv('ITK_SCENARIO_TIMEOUT', '7')
        assert config.scenario_timeout() == 7
