"""Fetch: retry classification, git argv, real ``file://`` remote round-trip.

We spin up a bare git repo on the local filesystem and use ``file://`` URLs to
avoid the network. That verifies the actual git plumbing while keeping tests
fast and hermetic.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from v2.launcher import fetch
from v2.launcher.errors import InfraFailure, PermanentError, Stage


# ---------------------------------------------------------------------------
# Local git fixture (real git, but no network)
# ---------------------------------------------------------------------------


def _init_bare_repo_with_commit(tmp_path: Path) -> tuple[Path, str]:
    """Create a bare repo containing exactly one commit; return (bare_path, sha)."""
    work = tmp_path / 'work'
    bare = tmp_path / 'bare.git'
    work.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=work, check=True)
    subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=work, check=True)
    subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=work, check=True)
    subprocess.run(['git', 'config', 'commit.gpgsign', 'false'], cwd=work, check=True)
    (work / 'itk').mkdir()
    (work / 'itk' / 'main.py').write_text('print(1)', encoding='utf-8')
    subprocess.run(['git', 'add', '.'], cwd=work, check=True)
    subprocess.run(['git', 'commit', '-q', '-m', 'init'], cwd=work, check=True)
    subprocess.run(
        ['git', 'clone', '-q', '--bare', str(work), str(bare)],
        check=True,
    )
    # Enable uploadpack.allowAnySHA1InWant so we can fetch by raw SHA.
    # `--file` is used because `git config` without it wants to auto-detect
    # a repo, which behaves oddly under bare paths in some git versions.
    subprocess.run(
        ['git', 'config', '--file', str(bare / 'config'),
         'uploadpack.allowAnySHA1InWant', 'true'],
        check=True,
    )
    sha = subprocess.run(
        ['git', 'rev-parse', 'HEAD'],
        cwd=work, check=True, capture_output=True, text=True,
    ).stdout.strip()
    return bare, sha


@pytest.fixture
def local_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """A local file:// repo posing as ``owner/name``."""
    bare, sha = _init_bare_repo_with_commit(tmp_path)
    monkeypatch.setattr(fetch, 'repo_url', lambda _repo: f'file://{bare}')
    return 'test/local', sha


# ---------------------------------------------------------------------------
# Real round-trip via file://
# ---------------------------------------------------------------------------


class TestFetchCommitReal:
    def test_fetches_specific_sha(self, tmp_path, local_repo, fast_backoff):  # noqa: ARG002
        repo, sha = local_repo
        dst = tmp_path / 'checkout'
        fetch.fetch_commit(repo, sha, dst, timeout=30)
        assert (dst / 'itk' / 'main.py').read_text(encoding='utf-8') == 'print(1)'

    def test_unknown_sha_is_permanent(self, tmp_path, local_repo, fast_backoff):  # noqa: ARG002
        repo, _ = local_repo
        bogus = '0' * 40
        with pytest.raises(PermanentError):
            fetch.fetch_commit(repo, bogus, tmp_path / 'checkout', timeout=30)


class TestResolveRefReal:
    def test_resolve_branch_to_sha(self, local_repo, fast_backoff):  # noqa: ARG002
        repo, sha = local_repo
        # git init used the default branch (main or master depending on git version).
        # ls-remote HEAD is the reliable way to resolve.
        got = fetch.resolve_ref(repo, 'HEAD')
        assert got == sha

    def test_unknown_ref_raises_permanent(self, local_repo, fast_backoff):  # noqa: ARG002
        repo, _ = local_repo
        with pytest.raises(PermanentError):
            fetch.resolve_ref(repo, 'refs/heads/no-such-branch')


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


class TestClassifyGitFailure:
    @pytest.mark.parametrize('stderr', [
        'fatal: repository not found',
        'error: 404 Not Found',
        "fatal: couldn't find remote ref foo",
        'fatal: bad object 0000',
        'fatal: reference is not a tree',
        'authentication failed for x',
        'remote: Permission denied',
    ])
    def test_permanent_markers(self, stderr):
        assert fetch._classify_git_failure(stderr) is PermanentError  # noqa: SLF001

    @pytest.mark.parametrize('stderr', [
        'fatal: unable to access: connection timed out',
        'ssh: connect to host github.com port 22: connection refused',
        'fatal: unable to update url base from redirection',
        '',
        # Regression: a bare "not found" substring in transient stderr (e.g.
        # a proxy outage reporting a missing pack) must NOT burn the retry
        # budget as if the SHA were permanently gone. This is what the
        # standalone 'not found' marker used to false-positive.
        "fatal: unable to access '.../.git': proxy target host not found",
        'fatal: index-pack file not found; falling back',
    ])
    def test_transient_markers(self, stderr):
        assert fetch._classify_git_failure(stderr) is InfraFailure  # noqa: SLF001


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


class TestRetryBehaviour:
    def test_transient_retries_then_gives_up(
        self, tmp_path, monkeypatch, fast_backoff,
    ):  # noqa: ARG002
        # ITK_RETRIES=2 comes from the cache_dir fixture default; we don't use
        # cache_dir here, so set it explicitly.
        monkeypatch.setenv('ITK_RETRIES', '3')
        n = {'n': 0}

        def flaky(args, cwd=None, timeout=None):  # noqa: ARG001
            n['n'] += 1
            return subprocess.CompletedProcess(
                args, 128, stdout='', stderr='fatal: connection reset',
            )

        monkeypatch.setattr(fetch, '_run_git', flaky)
        with pytest.raises(InfraFailure) as e:
            fetch.fetch_commit('x/y', 'a' * 40, tmp_path / 'dst', timeout=1)
        assert e.value.stage is Stage.FETCH
        # 3 attempts across the retry loop; init is inside the same loop iteration.
        # We only care that we tried more than once, not the exact count.
        assert n['n'] >= 2

    def test_permanent_does_not_retry(self, tmp_path, monkeypatch, fast_backoff):  # noqa: ARG002
        n = {'n': 0}

        def once(args, cwd=None, timeout=None):  # noqa: ARG001
            n['n'] += 1
            if 'init' in args:
                return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
            return subprocess.CompletedProcess(
                args, 128, stdout='', stderr='fatal: repository not found',
            )

        monkeypatch.setattr(fetch, '_run_git', once)
        with pytest.raises(PermanentError):
            fetch.fetch_commit('x/y', 'a' * 40, tmp_path / 'dst', timeout=1)
        # One init + one fetch = 2. No retries.
        assert n['n'] == 2

    def test_timeout_is_transient(self, tmp_path, monkeypatch, fast_backoff):  # noqa: ARG002
        monkeypatch.setenv('ITK_RETRIES', '2')
        calls = {'n': 0}

        def timeout_git(args, cwd=None, timeout=None):  # noqa: ARG001
            calls['n'] += 1
            raise subprocess.TimeoutExpired(cmd=args, timeout=timeout or 1)

        monkeypatch.setattr(fetch, '_run_git', timeout_git)
        with pytest.raises(InfraFailure):
            fetch.fetch_commit('x/y', 'a' * 40, tmp_path / 'dst', timeout=1)
        assert calls['n'] >= 2


class TestResolveRefRefPreference:
    """Regression: an unqualified ref like ``main`` must resolve to
    ``refs/heads/main``, not to ``refs/for/main`` (Gerrit review),
    ``refs/pull/N/head``, or any other namespace the server exposes.
    Discovered against a real GitHub mirror of a2a-python.
    """

    def test_prefers_heads_over_review_ref(self, monkeypatch, fast_backoff):  # noqa: ARG002
        heads_sha = 'a' * 40

        def fake(args, cwd=None, timeout=None):  # noqa: ARG001
            # With --heads --tags, servers only return refs/heads/* and
            # refs/tags/* — refs/for/* is filtered out at the server side.
            out = f'{heads_sha}\trefs/heads/main\n'
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr='')

        monkeypatch.setattr(fetch, '_run_git', fake)
        assert fetch.resolve_ref('x/y', 'main') == heads_sha

    def test_pick_sha_prefers_heads(self):
        out = (
            'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\trefs/for/main\n'
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\trefs/heads/main\n'
            'cccccccccccccccccccccccccccccccccccccccc\trefs/tags/main\n'
        )
        assert fetch._pick_sha(out) == 'a' * 40  # noqa: SLF001

    def test_pick_sha_prefers_tags_when_no_head(self):
        out = (
            'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\trefs/for/main\n'
            'cccccccccccccccccccccccccccccccccccccccc\trefs/tags/main\n'
        )
        assert fetch._pick_sha(out) == 'c' * 40  # noqa: SLF001

    def test_pick_sha_falls_back_to_head_ref(self):
        out = 'dddddddddddddddddddddddddddddddddddddddd\tHEAD\n'
        assert fetch._pick_sha(out) == 'd' * 40  # noqa: SLF001

    def test_pick_sha_empty(self):
        assert fetch._pick_sha('') is None
        assert fetch._pick_sha('malformed line\n') is None

    def test_head_falls_back_to_unfiltered(self, monkeypatch, fast_backoff):  # noqa: ARG002
        """``HEAD`` isn't matched by --heads --tags; the fallback query kicks in."""
        head_sha = 'e' * 40
        calls: list[list[str]] = []

        def fake(args, cwd=None, timeout=None):  # noqa: ARG001
            calls.append(list(args))
            if '--heads' in args:
                return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
            return subprocess.CompletedProcess(
                args, 0, stdout=f'{head_sha}\tHEAD\n', stderr='',
            )

        monkeypatch.setattr(fetch, '_run_git', fake)
        assert fetch.resolve_ref('x/y', 'HEAD') == head_sha
        assert len(calls) == 2
        assert '--heads' in calls[0]
        assert '--heads' not in calls[1]


class TestResolveRefRetry:
    def test_transient_ls_remote_retries(self, monkeypatch, fast_backoff):  # noqa: ARG002
        monkeypatch.setenv('ITK_RETRIES', '3')
        n = {'n': 0}

        def flaky(args, cwd=None, timeout=None):  # noqa: ARG001
            n['n'] += 1
            if n['n'] < 3:
                return subprocess.CompletedProcess(
                    args, 128, stdout='', stderr='fatal: unable to connect',
                )
            return subprocess.CompletedProcess(
                args, 0, stdout='deadbeef' * 5 + '\trefs/heads/main\n', stderr='',
            )

        monkeypatch.setattr(fetch, '_run_git', flaky)
        got = fetch.resolve_ref('x/y', 'main')
        assert got == 'deadbeef' * 5
        assert n['n'] == 3

    def test_ls_remote_empty_output_is_permanent(self, monkeypatch, fast_backoff):  # noqa: ARG002
        def empty(args, cwd=None, timeout=None):  # noqa: ARG001
            return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

        monkeypatch.setattr(fetch, '_run_git', empty)
        with pytest.raises(PermanentError, match='no matching ref'):
            fetch.resolve_ref('x/y', 'nope')


class TestInitFailurePreservesError:
    """Regression: retries exhausting on ``git init`` must surface the init
    stderr, not swallow it into ``cause=None``.
    """

    def test_init_failure_populates_last_exc(
        self, tmp_path, monkeypatch, fast_backoff,
    ):  # noqa: ARG002
        monkeypatch.setenv('ITK_RETRIES', '2')

        def always_init_fails(args, cwd=None, timeout=None):  # noqa: ARG001
            if 'init' in args:
                return subprocess.CompletedProcess(
                    args, 128, stdout='',
                    stderr='fatal: could not create work tree dir',
                )
            # We should never get here because init failure aborts each attempt.
            raise AssertionError(f'unexpected git call: {args}')

        monkeypatch.setattr(fetch, '_run_git', always_init_fails)
        with pytest.raises(InfraFailure) as e:
            fetch.fetch_commit('x/y', 'a' * 40, tmp_path / 'dst', timeout=1)
        # The cause chain must carry the underlying init stderr, not None.
        assert e.value.cause is not None
        assert 'work tree dir' in str(e.value.cause)
