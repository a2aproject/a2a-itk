"""Log-file naming: hash-suffixed default prevents collision; log_name overrides.

Concurrent peers commonly root at ``itk/`` (every SDK repo puts the agent there),
so ``agent_<basename>.log`` would collapse them all into one file. The default
naming appends an 8-hex hash of the full ``agent_dir`` path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from v2.launcher import spawn


class _NoOpPopen:
    """Stub Popen so tests never launch a real process."""

    def __init__(self, args, cwd=None, stdout=None, stderr=None, text=None):  # noqa: ARG002
        self.pid = 1
        self.args = args
        self.stdout_target = stdout

    def wait(self, *_a, **_k):
        return 0


@pytest.fixture
def stub_popen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, 'Popen', _NoOpPopen)


def _mount(tmp_path: Path, sub: str) -> Path:
    d = tmp_path / sub / 'itk'
    d.mkdir(parents=True)
    (d / 'main.py').write_text('# agent', encoding='utf-8')
    return d


class TestDefaultLogName:
    def test_two_itk_dirs_get_distinct_log_files(self, tmp_path, stub_popen):  # noqa: ARG002
        log_dir = tmp_path / 'logs'
        a = _mount(tmp_path, 'peer_a')
        b = _mount(tmp_path, 'peer_b')

        proc_a = spawn.spawn_from_dir(a, 8001, 8002, log_dir=log_dir)
        proc_b = spawn.spawn_from_dir(b, 8003, 8004, log_dir=log_dir)

        try:
            assert proc_a._log_file.name != proc_b._log_file.name, (  # noqa: SLF001
                'two agent dirs named itk/ must get distinct log files'
            )
            # Both files must be inside log_dir.
            assert Path(proc_a._log_file.name).parent == log_dir  # noqa: SLF001
            assert Path(proc_b._log_file.name).parent == log_dir  # noqa: SLF001
            # Default naming shape: agent_<basename>_<hash8>.log
            for p in (proc_a, proc_b):
                stem = Path(p._log_file.name).stem  # noqa: SLF001
                assert stem.startswith('agent_itk_'), stem
                # tag should be 8 hex chars
                tag = stem.rsplit('_', 1)[-1]
                assert len(tag) == 8
                int(tag, 16)  # raises if non-hex
        finally:
            proc_a._log_file.close()  # noqa: SLF001
            proc_b._log_file.close()  # noqa: SLF001

    def test_same_dir_same_default_name(self, tmp_path, stub_popen):  # noqa: ARG002
        # Same agent_dir path -> same default name -> same log file (append).
        d = _mount(tmp_path, 'peer_only')
        p1 = spawn.spawn_from_dir(d, 8001, 8002, log_dir=tmp_path)
        p2 = spawn.spawn_from_dir(d, 8003, 8004, log_dir=tmp_path)
        try:
            assert p1._log_file.name == p2._log_file.name  # noqa: SLF001
        finally:
            p1._log_file.close()  # noqa: SLF001
            p2._log_file.close()  # noqa: SLF001


class TestExplicitLogName:
    def test_log_name_overrides_default(self, tmp_path, stub_popen):  # noqa: ARG002
        d = _mount(tmp_path, 'peer')
        p = spawn.spawn_from_dir(
            d, 8001, 8002, log_dir=tmp_path, log_name='agent_python_v10',
        )
        try:
            assert Path(p._log_file.name).name == 'agent_python_v10.log'  # noqa: SLF001
        finally:
            p._log_file.close()  # noqa: SLF001


class TestNoLogDir:
    def test_no_log_dir_no_handle(self, tmp_path, stub_popen):  # noqa: ARG002
        d = _mount(tmp_path, 'peer')
        p = spawn.spawn_from_dir(d, 8001, 8002)  # no log_dir
        # Attribute must be absent so LaunchSession.__exit__ takes the noop path.
        assert not hasattr(p, '_log_file')
