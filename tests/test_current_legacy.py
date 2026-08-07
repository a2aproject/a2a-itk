"""Legacy contract for :func:`test_suite.current.spawn_agent`.

``test_suite.current`` is the hot path for every SDK's blocking ITK CI:
``itk_service.py`` -> ``testlib.start_itk_cluster`` -> per-SDK launcher
functions, one of which is ``spawn_agent`` from this file (registered as
``current`` in ``test_suite/__init__.py``).

The launcher extraction refactored ``spawn_agent`` into a wrapper over
``spawn_from_dir``. This suite pins the observable contract every SDK's
existing CI depends on, so a future edit can't silently regress a language:

  * mount-missing raises a specific error message
  * per-language argv is byte-identical to what production sees today
  * cwd is the mount dir (or the mount's parent for Maven pre-build)
  * DEBUG log filename stays ``agent_current.log`` under ``<root>/logs/``

subprocess.Popen is patched so no real toolchain is required.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from test_suite import current


# ---------------------------------------------------------------------------
# Recorder — captures every Popen and every subprocess.run
# ---------------------------------------------------------------------------


class _RecPopen:
    calls: list[tuple[list[str], Path, Any]] = []

    def __init__(self, args, cwd=None, stdout=None, stderr=None, text=None, **_kw):  # noqa: ARG002
        # Extra kwargs accepted (`start_new_session=True` is one) so the
        # real spawn code path exercises identical Popen arguments.
        _RecPopen.calls.append((list(args), Path(cwd) if cwd else Path.cwd(), stdout))
        self.pid = 42
        self.args = args

    def wait(self, *_a, **_k):
        return 0

    def terminate(self):
        pass


class _RecRun:
    """subprocess.run stub for the java pre-build."""

    calls: list[tuple[list[str], Path]] = []

    def __call__(self, args, *, cwd=None, check=None, **_kw):  # noqa: ARG002
        _RecRun.calls.append((list(args), Path(cwd) if cwd else Path.cwd()))
        return subprocess.CompletedProcess(args, 0, stdout='', stderr='')


@pytest.fixture
def mount(tmp_path, monkeypatch):
    """Redirect the legacy _MOUNT_DIR / _ROOT_DIR to a per-test tmp path."""
    _RecPopen.calls = []
    _RecRun.calls = []
    monkeypatch.setattr(current, '_ROOT_DIR', tmp_path)
    mount_dir = tmp_path / 'agents' / 'repo' / 'itk'
    mount_dir.mkdir(parents=True)
    monkeypatch.setattr(current, '_MOUNT_DIR', mount_dir)
    monkeypatch.setattr(subprocess, 'Popen', _RecPopen)
    monkeypatch.setattr(current.subprocess, 'run', _RecRun())
    return mount_dir


# ---------------------------------------------------------------------------
# Contract: mount-missing → RuntimeError with the message CI expects
# ---------------------------------------------------------------------------


class TestMountMissing:
    def test_error_message_exact(self, tmp_path, monkeypatch):
        # Point _MOUNT_DIR at a path that does NOT exist.
        monkeypatch.setattr(current, '_MOUNT_DIR', tmp_path / 'not-mounted')
        with pytest.raises(RuntimeError) as e:
            current.spawn_agent(8001, 8002)
        # Exact message — some tooling greps for this.
        assert str(e.value) == (
            'current agent has not been mounted and is not available to test'
        )


# ---------------------------------------------------------------------------
# Contract: per-language argv + cwd
# ---------------------------------------------------------------------------


class TestPerLanguageArgv:
    """Locks the argv/cwd every SDK's CI already relies on."""

    HTTP = 8001
    GRPC = 8002

    def test_go(self, mount):
        (mount / 'main.go').write_text('package main', encoding='utf-8')
        current.spawn_agent(self.HTTP, self.GRPC)
        argv, cwd, _ = _RecPopen.calls[0]
        assert argv == [
            'go', 'run', '-mod=readonly', 'main.go',
            '--httpPort', str(self.HTTP),
            '--grpcPort', str(self.GRPC),
        ]
        assert cwd == mount

    def test_python(self, mount):
        (mount / 'main.py').write_text('# agent', encoding='utf-8')
        current.spawn_agent(self.HTTP, self.GRPC)
        argv, cwd, _ = _RecPopen.calls[0]
        assert argv == [
            'uv', 'run', '--locked', 'main.py',
            '--httpPort', str(self.HTTP),
            '--grpcPort', str(self.GRPC),
        ]
        assert cwd == mount

    def test_ts(self, mount):
        # TS: parent dir has package.json; mount is the itk sub-dir.
        (mount.parent / 'package.json').write_text('{}', encoding='utf-8')
        current.spawn_agent(self.HTTP, self.GRPC)
        argv, cwd, _ = _RecPopen.calls[0]
        assert argv == [
            'npm', 'run', 'itk-agent', '--',
            '--httpPort', str(self.HTTP),
            '--grpcPort', str(self.GRPC),
        ]
        assert cwd == mount

    def test_dotnet(self, mount):
        csproj = mount / 'Agent.csproj'
        csproj.write_text('<Project/>', encoding='utf-8')
        current.spawn_agent(self.HTTP, self.GRPC)
        argv, cwd, _ = _RecPopen.calls[0]
        assert argv == [
            'dotnet', 'run', '--project', str(csproj), '--',
            '--httpPort', str(self.HTTP),
            '--grpcPort', str(self.GRPC),
        ]
        assert cwd == mount

    def test_java_prebuild_and_exec(self, mount):
        (mount / 'pom.xml').write_text('<project/>', encoding='utf-8')
        current.spawn_agent(self.HTTP, self.GRPC)

        # 1) sync pre-build via subprocess.run — argv and cwd (mount.parent)
        assert _RecRun.calls, 'expected `mvn ... install` pre-build call'
        pre_argv, pre_cwd = _RecRun.calls[0]
        assert pre_argv == [
            'mvn', '-Pitk', '-pl', 'itk', '-am', 'install',
            '-DskipTests', '-Dmaven.javadoc.skip=true',
        ]
        assert pre_cwd == mount.parent

        # 2) async exec via Popen
        argv, cwd, _ = _RecPopen.calls[0]
        assert argv == [
            'mvn', 'exec:java',
            '-Dexec.mainClass=org.a2aproject.sdk.itk.Main',
            f'-Dexec.args=--httpPort {self.HTTP} --grpcPort {self.GRPC}',
        ]
        assert cwd == mount

    def test_rust_prebuilt_binary(self, mount):
        (mount / 'Cargo.toml').write_text('[package]\nname="x"\n', encoding='utf-8')
        release = mount / 'target' / 'release'
        release.mkdir(parents=True)
        canonical = release / 'itk-current-agent'
        canonical.write_text('bin', encoding='utf-8')
        canonical.chmod(0o755)

        current.spawn_agent(self.HTTP, self.GRPC)
        argv, cwd, _ = _RecPopen.calls[0]
        assert argv == [
            str(canonical),
            '--httpPort', str(self.HTTP),
            '--grpcPort', str(self.GRPC),
        ]
        assert cwd == mount

    def test_rust_prefers_canonical_over_alternates(self, mount):
        # If both a canonical and an alternate binary exist, canonical wins.
        (mount / 'Cargo.toml').write_text('[package]\nname="x"\n', encoding='utf-8')
        release = mount / 'target' / 'release'
        release.mkdir(parents=True)
        alt = release / 'itk-something-else'
        alt.write_text('bin', encoding='utf-8')
        alt.chmod(0o755)
        canonical = release / 'itk-current-agent'
        canonical.write_text('bin', encoding='utf-8')
        canonical.chmod(0o755)

        current.spawn_agent(self.HTTP, self.GRPC)
        argv, _cwd, _ = _RecPopen.calls[0]
        assert argv[0] == str(canonical)


# ---------------------------------------------------------------------------
# Contract: DEBUG log file goes to <root>/logs/agent_current.log
# ---------------------------------------------------------------------------


class TestDebugLog:
    def test_debug_opens_agent_current_log(self, mount, monkeypatch):
        (mount / 'main.py').write_text('# agent', encoding='utf-8')
        monkeypatch.setenv('ITK_LOG_LEVEL', 'DEBUG')
        proc = current.spawn_agent(8001, 8002)
        try:
            # The log file must be exactly logs/agent_current.log — developers
            # tail this path today; the launcher extraction must not rename it.
            assert (mount.parent.parent.parent / 'logs' / 'agent_current.log').exists()
            assert hasattr(proc, '_log_file')
            assert Path(proc._log_file.name).name == 'agent_current.log'  # noqa: SLF001
        finally:
            proc._log_file.close()  # noqa: SLF001


class TestNoSessionOnLegacyPath:
    """Regression: `spawn_agent` (legacy path used by every SDK's ITK CI)
    must NOT spawn in a new POSIX session. Legacy `testlib.stop_itk_cluster`
    only calls `proc.terminate()`, which signals the direct child only —
    detaching would leak grandchildren (mvn -> java, npm -> tsx, etc).

    Only `Cluster` opts into a new session (its teardown uses killpg).
    """

    def _capture_popen_kwargs(self, monkeypatch):
        seen: list[dict] = []

        class _CapturingPopen:
            def __init__(self, args, **kw):  # noqa: ARG002
                seen.append(kw)
                self.pid = 100
                self.args = args
            def wait(self, *_a, **_k):
                return 0
            def terminate(self):
                pass

        monkeypatch.setattr(subprocess, 'Popen', _CapturingPopen)
        return seen

    def test_legacy_spawn_agent_defaults_to_same_session(self, mount, monkeypatch):
        (mount / 'main.py').write_text('# agent', encoding='utf-8')
        seen = self._capture_popen_kwargs(monkeypatch)
        current.spawn_agent(8001, 8002)
        # Kwarg present on every recorded Popen — and False for the legacy path.
        assert seen, 'expected at least one Popen call'
        assert seen[0].get('start_new_session') is False, (
            f'legacy spawn_agent must not detach the session; '
            f'saw start_new_session={seen[0].get("start_new_session")!r}'
        )

    def test_direct_call_with_new_session_true_opts_in(self, mount, monkeypatch):
        (mount / 'main.py').write_text('# agent', encoding='utf-8')
        seen = self._capture_popen_kwargs(monkeypatch)
        current.spawn_from_dir(mount, 8001, 8002, new_session=True)
        assert seen[0].get('start_new_session') is True, (
            f'new_session=True must propagate to Popen; '
            f'saw start_new_session={seen[0].get("start_new_session")!r}'
        )

    def test_non_debug_no_log_handle(self, mount, monkeypatch):
        (mount / 'main.py').write_text('# agent', encoding='utf-8')
        monkeypatch.setenv('ITK_LOG_LEVEL', 'INFO')
        proc = current.spawn_agent(8001, 8002)
        assert not hasattr(proc, '_log_file'), (
            'INFO-level runs must not leave an open log handle'
        )

    def test_debug_default_when_env_unset(self, mount, monkeypatch):
        # Legacy code treated missing ITK_LOG_LEVEL as INFO. Preserve that.
        (mount / 'main.py').write_text('# agent', encoding='utf-8')
        monkeypatch.delenv('ITK_LOG_LEVEL', raising=False)
        proc = current.spawn_agent(8001, 8002)
        assert not hasattr(proc, '_log_file')
