"""Drift guard: legacy ``test_suite.current`` MUST spawn identically to v2.

The design's One-Version rule was relaxed here to let ``v2/`` ship dark
without touching the hot path of five blocking CI jobs. The cost is two
copies of the polyglot spawn body. This test is the compensation: it drives
both implementations over the same fixture directories, records the argv +
cwd of every spawn, and asserts they match. If someone edits ``current.py``
to fix (say) the Java ``-Pitk`` profile and forgets ``v2/launcher/spawn.py``
(or vice versa), this test goes red before it can matter.

Both modules are exercised with subprocess.Popen / subprocess.run patched;
no real toolchain is needed.

Deliberately excluded from parity: the .NET branch reads ``current_dir``
early to search for ``*.csproj`` — spawn_from_dir supports it the same way,
and the argv is asserted directly rather than derived from the shared body
walk.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _load_legacy_current() -> types.ModuleType:
    """Import ``test_suite.current`` in isolation from ``test_suite/__init__.py``.

    The package's ``__init__.py`` pulls in every other launcher and imports
    ``agents.python.v03.pyproto`` — heavyweight and unrelated. We only need
    the ``spawn_agent`` function.
    """
    # Push repo root on the path so importlib finds test_suite/current.py.
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'test_suite_current_isolated',
        repo_root / 'test_suite' / 'current.py',
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope='module')
def legacy():
    return _load_legacy_current()


@pytest.fixture
def v2_spawn():
    from v2.launcher import spawn as v2_spawn_mod
    return v2_spawn_mod


# ---------------------------------------------------------------------------
# Recorder for both Popen and run
# ---------------------------------------------------------------------------


class _Recorded:
    """Captures argv + cwd on every subprocess.Popen / subprocess.run."""

    def __init__(self) -> None:
        self.popen_calls: list[tuple[list[str], Path]] = []
        self.run_calls: list[tuple[list[str], Path]] = []

    def make_popen(self):
        rec = self
        class FakePopen:
            def __init__(self, args, cwd=None, **_kw: Any):
                rec.popen_calls.append((list(args), Path(cwd) if cwd else Path.cwd()))
                self.args = args
                self.pid = 12345
            def wait(self, *_a, **_k): return 0
            def terminate(self): pass
            def kill(self): pass
        return FakePopen

    def make_run(self):
        rec = self
        def fake_run(args, cwd=None, **_kw: Any):
            rec.run_calls.append((list(args), Path(cwd) if cwd else Path.cwd()))
            return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
        return fake_run


# ---------------------------------------------------------------------------
# Fixtures per language
# ---------------------------------------------------------------------------


@pytest.fixture
def mount_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy) -> Path:
    """Make the legacy module look at ``tmp_path/agents/repo/itk`` as ``current``."""
    root = tmp_path / 'root'
    monkeypatch.setattr(legacy, '_ROOT_DIR', root)
    (root / 'agents' / 'repo' / 'itk').mkdir(parents=True)
    return root


def _mount_dir(root: Path) -> Path:
    return root / 'agents' / 'repo' / 'itk'


# ---------------------------------------------------------------------------
# Parity tests
# ---------------------------------------------------------------------------


def _run_both(
    legacy_mod,
    v2_spawn_mod,
    monkeypatch: pytest.MonkeyPatch,
    agent_dir: Path,
    http: int = 8001,
    grpc: int = 8002,
) -> tuple[list, list, list, list]:
    """Run both implementations and return their (popen, run) call records."""
    # Legacy
    rec1 = _Recorded()
    monkeypatch.setattr(subprocess, 'Popen', rec1.make_popen())
    monkeypatch.setattr(subprocess, 'run', rec1.make_run())
    legacy_mod.spawn_agent(http, grpc)
    monkeypatch.undo()

    # v2 (fresh patches)
    rec2 = _Recorded()
    monkeypatch.setattr(subprocess, 'Popen', rec2.make_popen())
    monkeypatch.setattr(subprocess, 'run', rec2.make_run())
    v2_spawn_mod.spawn_from_dir(agent_dir, http, grpc)
    monkeypatch.undo()

    return rec1.popen_calls, rec1.run_calls, rec2.popen_calls, rec2.run_calls


class TestParity:
    def test_go(self, legacy, v2_spawn, mount_root, monkeypatch):
        d = _mount_dir(mount_root)
        (d / 'main.go').write_text('package main', encoding='utf-8')
        lp, lr, vp, vr = _run_both(legacy, v2_spawn, monkeypatch, d)
        assert lp[0][0] == vp[0][0], f'go argv differ:\n legacy={lp[0][0]}\n v2   ={vp[0][0]}'
        assert lp[0][1] == vp[0][1]
        assert lr == vr == []

    def test_python(self, legacy, v2_spawn, mount_root, monkeypatch):
        d = _mount_dir(mount_root)
        (d / 'main.py').write_text('# agent', encoding='utf-8')
        lp, lr, vp, vr = _run_both(legacy, v2_spawn, monkeypatch, d)
        assert lp[0][0] == vp[0][0]
        assert lp[0][1] == vp[0][1]

    def test_ts(self, legacy, v2_spawn, mount_root, monkeypatch):
        d = _mount_dir(mount_root)
        (d.parent / 'package.json').write_text('{}', encoding='utf-8')
        lp, lr, vp, vr = _run_both(legacy, v2_spawn, monkeypatch, d)
        assert lp[0][0] == vp[0][0]
        assert lp[0][1] == vp[0][1]

    def test_java(self, legacy, v2_spawn, mount_root, monkeypatch):
        d = _mount_dir(mount_root)
        (d / 'pom.xml').write_text('<project/>', encoding='utf-8')
        lp, lr, vp, vr = _run_both(legacy, v2_spawn, monkeypatch, d)
        # Popen args: `mvn exec:java ...`
        assert lp[0][0] == vp[0][0], (
            f'java Popen argv differ:\n legacy={lp[0][0]}\n v2   ={vp[0][0]}'
        )
        assert lp[0][1] == vp[0][1]
        # Sync `mvn -Pitk -pl itk -am install` via subprocess.run — matched too.
        assert lr[0][0] == vr[0][0], (
            f'java pre-build argv differ:\n legacy={lr[0][0]}\n v2   ={vr[0][0]}'
        )
        assert lr[0][1] == vr[0][1]

    def test_rust_prebuilt(self, legacy, v2_spawn, mount_root, monkeypatch):
        d = _mount_dir(mount_root)
        (d / 'Cargo.toml').write_text('[package]\nname="x"\n', encoding='utf-8')
        release = d / 'target' / 'release'
        release.mkdir(parents=True)
        # Not the canonical name; both implementations should discover it.
        binary = release / 'itk-current-agent'
        binary.write_text('bin', encoding='utf-8')
        binary.chmod(0o755)

        lp, lr, vp, vr = _run_both(legacy, v2_spawn, monkeypatch, d)
        assert lp[0][0] == vp[0][0]
        assert lp[0][1] == vp[0][1]
        assert lr == vr == []  # binary already present -> no cargo build

    def test_rust_lazy_build(self, legacy, v2_spawn, mount_root, monkeypatch):
        d = _mount_dir(mount_root)
        (d / 'Cargo.toml').write_text('[package]\nname="x"\n', encoding='utf-8')
        release = d / 'target' / 'release'
        release.mkdir(parents=True)
        # Fake `run` mimics cargo: records the call AND creates the binary as
        # a side effect. Both implementations then re-glob, find it, and spawn.
        binary = release / 'itk-anything-agent'

        def make_fake_run(rec):
            def fake(args, cwd=None, **_kw: Any):
                rec.run_calls.append((list(args), Path(cwd) if cwd else Path.cwd()))
                if 'cargo' in args and 'build' in args:
                    binary.write_text('x', encoding='utf-8')
                    binary.chmod(0o755)
                return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
            return fake

        # Legacy first.
        rec1 = _Recorded()
        monkeypatch.setattr(subprocess, 'Popen', rec1.make_popen())
        monkeypatch.setattr(subprocess, 'run', make_fake_run(rec1))
        legacy.spawn_agent(8001, 8002)
        monkeypatch.undo()

        # Remove the binary so v2 also takes the lazy-build path.
        binary.unlink()

        rec2 = _Recorded()
        monkeypatch.setattr(subprocess, 'Popen', rec2.make_popen())
        monkeypatch.setattr(subprocess, 'run', make_fake_run(rec2))
        v2_spawn.spawn_from_dir(d, 8001, 8002)
        monkeypatch.undo()

        # Both implementations issue cargo build with identical argv.
        assert rec1.run_calls[0][0] == rec2.run_calls[0][0]
        assert rec1.run_calls[0][1] == rec2.run_calls[0][1]
        # Both spawn the discovered binary with identical argv.
        assert rec1.popen_calls[0][0] == rec2.popen_calls[0][0]

    def test_missing_entrypoint_raises(self, legacy, v2_spawn, mount_root, monkeypatch):
        d = _mount_dir(mount_root)  # empty dir
        with pytest.raises(RuntimeError):
            legacy.spawn_agent(8001, 8002)
        with pytest.raises(RuntimeError):
            v2_spawn.spawn_from_dir(d, 8001, 8002)
