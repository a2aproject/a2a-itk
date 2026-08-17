"""Cluster: batch spawn + readiness gate + process-group teardown.

Two flavours of test:

* **Mocked** — patch cache/spawn/health to isolate the orchestration
  logic (pin release order, log-handle close, port allocation, per-target
  outcome reporting, partial startup, teardown on error).

* **Real subprocess** — verify that ``start_new_session=True`` + killpg
  actually reaps the grandchildren every ITK agent launcher spawns
  (shell -> compiled binary is the shape). Uses ``sh -c`` around a
  ``sleep`` so we can inspect the child's process group after teardown.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from test_suite.launcher import cache, cluster, health
from test_suite.launcher.cluster import AgentHandle, Cluster
from test_suite.launcher.errors import InfraFailure, Stage
from test_suite.launcher.spec import Kind, TargetSpec


_SHA_A = 'a' * 40
_SHA_B = 'b' * 40


# ---------------------------------------------------------------------------
# Mocked orchestration tests
# ---------------------------------------------------------------------------


class _FakeProc:
    """Enough of Popen for the cluster to poll(), wait(), receive signals."""

    def __init__(self, pid: int = 12345):
        self.pid = pid
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout=None):  # noqa: ARG002
        # Simulate cooperative shutdown — becomes done immediately on wait.
        if self._returncode is None:
            self._returncode = -signal.SIGTERM
        return self._returncode

    @property
    def returncode(self) -> int | None:
        return self._returncode


@contextmanager
def _patch_cluster_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ready: bool = True,
    spawn_raises: type[BaseException] | None = None,
    resolve_raises: BaseException | None = None,
):
    """Stub cache/current.spawn_from_dir/health for isolated cluster tests."""
    pins_taken: list[tuple[str, str]] = []
    pins_released: list[tuple[str, str]] = []
    spawned_procs: list[_FakeProc] = []
    spawn_log_names: list[str | None] = []
    killed_pgids: list[tuple[int, int]] = []

    def fake_checkout(repo, sha, **_kw):
        if resolve_raises:
            raise resolve_raises
        pins_taken.append((repo, sha))
        return Path(f'/fake/{repo}/{sha}')

    def fake_release(repo, sha):
        pins_released.append((repo, sha))

    def fake_spawn(agent_dir, http, grpc, *, log_dir=None, log_name=None, new_session=False):  # noqa: ARG001
        # Accept new_session (Cluster passes True) but the mock doesn't
        # actually spawn a subprocess, so pgroup semantics are irrelevant.
        if spawn_raises:
            raise spawn_raises('fake spawn failure')
        p = _FakeProc()
        spawned_procs.append(p)
        spawn_log_names.append(log_name)
        return p

    def fake_wait_ready(_port, *, timeout_s, **_kw):  # noqa: ARG001
        return (ready, 0.01)

    def fake_killpg(pgid, sig):
        killed_pgids.append((pgid, sig))

    monkeypatch.setattr(cache, 'checkout_and_build', fake_checkout)
    monkeypatch.setattr(cache, 'release', fake_release)
    monkeypatch.setattr(cluster._current, 'spawn_from_dir', fake_spawn)  # noqa: SLF001
    monkeypatch.setattr(health, 'wait_ready', fake_wait_ready)
    monkeypatch.setattr(cluster.os, 'killpg', fake_killpg)

    yield {
        'pins_taken': pins_taken,
        'pins_released': pins_released,
        'spawned_procs': spawned_procs,
        'spawn_log_names': spawn_log_names,
        'killed_pgids': killed_pgids,
    }


class TestLogNames:
    """Log basenames are positional, not keyed by spec.

    Two scenario peers can resolve to the same (repo, sha) — `python_v10`
    and `python_v10_2` do. `TargetSpec` is a frozen dataclass, so those two
    specs compare equal; keying log names by spec silently collapsed them
    onto one file and interleaved both agents' output.
    """

    DUPE = TargetSpec(kind=Kind.CHECKOUT, repo='a2aproject/a2a-python', sha=_SHA_A)

    def test_duplicate_specs_get_distinct_log_names(self, monkeypatch):
        with _patch_cluster_deps(monkeypatch) as st:
            with Cluster(readiness_timeout_s=1, teardown_grace_s=1) as c:
                c.start_all(
                    [self.DUPE, self.DUPE],
                    log_names=['agent_python_v10', 'agent_python_v10_2'],
                )
        assert sorted(st['spawn_log_names']) == [
            'agent_python_v10', 'agent_python_v10_2',
        ]

    def test_omitted_log_names_pass_none_through(self, monkeypatch):
        with _patch_cluster_deps(monkeypatch) as st:
            with Cluster(readiness_timeout_s=1, teardown_grace_s=1) as c:
                c.start_all([self.DUPE, self.DUPE])
        assert st['spawn_log_names'] == [None, None]

    def test_length_mismatch_is_rejected(self, monkeypatch):
        with _patch_cluster_deps(monkeypatch):
            with Cluster(readiness_timeout_s=1, teardown_grace_s=1) as c:
                with pytest.raises(ValueError, match='line up positionally'):
                    c.start_all([self.DUPE, self.DUPE], log_names=['only_one'])


class TestAddSingle:
    def test_add_success_returns_handle(self, monkeypatch):
        with _patch_cluster_deps(monkeypatch) as st:
            with Cluster(readiness_timeout_s=1, teardown_grace_s=1) as c:
                handle = c.add(TargetSpec(
                    kind=Kind.CHECKOUT,
                    repo='a2aproject/a2a-python', sha=_SHA_A,
                ))
                assert isinstance(handle, AgentHandle)
                assert handle.spec.repo == 'a2aproject/a2a-python'
                assert 1024 < handle.http_port < 65536
                assert 1024 < handle.grpc_port < 65536
                assert handle.http_port != handle.grpc_port
                assert handle.pid == 12345
            # After exit: pin released, pgroup killed
            assert st['pins_released'] == [('a2aproject/a2a-python', _SHA_A)]
            assert (12345, signal.SIGTERM) in st['killed_pgids']

    def test_add_raises_on_readiness_failure(self, monkeypatch):
        with _patch_cluster_deps(monkeypatch, ready=False) as st:
            with Cluster(readiness_timeout_s=1, teardown_grace_s=1) as c:
                with pytest.raises(InfraFailure) as exc:
                    c.add(TargetSpec(
                        kind=Kind.CHECKOUT,
                        repo='a2aproject/a2a-python', sha=_SHA_A,
                    ))
                assert exc.value.stage is Stage.READY
                # Failed agent was killed
                assert (12345, signal.SIGTERM) in st['killed_pgids']
            # Pin still released on exit (we did take it, checkout succeeded)
            assert st['pins_released'] == [('a2aproject/a2a-python', _SHA_A)]

    def test_add_raises_on_spawn_failure(self, monkeypatch):
        with _patch_cluster_deps(monkeypatch, spawn_raises=OSError) as st:
            with Cluster(readiness_timeout_s=1, teardown_grace_s=1) as c:
                with pytest.raises(InfraFailure) as exc:
                    c.add(TargetSpec(
                        kind=Kind.CHECKOUT,
                        repo='a2aproject/a2a-python', sha=_SHA_A,
                    ))
                assert exc.value.stage is Stage.SPAWN
            # Pin was taken during resolve; still released.
            assert st['pins_released'] == [('a2aproject/a2a-python', _SHA_A)]

    def test_spawn_runtime_error_is_permanent(self, monkeypatch):
        """RuntimeError from spawn_from_dir (unknown language / missing dir /
        no binary after build) must surface as PermanentError, not
        InfraFailure — retrying will never fix these.
        """
        from test_suite.launcher.errors import PermanentError

        with _patch_cluster_deps(monkeypatch, spawn_raises=RuntimeError):
            with Cluster(readiness_timeout_s=1, teardown_grace_s=1) as c:
                with pytest.raises(PermanentError) as exc:
                    c.add(TargetSpec(
                        kind=Kind.CHECKOUT,
                        repo='a2aproject/a2a-python', sha=_SHA_A,
                    ))
                assert exc.value.stage is Stage.SPAWN
                # Message from the underlying RuntimeError propagates
                assert 'fake spawn failure' in str(exc.value)

    def test_spawn_subprocess_error_is_transient(self, monkeypatch):
        """CalledProcessError (e.g. cargo build itself failed) can be
        transient (mirror outage, disk full) so surfaces as InfraFailure.
        """
        with _patch_cluster_deps(monkeypatch, spawn_raises=subprocess.SubprocessError):
            with Cluster(readiness_timeout_s=1, teardown_grace_s=1) as c:
                with pytest.raises(InfraFailure) as exc:
                    c.add(TargetSpec(
                        kind=Kind.CHECKOUT,
                        repo='a2aproject/a2a-python', sha=_SHA_A,
                    ))
                assert exc.value.stage is Stage.SPAWN

    def test_add_raises_on_resolve_failure(self, monkeypatch):
        exc = InfraFailure('a2aproject/a2a-python', _SHA_A, Stage.FETCH,
                           message='network')
        with _patch_cluster_deps(monkeypatch, resolve_raises=exc) as st:
            with Cluster(readiness_timeout_s=1, teardown_grace_s=1) as c:
                with pytest.raises(InfraFailure) as e:
                    c.add(TargetSpec(
                        kind=Kind.CHECKOUT,
                        repo='a2aproject/a2a-python', sha=_SHA_A,
                    ))
                assert e.value is exc
            # No pin was ever taken (resolve failed before pin recorded).
            assert st['pins_released'] == []


class TestStartAllBatch:
    def test_all_succeed(self, monkeypatch):
        specs = [
            TargetSpec(kind=Kind.CHECKOUT, repo='org/a', sha=_SHA_A),
            TargetSpec(kind=Kind.CHECKOUT, repo='org/b', sha=_SHA_B),
        ]
        with _patch_cluster_deps(monkeypatch):
            with Cluster(readiness_timeout_s=1, teardown_grace_s=1) as c:
                outcomes = c.start_all(specs)
                assert len(outcomes) == 2
                assert all(o.ok() for o in outcomes)
                # Order matches input
                assert outcomes[0].spec.repo == 'org/a'
                assert outcomes[1].spec.repo == 'org/b'
                # Ports distinct across both agents
                ports_used = {
                    outcomes[0].handle.http_port, outcomes[0].handle.grpc_port,
                    outcomes[1].handle.http_port, outcomes[1].handle.grpc_port,
                }
                assert len(ports_used) == 4

    def test_partial_startup_reports_specific_failure(self, monkeypatch):
        """TC-009: one succeeds, one fails readiness — outcomes tell you which."""
        specs = [
            TargetSpec(kind=Kind.CHECKOUT, repo='org/good', sha=_SHA_A),
            TargetSpec(kind=Kind.CHECKOUT, repo='org/bad', sha=_SHA_B),
        ]
        call_count = [0]

        def selective_wait(*_a, **_kw):
            call_count[0] += 1
            # First wait_ready succeeds, second fails.
            return (call_count[0] == 1, 0.01)

        with _patch_cluster_deps(monkeypatch) as st:
            monkeypatch.setattr(health, 'wait_ready', selective_wait)
            with Cluster(readiness_timeout_s=1, teardown_grace_s=1) as c:
                outcomes = c.start_all(specs, max_workers=1)  # serial for determinism
                good = [o for o in outcomes if o.ok()]
                bad = [o for o in outcomes if not o.ok()]
                assert len(good) == 1 and good[0].spec.repo == 'org/good'
                assert len(bad) == 1 and bad[0].spec.repo == 'org/bad'
                assert isinstance(bad[0].error, InfraFailure)
                assert bad[0].error.stage is Stage.READY

            # Both pins still released on exit.
            assert sorted(st['pins_released']) == sorted([
                ('org/good', _SHA_A), ('org/bad', _SHA_B),
            ])

    def test_empty_specs_returns_empty(self, monkeypatch):
        with _patch_cluster_deps(monkeypatch):
            with Cluster() as c:
                assert c.start_all([]) == []

    def test_max_workers_env_caps_parallelism(self, monkeypatch):
        """ITK_MAX_WORKERS overrides the default when caller passes None.

        Regression: 8-peer scenarios_full on a 2-vCPU GHA runner OOM'd npm/uv
        under the default ``max(4, len(specs))`` fan-out; the env knob lets
        the CI workflow cap it at 2.
        """
        monkeypatch.setenv('ITK_MAX_WORKERS', '2')

        seen_workers: list[int] = []
        real_pool = ThreadPoolExecutor
        import test_suite.launcher.cluster as cluster_mod

        def spy_pool(*args, **kwargs):
            seen_workers.append(kwargs.get('max_workers'))
            return real_pool(*args, **kwargs)

        monkeypatch.setattr(cluster_mod, 'ThreadPoolExecutor', spy_pool)

        specs = [
            TargetSpec(kind=Kind.CHECKOUT, repo=f'org/r{i}', sha=_SHA_A)
            for i in range(6)
        ]
        with _patch_cluster_deps(monkeypatch):
            with Cluster(readiness_timeout_s=1, teardown_grace_s=1) as c:
                c.start_all(specs)
        assert seen_workers == [2]

    def test_max_workers_explicit_arg_wins_over_env(self, monkeypatch):
        """An explicit ``max_workers=`` on start_all beats the env var."""
        monkeypatch.setenv('ITK_MAX_WORKERS', '2')

        seen_workers: list[int] = []
        real_pool = ThreadPoolExecutor
        import test_suite.launcher.cluster as cluster_mod

        def spy_pool(*args, **kwargs):
            seen_workers.append(kwargs.get('max_workers'))
            return real_pool(*args, **kwargs)

        monkeypatch.setattr(cluster_mod, 'ThreadPoolExecutor', spy_pool)

        specs = [
            TargetSpec(kind=Kind.CHECKOUT, repo=f'org/r{i}', sha=_SHA_A)
            for i in range(6)
        ]
        with _patch_cluster_deps(monkeypatch):
            with Cluster(readiness_timeout_s=1, teardown_grace_s=1) as c:
                c.start_all(specs, max_workers=5)
        assert seen_workers == [5]


class TestTeardown:
    def test_teardown_on_success(self, monkeypatch):
        with _patch_cluster_deps(monkeypatch) as st:
            with Cluster(readiness_timeout_s=1, teardown_grace_s=1) as c:
                c.add(TargetSpec(kind=Kind.CHECKOUT, repo='org/a', sha=_SHA_A))
                c.add(TargetSpec(kind=Kind.CHECKOUT, repo='org/b', sha=_SHA_B))
            # Both pgroups signalled, both pins released
            assert len(st['killed_pgids']) >= 2
            assert sorted(st['pins_released']) == sorted([
                ('org/a', _SHA_A), ('org/b', _SHA_B),
            ])

    def test_teardown_on_exception(self, monkeypatch):
        with _patch_cluster_deps(monkeypatch) as st:
            with pytest.raises(RuntimeError, match='body failed'):
                with Cluster(readiness_timeout_s=1, teardown_grace_s=1) as c:
                    c.add(TargetSpec(
                        kind=Kind.CHECKOUT, repo='org/a', sha=_SHA_A,
                    ))
                    raise RuntimeError('body failed')
            # Cleanup still ran
            assert st['pins_released'] == [('org/a', _SHA_A)]
            assert (12345, signal.SIGTERM) in st['killed_pgids']

    def test_mount_target_no_pin(self, monkeypatch, tmp_path):
        # Set up so resolve.MOUNT succeeds against tmp_path
        mount = tmp_path / 'agents' / 'repo' / 'itk'
        monkeypatch.setenv('ITK_MOUNT_DIR', str(mount))
        mount.mkdir(parents=True)

        with _patch_cluster_deps(monkeypatch) as st:
            with Cluster(readiness_timeout_s=1, teardown_grace_s=1) as c:
                c.add(TargetSpec(kind=Kind.MOUNT))
            assert st['pins_released'] == []
            # Still signalled the pgroup — MOUNT targets get spawned too.
            assert (12345, signal.SIGTERM) in st['killed_pgids']

    def test_teardown_continues_after_kill_failure(self, monkeypatch):
        """Regression: if `_kill_pgroup` raises for one handle, the
        remaining handles/log-handles/pins/ports must still be cleaned up.
        Prior to the fix, __exit__ halted on the first exception.
        """
        with _patch_cluster_deps(monkeypatch) as st:
            with Cluster(readiness_timeout_s=1, teardown_grace_s=1) as c:
                c.add(TargetSpec(kind=Kind.CHECKOUT, repo='org/a', sha=_SHA_A))
                c.add(TargetSpec(kind=Kind.CHECKOUT, repo='org/b', sha=_SHA_B))

                # Break _kill_pgroup so the first call raises.
                calls = [0]
                real_kill = cluster._kill_pgroup  # noqa: SLF001

                def flaky_kill(proc, grace_s):
                    calls[0] += 1
                    if calls[0] == 1:
                        raise OSError('simulated kill failure')
                    return real_kill(proc, grace_s)

                monkeypatch.setattr(cluster, '_kill_pgroup', flaky_kill)
            # Both pins still released despite the first kill raising.
            assert sorted(st['pins_released']) == sorted([
                ('org/a', _SHA_A), ('org/b', _SHA_B),
            ])
            # Second _kill_pgroup call happened (the real one, so killed_pgids
            # has one entry via fake_killpg from patched deps).
            assert calls[0] == 2


# ---------------------------------------------------------------------------
# Real subprocess: verify pgroup teardown catches grandchildren
# ---------------------------------------------------------------------------


class TestProcessGroupTeardown:
    """Prove that `start_new_session=True` + killpg reaps grandchildren.

    Every real ITK agent looks like:  shell wrapper -> compiled binary.
    If teardown only signalled the direct child, the binary would leak.
    """

    def test_killpg_reaps_grandchild(self, tmp_path):
        # Shell script: launch a child `sleep` in the same session, then
        # sleep itself. Two processes in one pgroup; killpg must reap both.
        # The pid file lives under tmp_path so `pytest -n auto` runs don't
        # clobber each other.
        child_pid_file = tmp_path / 'child.pid'
        script = tmp_path / 'wrapper.sh'
        script.write_text(
            '#!/bin/sh\n'
            'sleep 60 &\n'
            f'echo $! > {child_pid_file}\n'
            'sleep 60\n',
            encoding='utf-8',
        )
        script.chmod(0o755)

        proc = subprocess.Popen(  # noqa: S603
            ['/bin/sh', str(script)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # Wait for the child pid to be written.
            for _ in range(50):
                if child_pid_file.exists() and child_pid_file.stat().st_size > 0:
                    break
                time.sleep(0.1)
            child_pid = int(child_pid_file.read_text().strip())

            # Both processes alive?
            assert _pid_alive(proc.pid), 'parent should be running'
            assert _pid_alive(child_pid), 'grandchild should be running'

            # Trigger the teardown we care about.
            cluster._kill_pgroup(proc, grace_s=2)  # noqa: SLF001

            # Wait a moment for the SIGTERM to propagate.
            for _ in range(20):
                if not _pid_alive(child_pid):
                    break
                time.sleep(0.1)

            assert not _pid_alive(proc.pid), 'parent should be dead'
            assert not _pid_alive(child_pid), 'grandchild should ALSO be dead'
        finally:
            # Belt-and-braces cleanup if the test failed mid-way.
            for pid in (proc.pid,):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def test_kill_pgroup_on_already_dead_proc_is_noop(self):
        # Popen for an immediate exit.
        proc = subprocess.Popen([sys.executable, '-c', 'pass'])  # noqa: S603
        proc.wait()
        # Should not raise even though pgroup is long gone.
        cluster._kill_pgroup(proc, grace_s=1)  # noqa: SLF001

    def test_try_killpg_handles_esrch(self, monkeypatch):
        # Simulate the "child exited between poll and killpg" race.
        def raise_esrch(_pgid, _sig):
            raise ProcessLookupError('no such process')

        monkeypatch.setattr(cluster.os, 'killpg', raise_esrch)
        # Must not raise.
        cluster._try_killpg(99999, signal.SIGTERM)  # noqa: SLF001


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours
    return True
