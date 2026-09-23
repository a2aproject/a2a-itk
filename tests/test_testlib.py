"""testlib.execute_itk_test — scenario-level exception handling.

A hop error (_read_sync_response RuntimeError, httpx failure after
readiness) must become a failed result, not an uncaught exception.
SDK scenarios omit build_subtests, so the default path has to match
the subtest path: log and return passed=False.
"""

from __future__ import annotations

import asyncio

import testlib
from notifications_app import _extract_task_id_v03
from test_suite.agent_table import AgentTable


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
