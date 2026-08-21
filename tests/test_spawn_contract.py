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
    """subprocess.run stub for the java pre-build and rust cargo build."""

    calls: list[tuple[list[str], Path]] = []
    envs: list[dict | None] = []

    def __call__(self, args, *, cwd=None, check=None, env=None, **_kw):  # noqa: ARG002
        _RecRun.calls.append((list(args), Path(cwd) if cwd else Path.cwd()))
        _RecRun.envs.append(env)
        return subprocess.CompletedProcess(args, 0, stdout='', stderr='')


@pytest.fixture
def mount(tmp_path, monkeypatch):
    """A per-test agent dir shaped like the container's bind mount."""
    _RecPopen.calls = []
    _RecRun.calls = []
    _RecRun.envs = []
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

    def test_dotnet_uses_a_prepublished_dll_without_building(self, mount):
        """A repo whose SDK requirements exceed the image publishes on the
        host; the launcher must then only run the output."""
        csproj = mount / 'Agent.csproj'
        csproj.write_text('<Project/>', encoding='utf-8')
        dll = mount / 'publish' / 'Agent.dll'
        dll.parent.mkdir()
        dll.write_text('assembly', encoding='utf-8')

        current.spawn_from_dir(mount, self.HTTP, self.GRPC)

        assert not _RecRun.calls, 'must not build when publish output exists'
        argv, cwd, _ = _RecPopen.calls[0]
        assert argv == [
            'dotnet', str(dll),
            '--httpPort', str(self.HTTP),
            '--grpcPort', str(self.GRPC),
        ]
        assert cwd == mount

    def test_dotnet_publishes_then_execs_when_not_prebuilt(self, mount, monkeypatch):
        """Never `dotnet run`: it builds inside the readiness window and
        defaults to Debug, so it cannot reuse a Release publish."""
        csproj = mount / 'Agent.csproj'
        csproj.write_text('<Project/>', encoding='utf-8')
        dll = mount / 'publish' / 'Agent.dll'

        # Stand in for the real publish, which the recording stub skips.
        def fake_run(argv, cwd=None, **kw):  # noqa: ANN001, ARG001
            _RecRun.calls.append((argv, Path(cwd) if cwd else None))
            dll.parent.mkdir(parents=True, exist_ok=True)
            dll.write_text('assembly', encoding='utf-8')
            return subprocess.CompletedProcess(argv, 0)
        monkeypatch.setattr(current.subprocess, 'run', fake_run)

        current.spawn_from_dir(mount, self.HTTP, self.GRPC)

        pre_argv, pre_cwd = _RecRun.calls[0]
        assert pre_argv == [
            'dotnet', 'publish', str(csproj),
            '-c', 'Release', '-o', str(mount / 'publish'),
        ]
        assert pre_cwd == mount

        argv, cwd, _ = _RecPopen.calls[0]
        assert argv[:2] == ['dotnet', str(dll)]
        assert 'run' not in argv
        assert cwd == mount

    def test_dotnet_reports_a_missing_assembly_clearly(self, mount, monkeypatch):
        """A publish that produces a differently-named assembly should say so,
        not fail later as an opaque readiness timeout."""
        csproj = mount / 'Agent.csproj'
        csproj.write_text('<Project/>', encoding='utf-8')

        def fake_run(argv, cwd=None, **kw):  # noqa: ANN001, ARG001
            return subprocess.CompletedProcess(argv, 0)
        monkeypatch.setattr(current.subprocess, 'run', fake_run)

        with pytest.raises(RuntimeError, match='Agent.dll is not in'):
            current.spawn_from_dir(mount, self.HTTP, self.GRPC)

    def test_a_csproj_dir_is_detected_as_dotnet(self, mount):
        """Regression guard: a .NET agent must not need a foreign marker file
        to control how it starts."""
        (mount / 'Agent.csproj').write_text('<Project/>', encoding='utf-8')
        (mount / 'publish').mkdir()
        (mount / 'publish' / 'Agent.dll').write_text('a', encoding='utf-8')
        current.spawn_from_dir(mount, self.HTTP, self.GRPC)
        argv, _, _ = _RecPopen.calls[0]
        assert argv[0] == 'dotnet'
        assert 'uv' not in argv

    def test_java_prebuild_and_exec(self, mount):
        (mount / 'pom.xml').write_text('<project/>', encoding='utf-8')
        current.spawn_from_dir(mount, self.HTTP, self.GRPC)

        # 1) sync pre-build via subprocess.run — argv and cwd (mount.parent)
        assert _RecRun.calls, 'expected `mvn ... install` pre-build call'
        pre_argv, pre_cwd = _RecRun.calls[0]
        local_repo = f'-Dmaven.repo.local={current.maven_repo_dir(mount)}'
        assert pre_argv == [
            'mvn', '-Pitk', '-pl', 'itk', '-am', 'install',
            '-DskipTests', '-Dmaven.javadoc.skip=true',
            local_repo,
        ]
        assert pre_cwd == mount.parent

        # 2) async exec via Popen
        argv, cwd, _ = _RecPopen.calls[0]
        assert argv == [
            'mvn', 'exec:java',
            '-Dexec.mainClass=org.a2aproject.sdk.itk.Main',
            f'-Dexec.args=--httpPort {self.HTTP} --grpcPort {self.GRPC}',
            local_repo,
        ]
        assert cwd == mount

    def test_rust_builds_and_executes_canonical_binary(self, mount, monkeypatch):
        (mount / 'Cargo.toml').write_text('[package]\nname="x"\n', encoding='utf-8')
        target_dir = mount.parent.parent.parent / 'rust-target'
        monkeypatch.setenv('ITK_RUST_CURRENT_TARGET_DIR', str(target_dir))
        release = current.rust_target_dir(mount) / 'release'
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
        release = current.rust_target_dir(mount) / 'release'
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

    def test_rust_isolates_target_dir_per_agent(self, mount, tmp_path, monkeypatch):
        target_root = tmp_path / 'rust-targets'
        monkeypatch.setenv('ITK_RUST_CURRENT_TARGET_DIR', str(target_root))
        (mount / 'Cargo.toml').write_text(
            '[package]\nname="itk-rust-current-agent"\n', encoding='utf-8',
        )
        rust_v10 = tmp_path / 'checkout' / 'a2a-rs' / 'itk'
        rust_v10.mkdir(parents=True)
        (rust_v10 / 'Cargo.toml').write_text(
            '[package]\nname="itk-rust-current-agent"\n', encoding='utf-8',
        )

        binaries = []
        for agent_dir in (mount, rust_v10):
            release = current.rust_target_dir(agent_dir) / 'release'
            release.mkdir(parents=True)
            binary = release / 'itk-rust-current-agent'
            binary.write_text(agent_dir.name, encoding='utf-8')
            binary.chmod(0o755)
            binaries.append(binary)

        current.spawn_from_dir(mount, self.HTTP, self.GRPC)
        current.spawn_from_dir(rust_v10, self.HTTP, self.GRPC)

        env_current, env_v10 = _RecRun.envs
        assert env_current is not None and env_v10 is not None
        assert env_current['CARGO_TARGET_DIR'] != env_v10['CARGO_TARGET_DIR']
        assert Path(env_current['CARGO_TARGET_DIR']).parent == target_root
        assert Path(env_v10['CARGO_TARGET_DIR']).parent == target_root

        argv_current = _RecPopen.calls[0][0]
        argv_v10 = _RecPopen.calls[1][0]
        assert argv_current[0] == str(binaries[0])
        assert argv_v10[0] == str(binaries[1])
        assert argv_current[0] != argv_v10[0]

    def test_rust_skips_depinfo_and_non_executables(self, mount, monkeypatch):
        (mount / 'Cargo.toml').write_text('[package]\nname="x"\n', encoding='utf-8')
        target_dir = mount.parent.parent.parent / 'rust-target'
        monkeypatch.setenv('ITK_RUST_CURRENT_TARGET_DIR', str(target_dir))
        release = current.rust_target_dir(mount) / 'release'
        release.mkdir(parents=True)
        depinfo = release / 'itk-rust-current-agent.d'
        depinfo.write_text('dep', encoding='utf-8')
        non_exec = release / 'itk-current-agent'
        non_exec.write_text('not exec', encoding='utf-8')
        non_exec.chmod(0o644)
        real = release / 'itk-fallback-agent'
        real.write_text('bin', encoding='utf-8')
        real.chmod(0o755)

        current.spawn_from_dir(mount, self.HTTP, self.GRPC)
        argv, _cwd, _ = _RecPopen.calls[0]
        assert argv[0] == str(real)


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


class TestDotnetBuildPhase:
    """The seam between the build phase and spawn.

    The builder's own argv, idempotence and timeout live in
    ``test_builders.py`` alongside every other language. What matters *here*
    is only that the two modules share one recipe: if they drift, the build
    publishes somewhere spawn doesn't look and every start silently pays for
    a second publish inside the readiness window.
    """

    def test_build_and_spawn_share_one_recipe(self, tmp_path):
        from test_suite.launcher import builders

        d = tmp_path / 'itk'
        d.mkdir()
        csproj = d / 'Agent.csproj'
        csproj.write_text('<Project/>', encoding='utf-8')

        assert builders.dotnet_csproj is current.dotnet_csproj
        assert builders.dotnet_publish_dll is current.dotnet_publish_dll
        assert builders.dotnet_publish_args is current.dotnet_publish_args
        assert current.dotnet_publish_dll(d, csproj) == d / 'publish' / 'Agent.dll'

    def test_project_choice_is_stable_when_a_dir_holds_two(self, tmp_path):
        """Both sides must pick the *same* project, deterministically.

        Raw ``glob`` order is filesystem order, which differs between the
        build host and a later run over the cached tree. Picking differently
        would publish one assembly and then exec-miss on the other.
        """
        d = tmp_path / 'itk'
        d.mkdir()
        (d / 'Zeta.csproj').write_text('<Project/>', encoding='utf-8')
        (d / 'Agent.csproj').write_text('<Project/>', encoding='utf-8')

        chosen = current.dotnet_csproj(d)
        assert chosen is not None
        assert chosen.name == 'Agent.csproj', 'expected a sorted, stable choice'
        assert current.dotnet_csproj(d) == chosen

    def test_no_project_is_reported_not_crashed(self, tmp_path):
        d = tmp_path / 'itk'
        d.mkdir()
        assert current.dotnet_csproj(d) is None
