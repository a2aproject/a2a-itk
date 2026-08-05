"""Codegen: per-language proto preparation.

Every codegen function is a mix of file operations (copies, symlinks) and
subprocess calls (protoc, buf). We stub subprocess.run so no real toolchain
is required — the point is locking down the exact argv, output paths, and
symlink layout, not proving that grpc_tools actually works.

The real toolchain smoke happens interactively (per-language build+spawn
smoke against real SDK repos, documented in the task history).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from v2.launcher import codegen


class _Recorder:
    """Captures subprocess.run calls; returns success."""

    def __init__(self):
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, args, *_a, cwd=None, **_kw):
        self.calls.append((list(args), Path(cwd) if cwd else Path.cwd()))
        return subprocess.CompletedProcess(args, 0, stdout='', stderr='')


@pytest.fixture
def rec(monkeypatch):
    r = _Recorder()
    monkeypatch.setattr(codegen.subprocess, 'run', r)
    return r


@pytest.fixture
def proto_file(tmp_path):
    """A fake instruction.proto we can copy from."""
    p = tmp_path / 'src_protos' / 'instruction.proto'
    p.parent.mkdir(parents=True)
    p.write_text('syntax = "proto3";', encoding='utf-8')
    return p


# ---------------------------------------------------------------------------
# ensure_itk_link
# ---------------------------------------------------------------------------


class TestEnsureItkLink:
    def test_creates_symlink_when_absent(self, tmp_path):
        agent = tmp_path / 'agent'
        agent.mkdir()
        itk = tmp_path / 'itk_source'
        itk.mkdir()
        codegen.ensure_itk_link(agent, itk)
        link = agent / 'a2a-itk'
        assert link.is_symlink()
        assert link.resolve() == itk.resolve()

    def test_idempotent_when_correct_target(self, tmp_path):
        agent = tmp_path / 'agent'
        agent.mkdir()
        itk = tmp_path / 'itk_source'
        itk.mkdir()
        codegen.ensure_itk_link(agent, itk)
        mtime_before = (agent / 'a2a-itk').lstat().st_mtime
        codegen.ensure_itk_link(agent, itk)  # should not recreate
        assert (agent / 'a2a-itk').lstat().st_mtime == mtime_before

    def test_replaces_symlink_pointing_elsewhere(self, tmp_path):
        agent = tmp_path / 'agent'
        agent.mkdir()
        old = tmp_path / 'old'
        old.mkdir()
        new = tmp_path / 'new'
        new.mkdir()
        (agent / 'a2a-itk').symlink_to(old)
        codegen.ensure_itk_link(agent, new)
        assert (agent / 'a2a-itk').resolve() == new.resolve()

    def test_leaves_real_directory_alone(self, tmp_path):
        agent = tmp_path / 'agent'
        agent.mkdir()
        real = agent / 'a2a-itk'
        real.mkdir()
        (real / 'marker').write_text('user', encoding='utf-8')
        itk = tmp_path / 'itk_source'
        itk.mkdir()
        codegen.ensure_itk_link(agent, itk)
        # Not a symlink; marker still there.
        assert not (agent / 'a2a-itk').is_symlink()
        assert (real / 'marker').read_text(encoding='utf-8') == 'user'


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


class TestPreparePython:
    def test_copies_proto_creates_pyproto_and_runs_protoc(
        self, tmp_path, proto_file, rec,
    ):
        agent = tmp_path / 'agent'
        agent.mkdir()

        # Simulate what protoc would write, so the import-patch step has a file.
        def fake_run(args, *_a, cwd=None, **_kw):
            rec(args, cwd=cwd)
            pyproto = Path(cwd) / 'pyproto'
            (pyproto / 'instruction_pb2.py').write_text('# fake', encoding='utf-8')
            (pyproto / 'instruction_pb2_grpc.py').write_text(
                'import instruction_pb2 as instruction__pb2\n',
                encoding='utf-8',
            )
            return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

        # Overwrite the recorder with the writing variant.
        codegen.subprocess.run = fake_run  # type: ignore[assignment]
        try:
            codegen.prepare_python(agent, proto_source=proto_file)
        finally:
            codegen.subprocess.run = subprocess.run  # restore

        assert (agent / 'instruction.proto').exists()
        assert (agent / 'pyproto' / '__init__.py').exists()
        assert rec.calls, 'expected a subprocess call'
        argv, cwd = rec.calls[0]
        assert argv == [
            'uv', 'run', '--with', 'grpcio-tools',
            'python', '-m', 'grpc_tools.protoc',
            '-I.', '--python_out=pyproto', '--grpc_python_out=pyproto',
            'instruction.proto',
        ]
        assert cwd == agent
        # Import-patch fired: line must start with `from .` now.
        patched = (agent / 'pyproto' / 'instruction_pb2_grpc.py').read_text(encoding='utf-8')
        assert patched.startswith('from . import instruction_pb2 as instruction__pb2')
        # And the bare unpatched form (line starting with `import`) is gone.
        assert '\nimport instruction_pb2 as instruction__pb2' not in patched
        assert not patched.startswith('import instruction_pb2 as instruction__pb2')

    def test_idempotent_skips_if_pb2_exists(self, tmp_path, proto_file, rec):
        agent = tmp_path / 'agent'
        (agent / 'pyproto').mkdir(parents=True)
        (agent / 'pyproto' / 'instruction_pb2.py').write_text('# existing', encoding='utf-8')
        codegen.prepare_python(agent, proto_source=proto_file)
        assert rec.calls == []
        # Existing content preserved
        assert (agent / 'pyproto' / 'instruction_pb2.py').read_text(encoding='utf-8') == '# existing'


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------


class TestPrepareGo:
    def test_copies_proto_creates_pb_and_runs_protoc(
        self, tmp_path, proto_file, rec,
    ):
        agent = tmp_path / 'agent'
        agent.mkdir()
        codegen.prepare_go(agent, proto_source=proto_file)
        assert (agent / 'instruction.proto').exists()
        assert (agent / 'pb').is_dir()
        argv, cwd = rec.calls[0]
        assert argv[0] == 'protoc'
        assert '--go_out=pb' in argv
        assert '--go-grpc_out=pb' in argv
        assert '--go_opt=Minstruction.proto=github.com/a2aproject/a2a-go/itk/pb' in argv
        assert 'instruction.proto' in argv
        assert cwd == agent

    def test_idempotent_skips_if_pb_exists(self, tmp_path, proto_file, rec):
        agent = tmp_path / 'agent'
        (agent / 'pb').mkdir(parents=True)
        (agent / 'pb' / 'instruction.pb.go').write_text('// existing', encoding='utf-8')
        codegen.prepare_go(agent, proto_source=proto_file)
        assert rec.calls == []


# ---------------------------------------------------------------------------
# TS
# ---------------------------------------------------------------------------


class TestPrepareTs:
    def _stage(self, tmp_path, proto_file):
        repo_root = tmp_path / 'a2a-js'
        repo_root.mkdir()
        (repo_root / 'node_modules' / '.bin').mkdir(parents=True)
        (repo_root / 'node_modules' / '.bin' / 'buf').write_text('#!/bin/sh', encoding='utf-8')
        agent = repo_root / 'itk'
        agent.mkdir()
        itk = tmp_path / 'a2a-itk-source'
        (itk / 'agents' / 'ts' / 'v10').mkdir(parents=True)
        return repo_root, agent, itk

    def test_symlinks_stages_proto_and_runs_buf(self, tmp_path, proto_file, rec):
        repo_root, agent, itk = self._stage(tmp_path, proto_file)
        codegen.prepare_ts(agent, proto_source=proto_file, itk_source=itk)

        # a2a-itk was symlinked into agent dir
        link = agent / 'a2a-itk'
        assert link.is_symlink()
        assert link.resolve() == itk.resolve()

        # buf was invoked from ts/v10 dir
        argv, cwd = rec.calls[0]
        assert argv[0].endswith('/node_modules/.bin/buf')
        assert argv[1] == 'generate'
        expected_ts_dir = agent / 'a2a-itk' / 'agents' / 'ts' / 'v10'
        assert cwd == expected_ts_dir

        # Staging protos dir was cleaned up (finally block)
        assert not (expected_ts_dir / 'protos').exists()

    def test_cleans_up_stage_on_failure(self, tmp_path, proto_file, monkeypatch):
        _repo_root, agent, itk = self._stage(tmp_path, proto_file)

        def bad(*_a, **_kw):
            raise subprocess.CalledProcessError(returncode=1, cmd=['buf'], stderr='fail')

        monkeypatch.setattr(codegen.subprocess, 'run', bad)
        with pytest.raises(subprocess.CalledProcessError):
            codegen.prepare_ts(agent, proto_source=proto_file, itk_source=itk)

        ts_dir = agent / 'a2a-itk' / 'agents' / 'ts' / 'v10'
        assert not (ts_dir / 'protos').exists(), (
            'staging protos dir must be cleaned even when buf fails'
        )


# ---------------------------------------------------------------------------
# Rust + Java (symlink-only)
# ---------------------------------------------------------------------------


class TestPrepareRust:
    def test_symlink_only_no_subprocess(self, tmp_path, rec):
        agent = tmp_path / 'agent'
        agent.mkdir()
        itk = tmp_path / 'a2a-itk-source'
        itk.mkdir()
        codegen.prepare_rust(agent, itk_source=itk)
        assert (agent / 'a2a-itk').is_symlink()
        assert rec.calls == []


class TestPrepareJava:
    def test_symlink_only_no_subprocess(self, tmp_path, rec):
        agent = tmp_path / 'agent'
        agent.mkdir()
        itk = tmp_path / 'a2a-itk-source'
        itk.mkdir()
        codegen.prepare_java(agent, itk_source=itk)
        assert (agent / 'a2a-itk').is_symlink()
        assert rec.calls == []


# ---------------------------------------------------------------------------
# .NET no-op
# ---------------------------------------------------------------------------


class TestPrepareDotnet:
    def test_noop(self, tmp_path, rec):
        agent = tmp_path / 'agent'
        agent.mkdir()
        codegen.prepare_dotnet(agent)
        assert list(agent.iterdir()) == []
        assert rec.calls == []


# ---------------------------------------------------------------------------
# Default sources
# ---------------------------------------------------------------------------


class TestDefaultSources:
    def test_default_proto_source_points_at_repo_protos(self):
        p = codegen.default_proto_source()
        # Path shape: <repo>/protos/instruction.proto — don't require it to
        # exist (tests can run in trees without the real proto), just check
        # the shape.
        assert p.name == 'instruction.proto'
        assert p.parent.name == 'protos'

    def test_default_itk_source_points_at_repo_root(self):
        root = codegen.default_itk_source()
        assert (root / 'v2' / 'launcher' / 'codegen.py').exists(), (
            f'default_itk_source() should be the a2a-itk root; got {root}'
        )
