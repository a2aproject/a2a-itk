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

import httpx
import pytest

import test_suite
import testlib
from notifications_app import (
    _extract_task_id_v03,
    _extract_task_id_v1,
    create_notifications_app,
)
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


class TestExtractTaskIdV10:
    """Which task an A2A 1.x push payload is about. A payload the receiver
    cannot place is refused, and no test can then assert on it."""

    @pytest.mark.parametrize(('payload', 'task_id'), [
        ({'task': {'id': 't1', 'status': {}}}, 't1'),
        ({'statusUpdate': {'taskId': 't2', 'status': {}}}, 't2'),
        ({'artifactUpdate': {'taskId': 't4', 'artifact': {'artifactId': 'a'}}}, 't4'),
        # From A2A 1.1: a client message added to the task.
        ({'messageUpdate': {'taskId': 't3', 'message': {'messageId': 'm'}}}, 't3'),
    ])
    def test_the_kinds_it_stores(self, payload, task_id):
        assert _extract_task_id_v1(payload) == task_id

    def test_the_kind_it_does_not_store_yet(self):
        """A2A delivers a message as well. The conformance test of the payload
        format does not accept one, so that test has to change first."""
        assert _extract_task_id_v1({'message': {'messageId': 'm', 'taskId': 't1'}}) is None

    @pytest.mark.parametrize('payload', [
        {'statusUpdate': {'status': {}}},
        {'statusUpdate': 'not an object'},
        {'messageUpdate': {'taskId': 123}},
        {'statusUpdate': {'taskId': ['t1']}},
        {'task': {'id': {'not': 'a string'}}},
        {},
    ])
    def test_a_payload_that_names_no_task(self, payload):
        """A task id that is not a string is not one: the delivery could not
        be read back under it."""
        assert _extract_task_id_v1(payload) is None

    def test_a_delivery_that_names_no_task_is_refused(self):
        async def deliver() -> int:
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_notifications_app()),
                base_url='http://receiver.test',
            )
            posted = await client.post(
                '/notifications', json={'statusUpdate': {'taskId': ['t1']}}
            )
            await client.aclose()
            return posted.status_code

        assert asyncio.run(deliver()) == 400

    @pytest.mark.parametrize('payload', [
        # So a test can see one that should not have been delivered.
        {'messageUpdate': {'taskId': 't3', 'message': {'messageId': 'm'}}},
        # So a test can assert on what an agent delivers for an artifact.
        {'artifactUpdate': {'taskId': 't3', 'artifact': {'artifactId': 'a'}}},
    ])
    def test_a_stored_kind_is_kept_under_its_task(self, payload):
        async def deliver() -> list[dict]:
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_notifications_app()),
                base_url='http://receiver.test',
            )
            posted = await client.post('/notifications', json=payload)
            assert posted.status_code == 200
            stored = await client.get('/t3/notifications')
            await client.aclose()
            return stored.json()['notifications']

        stored = asyncio.run(deliver())
        assert [n['event'] for n in stored] == [payload]

    def test_a_message_delivery_is_refused(self):
        async def deliver() -> int:
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_notifications_app()),
                base_url='http://receiver.test',
            )
            posted = await client.post(
                '/notifications', json={'message': {'messageId': 'm', 'taskId': 't1'}}
            )
            await client.aclose()
            return posted.status_code

        assert asyncio.run(deliver()) == 400

    def test_traversal_reads_no_text_from_an_artifact_update(self):
        """The traversal suite reads the same receiver, and looks only for the
        agent's text in a status update. An artifact update gives it none."""
        event = {'artifactUpdate': {
            'taskId': 't1',
            'artifact': {'artifactId': 'a', 'parts': [{'text': 'in an artifact'}]},
        }}
        assert testlib._read_v10_notif(event) == []
        assert testlib._read_v03_notif(event) == []


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
