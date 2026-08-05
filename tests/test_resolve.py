"""Kind dispatch + LaunchSession pin lifecycle.

We stub cache.checkout_and_build so no real fetch happens, and we assert the
session releases pins even when the body raises.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_suite.launcher import cache, resolve
from test_suite.launcher.spec import Kind, TargetSpec


_SHA = 'a' * 40


class TestResolve:
    def test_mount_returns_agents_repo_itk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(resolve, '_repo_root', lambda: tmp_path)
        (tmp_path / 'agents' / 'repo' / 'itk').mkdir(parents=True)
        spec = TargetSpec(kind=Kind.MOUNT)
        got = resolve.resolve(spec)
        assert got == tmp_path / 'agents' / 'repo' / 'itk'

    def test_mount_missing_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(resolve, '_repo_root', lambda: tmp_path)
        with pytest.raises(RuntimeError, match='not been mounted'):
            resolve.resolve(TargetSpec(kind=Kind.MOUNT))

    def test_local_composes_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(resolve, '_repo_root', lambda: tmp_path)
        spec = TargetSpec(kind=Kind.LOCAL, sdk='python', line='v10')
        got = resolve.resolve(spec)
        assert got == tmp_path / 'agents' / 'python' / 'v10'
        # LOCAL does NOT check existence — it is a degenerate ref that may be
        # deleted between plan and resolve.

    def test_checkout_delegates_to_cache(self, monkeypatch):
        called = {}

        def fake(repo, sha, **_kw):
            called['args'] = (repo, sha)
            return Path('/fake/agent')

        monkeypatch.setattr(cache, 'checkout_and_build', fake)
        spec = TargetSpec(
            kind=Kind.CHECKOUT,
            sdk='python',
            repo='a2aproject/a2a-python',
            sha=_SHA,
        )
        got = resolve.resolve(spec)
        assert got == Path('/fake/agent')
        assert called['args'] == ('a2aproject/a2a-python', _SHA)


class TestLaunchSession:
    def test_pin_released_on_exit(self, monkeypatch):
        released: list[tuple[str, str]] = []
        monkeypatch.setattr(cache, 'checkout_and_build',
                            lambda repo, sha, **_kw: Path(f'/fake/{sha}'))
        monkeypatch.setattr(cache, 'release',
                            lambda repo, sha: released.append((repo, sha)))
        spec = TargetSpec(
            kind=Kind.CHECKOUT, sdk='python',
            repo='a2aproject/a2a-python', sha=_SHA,
        )
        with resolve.LaunchSession() as sess:
            sess.resolve(spec)
        assert released == [('a2aproject/a2a-python', _SHA)]

    def test_pin_released_on_exception(self, monkeypatch):
        released: list[tuple[str, str]] = []
        monkeypatch.setattr(cache, 'checkout_and_build',
                            lambda repo, sha, **_kw: Path(f'/fake/{sha}'))
        monkeypatch.setattr(cache, 'release',
                            lambda repo, sha: released.append((repo, sha)))
        spec = TargetSpec(
            kind=Kind.CHECKOUT, sdk='python',
            repo='a2aproject/a2a-python', sha=_SHA,
        )
        with pytest.raises(RuntimeError, match='deliberate'):
            with resolve.LaunchSession() as sess:
                sess.resolve(spec)
                raise RuntimeError('deliberate')
        assert released == [('a2aproject/a2a-python', _SHA)]

    def test_mount_and_local_do_not_pin(self, tmp_path, monkeypatch):
        released: list[tuple[str, str]] = []
        monkeypatch.setattr(cache, 'release',
                            lambda repo, sha: released.append((repo, sha)))
        monkeypatch.setattr(resolve, '_repo_root', lambda: tmp_path)
        (tmp_path / 'agents' / 'repo' / 'itk').mkdir(parents=True)

        with resolve.LaunchSession() as sess:
            sess.resolve(TargetSpec(kind=Kind.MOUNT))
            sess.resolve(TargetSpec(kind=Kind.LOCAL, sdk='python', line='v10'))
        assert released == [], 'MOUNT/LOCAL must not create cache pins'

    def test_multiple_checkouts_all_released(self, monkeypatch):
        released: list[tuple[str, str]] = []
        monkeypatch.setattr(cache, 'checkout_and_build',
                            lambda repo, sha, **_kw: Path(f'/fake/{sha}'))
        monkeypatch.setattr(cache, 'release',
                            lambda repo, sha: released.append((repo, sha)))
        specs = [
            TargetSpec(kind=Kind.CHECKOUT, sdk='python',
                       repo='a2aproject/a2a-python', sha='a' * 40),
            TargetSpec(kind=Kind.CHECKOUT, sdk='go',
                       repo='a2aproject/a2a-go', sha='b' * 40),
        ]
        with resolve.LaunchSession() as sess:
            for s in specs:
                sess.resolve(s)
        assert sorted(released) == sorted([
            ('a2aproject/a2a-python', 'a' * 40),
            ('a2aproject/a2a-go', 'b' * 40),
        ])


class TestLaunchSessionLogHandles:
    """Regression: LaunchSession.__exit__ must close every ._log_file attribute
    on spawned Popens. Prior to the review fix the handles leaked.
    """

    def _make_fake_proc_with_log(self, log_file) -> object:
        class FakeProc:
            def __init__(self):
                self._log_file = log_file
        return FakeProc()

    def test_closes_all_log_handles_on_exit(self, tmp_path, monkeypatch):
        from test_suite import current as spawn_mod

        # Two "spawns" — each gets its own StringIO-like open file.
        opened: list = []

        def fake_spawn_from_dir(agent_dir, http, grpc, *, log_dir=None, log_name=None):  # noqa: ARG001
            f = open(tmp_path / f'log_{len(opened)}.log', 'a', encoding='utf-8')  # noqa: SIM115
            opened.append(f)
            class FakeProc:
                pass
            p = FakeProc()
            p._log_file = f  # noqa: SLF001
            return p

        monkeypatch.setattr(spawn_mod, 'spawn_from_dir', fake_spawn_from_dir)
        monkeypatch.setattr(resolve, '_repo_root', lambda: tmp_path)
        (tmp_path / 'agents' / 'repo' / 'itk').mkdir(parents=True)

        with resolve.LaunchSession() as sess:
            sess.spawn(TargetSpec(kind=Kind.MOUNT), 8001, 8002,
                       log_dir=tmp_path)
            sess.spawn(TargetSpec(kind=Kind.MOUNT), 8003, 8004,
                       log_dir=tmp_path)

        assert len(opened) == 2
        assert all(f.closed for f in opened), 'LaunchSession must close every log handle'

    def test_closes_log_handles_even_on_exception(self, tmp_path, monkeypatch):
        from test_suite import current as spawn_mod
        opened: list = []

        def fake_spawn_from_dir(agent_dir, http, grpc, *, log_dir=None, log_name=None):  # noqa: ARG001
            f = open(tmp_path / f'log_{len(opened)}.log', 'a', encoding='utf-8')  # noqa: SIM115
            opened.append(f)
            class FakeProc:
                pass
            p = FakeProc()
            p._log_file = f  # noqa: SLF001
            return p

        monkeypatch.setattr(spawn_mod, 'spawn_from_dir', fake_spawn_from_dir)
        monkeypatch.setattr(resolve, '_repo_root', lambda: tmp_path)
        (tmp_path / 'agents' / 'repo' / 'itk').mkdir(parents=True)

        with pytest.raises(RuntimeError, match='deliberate'):
            with resolve.LaunchSession() as sess:
                sess.spawn(TargetSpec(kind=Kind.MOUNT), 8001, 8002,
                           log_dir=tmp_path)
                raise RuntimeError('deliberate')

        assert opened and opened[0].closed

    def test_no_log_dir_leaves_no_log_file_attr(self, tmp_path, monkeypatch):
        # When log_dir is None, spawn should not attach ._log_file. Exit path
        # must not choke on its absence.
        from test_suite import current as spawn_mod
        def fake(agent_dir, http, grpc, *, log_dir=None, log_name=None):  # noqa: ARG001
            class FakeProc:  # no _log_file attribute
                pass
            return FakeProc()

        monkeypatch.setattr(spawn_mod, 'spawn_from_dir', fake)
        monkeypatch.setattr(resolve, '_repo_root', lambda: tmp_path)
        (tmp_path / 'agents' / 'repo' / 'itk').mkdir(parents=True)

        with resolve.LaunchSession() as sess:
            sess.spawn(TargetSpec(kind=Kind.MOUNT), 8001, 8002)
        # Reaching here without an AttributeError is the assertion.
