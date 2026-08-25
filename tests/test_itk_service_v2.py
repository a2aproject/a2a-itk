"""itk_service_v2 + itk_runner — /run wiring, matrix + launcher integration.

Drives the pipeline through the HTTP surface, mocked below the launcher and
scenario-execution layer (Cluster, matrix, resolve_ref, execute_itk_test are
stubbed on :mod:`itk_runner`, where they now live). Each real subsystem is
tested independently in its own module — here we only verify the glue:

  * frozen wire schema in and out
  * matrix + resolve_ref invoked with the right args per unique SDK
  * 'current' bypasses matrix -> MOUNT
  * Cluster.start_all called with the built specs
  * partial-startup failure -> 502 with per-target detail
  * launcher handles reach the executor as an AgentTable
  * scenarios executed sequentially against the shared cluster
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

import itk_runner
from itk_service_v2 import app
from test_suite.launcher.errors import InfraFailure, PermanentError, Stage
from test_suite.launcher.matrix import Matrix
from test_suite.launcher.spec import Kind


_SHA_A = 'a' * 40
_SHA_B = 'b' * 40


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_TEST_MATRIX = {
    'sdks': {
        'python': {'v10': {'repo': 'a2aproject/a2a-python', 'ref': 'main'}},
        'go': {'v10': {'repo': 'a2aproject/a2a-go', 'ref': 'main'}},
    },
}


@dataclass
class _FakeHandle:
    http_port: int
    grpc_port: int
    pid: int = 42


@dataclass
class _FakeOutcome:
    spec: Any
    handle: _FakeHandle | None
    error: Any | None = None
    elapsed_s: float = 0.1

    def ok(self) -> bool:
        return self.handle is not None


class _FakeCluster:
    """Records what start_all was called with; returns preset outcomes."""

    instances: list['_FakeCluster'] = []

    def __init__(self, *_a: Any, **kw: Any) -> None:
        self.start_all_calls: list[tuple[list[Any], list[str | None] | None]] = []
        self.init_kwargs: dict[str, Any] = dict(kw)
        self.exited = False
        # Default: every spec succeeds with sequential fake ports.
        self._outcomes: list[_FakeOutcome] | None = None
        _FakeCluster.instances.append(self)

    def set_outcomes(self, outcomes: list[_FakeOutcome]) -> None:
        self._outcomes = outcomes

    def start_all(self, specs, *, log_names=None, max_workers=None):  # noqa: ARG002
        self.start_all_calls.append((list(specs), log_names))
        if self._outcomes is not None:
            return self._outcomes
        return [
            _FakeOutcome(
                spec=s,
                handle=_FakeHandle(
                    http_port=50000 + i * 2,
                    grpc_port=50001 + i * 2,
                    pid=1000 + i,
                ),
            )
            for i, s in enumerate(specs)
        ]

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.exited = True
        return None


@pytest.fixture(autouse=True)
def stub_deps(monkeypatch):
    """Every test gets: an injected matrix + a fake Cluster + a stub
    resolve_ref + a stub execute_itk_test. Individual tests override
    pieces via monkeypatch."""
    # Fresh matrix each test — some tests replace this with their own.
    monkeypatch.setattr(itk_runner, '_matrix', Matrix.from_dict(_TEST_MATRIX))

    # Fake Cluster class.
    _FakeCluster.instances = []
    monkeypatch.setattr(itk_runner, 'Cluster', _FakeCluster)

    # Stub resolve_ref: main -> _SHA_A for a2a-python, _SHA_B for a2a-go.
    def fake_resolve_ref(repo: str, ref: str) -> str:  # noqa: ARG001
        return {'a2aproject/a2a-python': _SHA_A,
                'a2aproject/a2a-go': _SHA_B}.get(repo, 'c' * 40)
    monkeypatch.setattr(itk_runner, 'resolve_ref', fake_resolve_ref)

    # Stub execute_itk_test: returns a passing result for the label.
    async def fake_execute(sdks, behavior, edges=None, scenario_name=None, **_kw):  # noqa: ARG001
        return {scenario_name: {'passed': True, 'sdks': sdks, 'edges': edges}}
    monkeypatch.setattr(itk_runner, 'execute_itk_test', fake_execute)

    return _FakeCluster


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Frozen wire schema
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_shape(self, client):
        r = client.get('/health')
        assert r.status_code == 200
        assert r.json() == {'status': 'ok'}


class TestRunSchema:
    def test_empty_tests_400(self, client):
        r = client.post('/run', json={'tests': []})
        assert r.status_code == 400
        assert 'No tests provided' in r.json()['detail']

    def test_response_shape(self, client):
        r = client.post('/run', json={
            'tests': [{
                'name': 'test1',
                'sdks': ['current', 'python_v10'],
                'behavior': 'echo',
            }],
        })
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {'results', 'all_passed'}
        assert body['all_passed'] is True
        assert 'test1' in body['results']
        assert body['results']['test1']['sdks'] == ['current', 'python_v10']

    def test_original_response_fields_are_unchanged(self, client):
        """Five SDK repos parse this response. The three original fields must
        keep their names and meaning; anything added is additive."""
        r = client.post('/run', json={
            'tests': [{'name': 'test1', 'sdks': ['current', 'python_v10'],
                       'behavior': 'echo'}],
        })
        details = r.json()['results']['test1']
        assert {'passed', 'sdks', 'edges'} <= set(details)
        assert details['passed'] is True
        assert details['sdks'] == ['current', 'python_v10']

    def test_scenario_metadata_is_returned(self, client):
        """Carried so the nightly processor needn't match result names back
        against the scenario file — a lookup that silently dropped anything
        it couldn't find."""
        r = client.post('/run', json={
            'tests': [{
                'name': 'test1', 'sdks': ['current', 'python_v10'],
                'behavior': 'send_message', 'protocols': ['jsonrpc', 'grpc'],
                'streaming': True,
            }],
        })
        details = r.json()['results']['test1']
        assert details['behavior'] == 'send_message'
        assert details['protocols'] == ['jsonrpc', 'grpc']
        assert details['streaming'] is True


# ---------------------------------------------------------------------------
# Spec building
# ---------------------------------------------------------------------------


class TestSpecBuilding:
    def test_current_becomes_mount(self, client, stub_deps):  # noqa: ARG002
        client.post('/run', json={
            'tests': [{'name': 't', 'sdks': ['current'], 'behavior': 'echo'}],
        })
        cluster = _FakeCluster.instances[-1]
        specs = cluster.start_all_calls[0][0]
        assert len(specs) == 1
        assert specs[0].kind is Kind.MOUNT

    def test_peer_becomes_checkout(self, client, stub_deps):  # noqa: ARG002
        client.post('/run', json={
            'tests': [{'name': 't', 'sdks': ['python_v10'], 'behavior': 'echo'}],
        })
        specs = _FakeCluster.instances[-1].start_all_calls[0][0]
        assert len(specs) == 1
        assert specs[0].kind is Kind.CHECKOUT
        assert specs[0].repo == 'a2aproject/a2a-python'
        assert specs[0].sha == _SHA_A  # from stub resolve_ref

    def test_mixed_current_and_peers(self, client, stub_deps):  # noqa: ARG002
        client.post('/run', json={
            'tests': [{
                'name': 't',
                'sdks': ['current', 'python_v10', 'go_v10'],
                'behavior': 'echo',
            }],
        })
        specs = _FakeCluster.instances[-1].start_all_calls[0][0]
        kinds = sorted(s.kind.value for s in specs)
        assert kinds == ['checkout', 'checkout', 'mount']

    def test_union_across_scenarios(self, client, stub_deps):  # noqa: ARG002
        # Two scenarios each need current+python — cluster should have 2
        # specs total (current + python_v10), not 4.
        client.post('/run', json={
            'tests': [
                {'name': 't1', 'sdks': ['current', 'python_v10'], 'behavior': 'echo'},
                {'name': 't2', 'sdks': ['current', 'python_v10'], 'behavior': 'echo'},
            ],
        })
        specs = _FakeCluster.instances[-1].start_all_calls[0][0]
        assert len(specs) == 2, f'expected 2 unique specs, got {len(specs)}'

    def test_ref_resolved_once_per_repo(self, client, monkeypatch, stub_deps):  # noqa: ARG002
        # Two IDs that share (repo, ref) — resolve_ref called once, not twice.
        calls: list[tuple[str, str]] = []
        def counting_resolve(repo, ref):
            calls.append((repo, ref))
            return _SHA_A
        monkeypatch.setattr(itk_runner, 'resolve_ref', counting_resolve)

        # python_v10 and python_v10_2 both map to same matrix entry.
        client.post('/run', json={
            'tests': [{'name': 't', 'sdks': ['python_v10', 'python_v10_2'], 'behavior': 'echo'}],
        })
        assert calls == [('a2aproject/a2a-python', 'main')], (
            f'ref should resolve once per (repo, ref); got {calls!r}'
        )

    def test_unknown_agent_id_400(self, client, stub_deps):  # noqa: ARG002
        r = client.post('/run', json={
            'tests': [{'name': 't', 'sdks': ['rust_v10'], 'behavior': 'echo'}],
        })
        # rust_v10 not in the injected _TEST_MATRIX
        assert r.status_code == 400
        assert 'rust_v10' in r.json()['detail']

    def test_matrix_error_400(self, client, monkeypatch, stub_deps):  # noqa: ARG002
        # A totally malformed agent id.
        r = client.post('/run', json={
            'tests': [{'name': 't', 'sdks': ['malformed'], 'behavior': 'echo'}],
        })
        assert r.status_code == 400

    def test_permanent_resolve_error_400(self, client, monkeypatch, stub_deps):  # noqa: ARG002
        def bad(repo, ref):
            raise PermanentError(repo, ref, Stage.FETCH, 'ref does not exist')
        monkeypatch.setattr(itk_runner, 'resolve_ref', bad)
        r = client.post('/run', json={
            'tests': [{'name': 't', 'sdks': ['python_v10'], 'behavior': 'echo'}],
        })
        assert r.status_code == 400

    def test_transient_resolve_error_502(self, client, monkeypatch, stub_deps):  # noqa: ARG002
        def flaky(repo, ref):
            raise InfraFailure(repo, ref, Stage.FETCH, message='ls-remote timeout')
        monkeypatch.setattr(itk_runner, 'resolve_ref', flaky)
        r = client.post('/run', json={
            'tests': [{'name': 't', 'sdks': ['python_v10'], 'behavior': 'echo'}],
        })
        assert r.status_code == 502


# ---------------------------------------------------------------------------
# Cluster startup + partial failure
# ---------------------------------------------------------------------------


class TestLogNaming:
    def test_log_names_are_positional_and_match_agent_ids(self, client):
        """Two ids sharing one spec must still get distinct log files."""
        client.post('/run', json={'tests': [{
            'name': 's', 'sdks': ['python_v10', 'python_v10_2'],
            'behavior': 'send_message',
        }]})
        specs, log_names = _FakeCluster.instances[-1].start_all_calls[0]
        # Same repo+ref, so the two specs are equal — the log names must
        # not be (a dict keyed by spec would collapse them).
        assert specs[0] == specs[1]
        assert log_names == ['agent_python_v10', 'agent_python_v10_2']


class TestClusterStartup:
    def test_cluster_exit_called(self, client, stub_deps):  # noqa: ARG002
        client.post('/run', json={
            'tests': [{'name': 't', 'sdks': ['current'], 'behavior': 'echo'}],
        })
        assert _FakeCluster.instances[-1].exited, 'Cluster.__exit__ must run'

    def test_cluster_uses_app_logs_when_bind_mount_present(
        self, client, monkeypatch, stub_deps, tmp_path,  # noqa: ARG002
    ):
        """When run_itk.sh bind-mounts a log dir at /app/logs, agent stderr
        must land there (via Cluster(log_dir=...)) so post-mortem debugging
        of readiness failures doesn't need a live container.

        Regression: itk_service_v2.py used to instantiate Cluster() with no
        args, so agent output was silently discarded and every readiness
        failure looked identical.
        """
        # Simulate /app/logs existing by patching Path.is_dir just for /app/logs.
        import pathlib
        real_is_dir = pathlib.Path.is_dir

        def fake_is_dir(self):
            if str(self) == '/app/logs':
                return True
            return real_is_dir(self)

        monkeypatch.setattr(pathlib.Path, 'is_dir', fake_is_dir)

        client.post('/run', json={
            'tests': [{'name': 't', 'sdks': ['current'], 'behavior': 'echo'}],
        })
        last = _FakeCluster.instances[-1]
        assert last.init_kwargs.get('log_dir') == pathlib.Path('/app/logs')

    def test_cluster_log_dir_none_when_app_logs_missing(
        self, client, stub_deps,  # noqa: ARG002
    ):
        """When /app/logs isn't mounted, Cluster gets log_dir=None so we
        don't crash trying to write into a nonexistent host path."""
        client.post('/run', json={
            'tests': [{'name': 't', 'sdks': ['current'], 'behavior': 'echo'}],
        })
        last = _FakeCluster.instances[-1]
        assert last.init_kwargs.get('log_dir') is None

    def test_partial_startup_502_lists_failed(self, client, monkeypatch, stub_deps):  # noqa: ARG002
        cluster = _FakeCluster()
        monkeypatch.setattr(itk_runner, 'Cluster', lambda *a, **kw: cluster)

        # First succeeds, second fails at READY.
        original_start = cluster.start_all
        def start_with_failure(specs, **kw):
            out = original_start(specs, **kw)
            out[1] = _FakeOutcome(
                spec=specs[1], handle=None,
                error=InfraFailure(
                    specs[1].repo, specs[1].sha, Stage.READY,
                    message='agent did not respond within 35s',
                ),
            )
            return out
        cluster.start_all = start_with_failure

        r = client.post('/run', json={
            'tests': [{
                'name': 't', 'sdks': ['current', 'python_v10'], 'behavior': 'echo',
            }],
        })
        assert r.status_code == 502
        detail = r.json()['detail']
        assert 'Cluster startup failed' in detail
        # The failed peer's name (python_v10) must appear so the operator
        # knows which specific target didn't come up.
        assert 'python_v10' in detail
        assert 'ready' in detail  # Stage.READY.value

    def test_cluster_teardown_on_scenario_exception(self, client, monkeypatch, stub_deps):  # noqa: ARG002
        async def raising_execute(**_kw):
            raise RuntimeError('scenario blew up')
        monkeypatch.setattr(itk_runner, 'execute_itk_test', raising_execute)

        r = client.post('/run', json={
            'tests': [{'name': 't', 'sdks': ['current'], 'behavior': 'echo'}],
        })
        assert r.status_code == 500
        # Cluster must have exited despite the scenario raising.
        assert _FakeCluster.instances[-1].exited


# ---------------------------------------------------------------------------
# Adapter: launcher handles → AgentTable
# ---------------------------------------------------------------------------


class TestAgentTableWiring:
    """The executor must be handed this run's real ports, and nothing else.

    Ports used to live in a process-global registry, where a leaked entry
    silently retargeted the next run. Passing the table as an argument makes
    that structurally impossible; these tests pin both halves.
    """

    def test_executor_receives_the_launchers_ports(self, client, monkeypatch):
        seen: dict[str, tuple[int, int]] = {}

        async def probing_execute(sdks, behavior, agents, edges=None, scenario_name=None, **_kw):  # noqa: ARG001
            for s in sdks:
                seen[s] = (agents[s].http_port, agents[s].grpc_port)
            return {scenario_name: {'passed': True, 'sdks': sdks, 'edges': edges}}

        monkeypatch.setattr(itk_runner, 'execute_itk_test', probing_execute)
        client.post('/run', json={
            'tests': [{'name': 't', 'sdks': ['current', 'python_v10'], 'behavior': 'echo'}],
        })

        assert set(seen) == {'current', 'python_v10'}
        for http_port, grpc_port in seen.values():
            assert http_port and grpc_port
            assert http_port != grpc_port

    def test_table_holds_exactly_the_started_agents(self, client, monkeypatch):
        """Not a fixed roster: only what the cluster actually started."""
        tables = []

        async def capturing_execute(sdks, behavior, agents, edges=None, scenario_name=None, **_kw):  # noqa: ARG001
            tables.append(sorted(agents))
            return {scenario_name: {'passed': True, 'sdks': sdks, 'edges': edges}}

        monkeypatch.setattr(itk_runner, 'execute_itk_test', capturing_execute)
        client.post('/run', json={
            'tests': [{'name': 't', 'sdks': ['current', 'go_v10'], 'behavior': 'echo'}],
        })
        assert tables == [['current', 'go_v10']]

    def test_consecutive_runs_get_independent_tables(self, client, monkeypatch):
        """The service is long-lived; one run's ports must not reach the next."""
        tables = []

        async def capturing_execute(sdks, behavior, agents, edges=None, scenario_name=None, **_kw):  # noqa: ARG001
            tables.append(dict(agents))
            return {scenario_name: {'passed': True, 'sdks': sdks, 'edges': edges}}

        monkeypatch.setattr(itk_runner, 'execute_itk_test', capturing_execute)
        for sdk in ('python_v10', 'go_v10'):
            client.post('/run', json={
                'tests': [{'name': f't-{sdk}', 'sdks': ['current', sdk],
                           'behavior': 'echo'}],
            })

        assert len(tables) == 2
        assert set(tables[0]) == {'current', 'python_v10'}
        assert set(tables[1]) == {'current', 'go_v10'}
        # No carry-over of the first run's peer into the second.
        assert 'python_v10' not in tables[1]

    def test_unknown_agent_is_a_runtime_error_not_a_value_error(self):
        """_get_valid_subgraphs swallows ValueError to skip untraversable
        subgraphs. A peer that never started must not be silently skipped
        that way — it has to surface."""
        from test_suite.agent_table import AgentEndpoint, AgentTable

        table = AgentTable({'current': AgentEndpoint(1, 2)})
        with pytest.raises(RuntimeError, match='No running agent'):
            table.card_uri('python_v10')


# ---------------------------------------------------------------------------
# Multiple scenarios use one cluster
# ---------------------------------------------------------------------------


class TestSharedCluster:
    def test_scenarios_run_sequentially_against_one_cluster(self, client, monkeypatch, stub_deps):  # noqa: ARG002
        seen_call_order: list[str] = []

        async def recording_execute(sdks, behavior, edges=None, scenario_name=None, **_kw):  # noqa: ARG001
            seen_call_order.append(scenario_name)
            return {scenario_name: {'passed': True, 'sdks': sdks, 'edges': edges}}
        monkeypatch.setattr(itk_runner, 'execute_itk_test', recording_execute)

        r = client.post('/run', json={
            'tests': [
                {'name': 't1', 'sdks': ['current'], 'behavior': 'echo'},
                {'name': 't2', 'sdks': ['current'], 'behavior': 'echo'},
                {'name': 't3', 'sdks': ['current'], 'behavior': 'echo'},
            ],
        })
        assert r.status_code == 200
        # One cluster instance for all three scenarios.
        assert len(_FakeCluster.instances) == 1
        # start_all called ONCE, scenarios ran in order.
        assert len(_FakeCluster.instances[0].start_all_calls) == 1
        assert seen_call_order == ['t1', 't2', 't3']


# ---------------------------------------------------------------------------
# Matrix lazy-load
# ---------------------------------------------------------------------------


class TestMatrixLoad:
    def test_lazy_load_from_default_on_first_use(self, monkeypatch, client):
        # Reset the cache and monkey-patch Matrix.from_default to verify.
        monkeypatch.setattr(itk_runner, '_matrix', None)
        called = [0]
        real_from_default = Matrix.from_default

        def counted():
            called[0] += 1
            return Matrix.from_dict(_TEST_MATRIX)

        monkeypatch.setattr(itk_runner.Matrix, 'from_default', staticmethod(counted))

        # First /run triggers a load.
        client.post('/run', json={
            'tests': [{'name': 't', 'sdks': ['current'], 'behavior': 'echo'}],
        })
        assert called[0] == 1, 'first /run should load matrix'

        # Second /run reuses cached matrix.
        client.post('/run', json={
            'tests': [{'name': 't', 'sdks': ['current'], 'behavior': 'echo'}],
        })
        assert called[0] == 1, 'second /run should not reload matrix'

        # Cleanup — restore real classmethod so other tests aren't affected.
        monkeypatch.setattr(itk_runner.Matrix, 'from_default',
                            staticmethod(real_from_default))
