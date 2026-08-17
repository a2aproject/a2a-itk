"""Spawn contract for :func:`test_suite.current.spawn_from_dir`.

Every agent in every scenario — the SUT (``MOUNT``) and each peer fetched
from its SDK repo (``CHECKOUT``) — starts through this one function, called
by ``launcher.resolve.spawn`` and ``launcher.cluster.Cluster``. This suite
pins the observable contract so a future edit can't silently regress a
language:

  * an unmounted SUT raises the exact error message CI greps for
  * per-language argv is byte-identical to what production sees today
  * cwd is the agent dir (or its parent for the Maven pre-build)
  * ``new_session`` stays opt-in

subprocess.Popen is patched so no real toolchain is required.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from test_suite import current
from test_suite.launcher import resolve
from test_suite.launcher.spec import Kind, TargetSpec


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
    """A per-test agent dir shaped like the container's bind mount."""
    _RecPopen.calls = []
    _RecRun.calls = []
    mount_dir = tmp_path / 'agents' / 'repo' / 'itk'
    mount_dir.mkdir(parents=True)
    monkeypatch.setattr(subprocess, 'Popen', _RecPopen)
    monkeypatch.setattr(current.subprocess, 'run', _RecRun())
    return mount_dir


# ---------------------------------------------------------------------------
# Contract: unmounted SUT → RuntimeError with the message CI expects
# ---------------------------------------------------------------------------


class TestMountMissing:
    def test_error_message_exact(self, tmp_path, monkeypatch):
        # The message originates in launcher.resolve, which is what decides
        # a MOUNT spec's directory. Point it at a path that isn't there.
        monkeypatch.setenv('ITK_MOUNT_DIR', str(tmp_path / 'not-mounted'))
        with pytest.raises(RuntimeError) as e:
            resolve.resolve(TargetSpec(kind=Kind.MOUNT))
        # Exact message — some tooling greps for this.
        assert str(e.value) == (
            'current agent has not been mounted and is not available to test'
        )

    def test_mount_dir_override_is_honoured(self, tmp_path, monkeypatch):
        # `run_tests.py --mount` works by setting this env var, so a MOUNT
        # spec must resolve to it rather than to the container path.
        local_sut = tmp_path / 'a2a-python' / 'itk'
        local_sut.mkdir(parents=True)
        monkeypatch.setenv('ITK_MOUNT_DIR', str(local_sut))
        assert resolve.resolve(TargetSpec(kind=Kind.MOUNT)) == local_sut

    def test_spawn_from_dir_rejects_missing_dir(self, tmp_path):
        with pytest.raises(RuntimeError, match='agent dir does not exist'):
            current.spawn_from_dir(tmp_path / 'nope', 8001, 8002)


# ---------------------------------------------------------------------------
# Contract: per-language argv + cwd
# ---------------------------------------------------------------------------


class TestPerLanguageArgv:
    """Locks the argv/cwd every SDK's CI already relies on."""

    HTTP = 8001
    GRPC = 8002

    def test_go(self, mount):
        (mount / 'main.go').write_text('package main', encoding='utf-8')
        current.spawn_from_dir(mount, self.HTTP, self.GRPC)
        argv, cwd, _ = _RecPopen.calls[0]
        assert argv == [
            'go', 'run', '-mod=readonly', 'main.go',
            '--httpPort', str(self.HTTP),
            '--grpcPort', str(self.GRPC),
        ]
        assert cwd == mount

    def test_python(self, mount):
        (mount / 'main.py').write_text('# agent', encoding='utf-8')
        current.spawn_from_dir(mount, self.HTTP, self.GRPC)
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
        current.spawn_from_dir(mount, self.HTTP, self.GRPC)
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
        current.spawn_from_dir(mount, self.HTTP, self.GRPC)
        argv, cwd, _ = _RecPopen.calls[0]
        assert argv == [
            'dotnet', 'run', '--project', str(csproj), '--',
            '--httpPort', str(self.HTTP),
            '--grpcPort', str(self.GRPC),
        ]
        assert cwd == mount

    def test_java_prebuild_and_exec(self, mount):
        (mount / 'pom.xml').write_text('<project/>', encoding='utf-8')
        current.spawn_from_dir(mount, self.HTTP, self.GRPC)

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

    def test_rust_builds_and_executes_canonical_binary(self, mount, monkeypatch):
        (mount / 'Cargo.toml').write_text('[package]\nname="x"\n', encoding='utf-8')
        target_dir = mount.parent.parent.parent / 'rust-target'
        monkeypatch.setenv('ITK_RUST_CURRENT_TARGET_DIR', str(target_dir))
        release = target_dir / 'release'
        release.mkdir(parents=True)
        canonical = release / 'itk-current-agent'
        canonical.write_text('bin', encoding='utf-8')
        canonical.chmod(0o755)

        current.spawn_from_dir(mount, self.HTTP, self.GRPC)
        argv, cwd, _ = _RecPopen.calls[0]
        assert argv == [
            str(canonical),
            '--httpPort', str(self.HTTP),
            '--grpcPort', str(self.GRPC),
        ]
        assert cwd == mount

    def test_rust_prefers_canonical_over_alternates(self, mount, monkeypatch):
        # If both a canonical and an alternate binary exist, canonical wins.
        (mount / 'Cargo.toml').write_text('[package]\nname="x"\n', encoding='utf-8')
        target_dir = mount.parent.parent.parent / 'rust-target'
        monkeypatch.setenv('ITK_RUST_CURRENT_TARGET_DIR', str(target_dir))
        release = target_dir / 'release'
        release.mkdir(parents=True)
        alt = release / 'itk-something-else'
        alt.write_text('bin', encoding='utf-8')
        alt.chmod(0o755)
        canonical = release / 'itk-current-agent'
        canonical.write_text('bin', encoding='utf-8')
        canonical.chmod(0o755)

        current.spawn_from_dir(mount, self.HTTP, self.GRPC)
        argv, _cwd, _ = _RecPopen.calls[0]
        assert argv[0] == str(canonical)


class TestNewSessionIsOptIn:
    """Regression: detaching the POSIX session must stay opt-in.

    A caller that reaps with `proc.terminate()` signals the direct child
    only, so detaching behind its back would leak grandchildren (mvn ->
    java, npm -> tsx, etc). Only `Cluster` opts in — its teardown uses
    killpg and can reap the whole group.
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

    def test_defaults_to_same_session(self, mount, monkeypatch):
        (mount / 'main.py').write_text('# agent', encoding='utf-8')
        seen = self._capture_popen_kwargs(monkeypatch)
        current.spawn_from_dir(mount, 8001, 8002)
        # Kwarg present on every recorded Popen — and False by default.
        assert seen, 'expected at least one Popen call'
        assert seen[0].get('start_new_session') is False, (
            f'spawn_from_dir must not detach the session unless asked; '
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

    def test_no_log_dir_leaves_no_log_handle(self, mount):
        (mount / 'main.py').write_text('# agent', encoding='utf-8')
        proc = current.spawn_from_dir(mount, 8001, 8002)
        assert not hasattr(proc, '_log_file'), (
            'runs without log_dir must not leave an open log handle'
        )
