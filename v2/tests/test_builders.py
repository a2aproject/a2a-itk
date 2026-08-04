"""Builders: detection precedence and per-language argv.

Every builder is a ``subprocess.run`` invocation — we patch ``subprocess.run``
and record the argv/cwd, so no real toolchain is required. The purpose is
locking down: (a) detection precedence matches :mod:`.spawn`, and (b) the
exact commands and lockfile flags.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from v2.launcher import builders
from v2.launcher.builders import Language, detect_language
from v2.launcher.errors import InfraFailure, Stage


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
        builders.build_in_place('x/y', 'a' * 40, tmp_path)
        assert rec.calls[0][0] == ['uv', 'sync', '--locked']
        assert rec.calls[0][1] == tmp_path


class TestGoBuilder:
    def test_go_build_uses_readonly_mode(self, tmp_path, rec):
        (tmp_path / 'main.go').touch()
        builders.build_in_place('x/y', 'a' * 40, tmp_path)
        assert rec.calls[0][0] == [
            'go', 'build', '-mod=readonly', '-o', str(tmp_path / 'bin' / 'agent'), '.',
        ]
        assert rec.calls[0][1] == tmp_path
        assert (tmp_path / 'bin').is_dir()

    def test_go_skip_if_binary_exists(self, tmp_path, rec):
        (tmp_path / 'main.go').touch()
        (tmp_path / 'bin').mkdir()
        (tmp_path / 'bin' / 'agent').write_text('binary', encoding='utf-8')
        builders.build_in_place('x/y', 'a' * 40, tmp_path)
        assert rec.calls == []


class TestJavaBuilder:
    def test_mvn_profile_and_cwd(self, tmp_path, rec):
        (tmp_path / 'pom.xml').touch()
        builders.build_in_place('x/y', 'a' * 40, tmp_path)
        assert rec.calls[0][0] == [
            'mvn', '-Pitk', '-pl', 'itk', '-am', 'install',
            '-DskipTests', '-Dmaven.javadoc.skip=true',
        ]
        # Maven must run from the *parent* — itk is a submodule of the SDK repo.
        assert rec.calls[0][1] == tmp_path.parent


class TestRustBuilder:
    def test_cargo_build_locked_release(self, tmp_path, rec):
        (tmp_path / 'Cargo.toml').touch()
        builders.build_in_place('x/y', 'a' * 40, tmp_path)
        assert rec.calls[0][0] == ['cargo', 'build', '--locked', '--release']
        assert rec.calls[0][1] == tmp_path

    def test_rust_skip_if_binary_exists(self, tmp_path, rec):
        (tmp_path / 'Cargo.toml').touch()
        rel = tmp_path / 'target' / 'release'
        rel.mkdir(parents=True)
        (rel / 'itk-something').write_text('x', encoding='utf-8')
        builders.build_in_place('x/y', 'a' * 40, tmp_path)
        assert rec.calls == []


class TestTsBuilder:
    def test_npm_ci_at_repo_root(self, tmp_path, rec):
        (tmp_path / 'package.json').touch()
        agent = tmp_path / 'itk'
        agent.mkdir()
        builders.build_in_place('x/y', 'a' * 40, agent)
        assert rec.calls[0][0] == ['npm', 'ci']
        # npm ci must run from the *repo root*, not the agent subdir.
        assert rec.calls[0][1] == tmp_path

    def test_ts_skip_if_node_modules_exists(self, tmp_path, rec):
        (tmp_path / 'package.json').touch()
        (tmp_path / 'node_modules').mkdir()
        agent = tmp_path / 'itk'
        agent.mkdir()
        builders.build_in_place('x/y', 'a' * 40, agent)
        assert rec.calls == []


class TestDotnetBuilder:
    def test_dotnet_is_noop(self, tmp_path, rec):
        (tmp_path / 'Agent.csproj').touch()
        lang = builders.build_in_place('x/y', 'a' * 40, tmp_path)
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
            builders.build_in_place('x/y', 'a' * 40, tmp_path)
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
            builders.build_in_place('x/y', 'a' * 40, tmp_path)
        assert e.value.stage is Stage.BUILD
