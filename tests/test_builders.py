"""Builders: detection precedence and per-language argv.

Every builder is a ``subprocess.run`` invocation — we patch ``subprocess.run``
and record the argv/cwd, so no real toolchain is required. The purpose is
locking down: (a) detection precedence matches :mod:`.spawn`, and (b) the
exact commands and lockfile flags.

These tests pass ``skip_codegen=True`` to isolate the SDK-build step from
the codegen preparer (that lives in :mod:`test_suite.launcher.codegen` and has its
own test module). The end-to-end order (codegen ↔ build) is tested in
:class:`TestCodegenOrdering` at the bottom of this file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from test_suite.launcher import builders
from test_suite.launcher.builders import Language, detect_language
from test_suite.launcher.errors import InfraFailure, Stage


class _Recorder:
    """Records subprocess.run calls; returns success by default."""

    def __init__(self, returncode: int = 0):
        self.calls: list[tuple[list[str], Path]] = []
        self.returncode = returncode

    def __call__(self, args, *_, cwd=None, **__):
        self.calls.append((list(args), Path(cwd) if cwd else Path.cwd()))
        return subprocess.CompletedProcess(args, self.returncode, stdout='', stderr='')


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    r = _Recorder()
    monkeypatch.setattr(builders.subprocess, 'run', r)
    return r


# ---------------------------------------------------------------------------
# Detection precedence
# ---------------------------------------------------------------------------


class TestDetectLanguage:
    def _touch(self, root: Path, relpath: str, content: str = '') -> Path:
        p = root / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding='utf-8')
        return p

    def test_go(self, tmp_path):
        self._touch(tmp_path, 'main.go')
        assert detect_language(tmp_path) is Language.GO

    def test_python(self, tmp_path):
        self._touch(tmp_path, 'main.py')
        assert detect_language(tmp_path) is Language.PYTHON

    def test_ts_via_parent_package_json(self, tmp_path):
        # TS agents live one level deep; the parent has package.json.
        self._touch(tmp_path, 'package.json')
        agent = tmp_path / 'itk'
        agent.mkdir()
        assert detect_language(agent) is Language.TS

    def test_dotnet(self, tmp_path):
        self._touch(tmp_path, 'Agent.csproj')
        assert detect_language(tmp_path) is Language.DOTNET

    def test_java(self, tmp_path):
        self._touch(tmp_path, 'pom.xml')
        assert detect_language(tmp_path) is Language.JAVA

    def test_rust(self, tmp_path):
        self._touch(tmp_path, 'Cargo.toml')
        assert detect_language(tmp_path) is Language.RUST

    def test_unknown_raises(self, tmp_path):
        with pytest.raises(RuntimeError):
            detect_language(tmp_path)

    def test_go_beats_python(self, tmp_path):
        # If both are present, Go wins — must match spawn.spawn_from_dir order.
        self._touch(tmp_path, 'main.go')
        self._touch(tmp_path, 'main.py')
        assert detect_language(tmp_path) is Language.GO

    def test_python_beats_java(self, tmp_path):
        self._touch(tmp_path, 'main.py')
        self._touch(tmp_path, 'pom.xml')
        assert detect_language(tmp_path) is Language.PYTHON


# ---------------------------------------------------------------------------
# Per-builder argv
# ---------------------------------------------------------------------------


class TestPythonBuilder:
    def test_uv_sync_locked(self, tmp_path, rec):
        (tmp_path / 'main.py').touch()
        builders.build_in_place('x/y', 'a' * 40, tmp_path, skip_codegen=True)
        assert rec.calls[0][0] == ['uv', 'sync', '--locked']
        assert rec.calls[0][1] == tmp_path


class TestGoBuilder:
    def test_go_build_uses_readonly_mode(self, tmp_path, rec):
        (tmp_path / 'main.go').touch()
        builders.build_in_place('x/y', 'a' * 40, tmp_path, skip_codegen=True)
        assert rec.calls[0][0] == [
            'go', 'build', '-mod=readonly', '-o', str(tmp_path / 'bin' / 'agent'), '.',
        ]
        assert rec.calls[0][1] == tmp_path
        assert (tmp_path / 'bin').is_dir()

    def test_go_skip_if_binary_exists(self, tmp_path, rec):
        (tmp_path / 'main.go').touch()
        (tmp_path / 'bin').mkdir()
        (tmp_path / 'bin' / 'agent').write_text('binary', encoding='utf-8')
        builders.build_in_place('x/y', 'a' * 40, tmp_path, skip_codegen=True)
        assert rec.calls == []


class TestJavaBuilder:
    def test_mvn_profile_and_cwd(self, tmp_path, rec):
        (tmp_path / 'pom.xml').touch()
        builders.build_in_place('x/y', 'a' * 40, tmp_path, skip_codegen=True)
        assert rec.calls[0][0] == [
            'mvn', '-Pitk', '-pl', 'itk', '-am', 'install',
            '-DskipTests', '-Dmaven.javadoc.skip=true',
        ]
        # Maven must run from the *parent* — itk is a submodule of the SDK repo.
        assert rec.calls[0][1] == tmp_path.parent


class TestRustBuilder:
    def test_cargo_build_locked_release(self, tmp_path, rec):
        (tmp_path / 'Cargo.toml').touch()
        builders.build_in_place('x/y', 'a' * 40, tmp_path, skip_codegen=True)
        assert rec.calls[0][0] == ['cargo', 'build', '--locked', '--release']
        assert rec.calls[0][1] == tmp_path

    def test_rust_skip_if_binary_exists(self, tmp_path, rec):
        (tmp_path / 'Cargo.toml').touch()
        rel = tmp_path / 'target' / 'release'
        rel.mkdir(parents=True)
        (rel / 'itk-something').write_text('x', encoding='utf-8')
        builders.build_in_place('x/y', 'a' * 40, tmp_path, skip_codegen=True)
        assert rec.calls == []


class TestTsBuilder:
    def test_npm_ci_at_repo_root(self, tmp_path, rec):
        (tmp_path / 'package.json').touch()
        agent = tmp_path / 'itk'
        agent.mkdir()
        builders.build_in_place('x/y', 'a' * 40, agent, skip_codegen=True)
        assert rec.calls[0][0] == ['npm', 'ci']
        # npm ci must run from the *repo root*, not the agent subdir.
        assert rec.calls[0][1] == tmp_path

    def test_ts_skip_if_node_modules_exists(self, tmp_path, rec):
        (tmp_path / 'package.json').touch()
        (tmp_path / 'node_modules').mkdir()
        agent = tmp_path / 'itk'
        agent.mkdir()
        builders.build_in_place('x/y', 'a' * 40, agent, skip_codegen=True)
        assert rec.calls == []

    def test_ts_inner_npm_ci_for_overlay_with_own_package_json(
        self, tmp_path, rec,
    ):
        """Overlay-style agents (v03) declare their own itk/package.json
        pinning @a2a-js/sdk at the matching version. Root npm ci alone
        doesn't populate <agent>/node_modules, so the launcher must also
        install there or the agent crashes with ERR_MODULE_NOT_FOUND at
        spawn time."""
        (tmp_path / 'package.json').touch()
        agent = tmp_path / 'itk'
        agent.mkdir()
        (agent / 'package.json').touch()
        builders.build_in_place('x/y', 'a' * 40, agent, skip_codegen=True)
        # Root install first, then inner install.
        assert rec.calls == [
            (['npm', 'ci'], tmp_path),
            (['npm', 'ci'], agent),
        ]

    def test_ts_inner_install_skipped_when_node_modules_already_present(
        self, tmp_path, rec,
    ):
        """v03 overlay re-runs must not reinstall if inner node_modules is
        already there (typical of a warm launcher cache)."""
        (tmp_path / 'package.json').touch()
        agent = tmp_path / 'itk'
        agent.mkdir()
        (agent / 'package.json').touch()
        (agent / 'node_modules').mkdir()
        builders.build_in_place('x/y', 'a' * 40, agent, skip_codegen=True)
        # Only the root install ran; inner was skipped.
        assert rec.calls == [(['npm', 'ci'], tmp_path)]

    def test_ts_v10_pattern_no_inner_install(self, tmp_path, rec):
        """v10 agents share the SDK root's node_modules and have no
        itk/package.json — the inner install must not fire."""
        (tmp_path / 'package.json').touch()
        agent = tmp_path / 'itk'
        agent.mkdir()
        # No agent/package.json (v10 pattern)
        builders.build_in_place('x/y', 'a' * 40, agent, skip_codegen=True)
        assert rec.calls == [(['npm', 'ci'], tmp_path)]


class TestDotnetBuilder:
    def test_dotnet_is_noop(self, tmp_path, rec):
        (tmp_path / 'Agent.csproj').touch()
        lang = builders.build_in_place('x/y', 'a' * 40, tmp_path, skip_codegen=True)
        assert lang is Language.DOTNET
        assert rec.calls == []


# ---------------------------------------------------------------------------
# Failure wrapping
# ---------------------------------------------------------------------------


class TestFailureWrapping:
    def test_non_zero_becomes_infra_failure(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        (tmp_path / 'main.py').touch()

        def boom(*_a: Any, **_k: Any) -> Any:
            raise subprocess.CalledProcessError(returncode=1, cmd=['uv'], stderr='nope')

        monkeypatch.setattr(builders.subprocess, 'run', boom)
        with pytest.raises(InfraFailure) as e:
            builders.build_in_place('x/y', 'a' * 40, tmp_path, skip_codegen=True)
        assert e.value.stage is Stage.BUILD
        assert e.value.repo == 'x/y'

    def test_timeout_becomes_infra_failure(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ):
        (tmp_path / 'main.py').touch()

        def hang(*_a: Any, **_k: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd=['uv'], timeout=1)

        monkeypatch.setattr(builders.subprocess, 'run', hang)
        with pytest.raises(InfraFailure) as e:
            builders.build_in_place('x/y', 'a' * 40, tmp_path, skip_codegen=True)
        assert e.value.stage is Stage.BUILD


# ---------------------------------------------------------------------------
# Codegen ↔ build ordering
# ---------------------------------------------------------------------------


class TestCodegenOrdering:
    """The end-to-end ordering: some languages need codegen BEFORE the SDK
    build tool runs; others AFTER. Verified by observing which of the
    recorded subprocess calls fires first.
    """

    def _first_cmd(self, rec, cmd: str) -> int | None:
        """Return index of the first call whose argv[0] equals cmd."""
        for i, (argv, _cwd) in enumerate(rec.calls):
            if argv and argv[0] == cmd:
                return i
        return None

    def test_go_codegen_runs_before_go_build(self, tmp_path, rec):
        (tmp_path / 'main.go').touch()
        # Real codegen would need protoc; the Recorder returns 0 for anything.
        builders.build_in_place('x/y', 'a' * 40, tmp_path)
        protoc_at = self._first_cmd(rec, 'protoc')
        gobuild_at = self._first_cmd(rec, 'go')
        assert protoc_at is not None, 'expected protoc call from codegen.prepare_go'
        assert gobuild_at is not None, 'expected go build call'
        assert protoc_at < gobuild_at, 'codegen must run before go build'

    def test_python_codegen_runs_after_uv_sync(self, tmp_path, rec):
        (tmp_path / 'main.py').touch()
        builders.build_in_place('x/y', 'a' * 40, tmp_path)
        # Both are 'uv ...' — distinguish by the subcommand.
        first_uv_sub = rec.calls[0][0][1]
        assert first_uv_sub == 'sync', (
            f'expected uv sync first; got {rec.calls[0][0]!r}'
        )
        # Codegen uses `uv run --with grpcio-tools python -m grpc_tools.protoc`.
        assert any('grpc_tools.protoc' in ' '.join(argv) for argv, _ in rec.calls), (
            'codegen protoc call missing'
        )

    def test_ts_codegen_runs_after_npm_ci(self, tmp_path, rec):
        (tmp_path / 'package.json').touch()
        agent = tmp_path / 'itk'
        agent.mkdir()
        builders.build_in_place('x/y', 'a' * 40, agent)
        # npm ci comes first
        assert rec.calls[0][0] == ['npm', 'ci']
        # buf generate is somewhere after — argv[0] ends with '/node_modules/.bin/buf'
        buf_at = None
        for i, (argv, _cwd) in enumerate(rec.calls):
            if argv and argv[0].endswith('/node_modules/.bin/buf'):
                buf_at = i
                break
        assert buf_at is not None and buf_at > 0

    def test_rust_codegen_creates_symlink_before_cargo(self, tmp_path, rec):
        (tmp_path / 'Cargo.toml').touch()
        builders.build_in_place('x/y', 'a' * 40, tmp_path)
        # No subprocess for rust codegen — just a symlink.
        assert (tmp_path / 'a2a-itk').is_symlink(), (
            'codegen must symlink a2a-itk into rust agent dir'
        )
        # Only one recorded call — cargo build (rust codegen has no subprocess).
        assert rec.calls[0][0] == ['cargo', 'build', '--locked', '--release']

    def test_java_codegen_creates_symlink_before_mvn(self, tmp_path, rec):
        (tmp_path / 'pom.xml').touch()
        builders.build_in_place('x/y', 'a' * 40, tmp_path)
        assert (tmp_path / 'a2a-itk').is_symlink()
        assert rec.calls[0][0][0] == 'mvn'

    def test_skip_codegen_bypasses_all(self, tmp_path, rec):
        (tmp_path / 'Cargo.toml').touch()
        builders.build_in_place('x/y', 'a' * 40, tmp_path, skip_codegen=True)
        assert not (tmp_path / 'a2a-itk').exists()
        assert rec.calls[0][0] == ['cargo', 'build', '--locked', '--release']

    def test_dotnet_has_no_codegen(self, tmp_path, rec):
        (tmp_path / 'Agent.csproj').touch()
        builders.build_in_place('x/y', 'a' * 40, tmp_path)
        assert rec.calls == []
        assert not (tmp_path / 'a2a-itk').exists()
