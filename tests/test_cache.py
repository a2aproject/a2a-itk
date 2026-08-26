"""Cache core: hit/miss, sentinel semantics, cleanup, key composition.

Concurrency-specific behaviour lives in test_cache_concurrency.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_suite.launcher import cache, config
from test_suite.launcher.errors import InfraFailure, PermanentError, Stage


_SHA_A = 'a' * 40
_SHA_B = 'b' * 40
_REPO = 'a2aproject/a2a-python'


def _fake_fetch_ok(repo: str, sha: str, dst: Path) -> None:  # noqa: ARG001
    """Materialise a minimal ``itk/`` subdir at ``dst`` so builders see something."""
    (dst / 'itk').mkdir(parents=True, exist_ok=True)
    (dst / 'itk' / 'main.py').write_text('# fake', encoding='utf-8')


def _fake_build_ok(repo: str, sha: str, agent_dir: Path) -> str:  # noqa: ARG001
    (agent_dir / '.built-marker').write_text('done', encoding='utf-8')
    return 'python'


class TestKeys:
    def test_key_includes_image_digest(self, cache_dir, monkeypatch):  # noqa: ARG002
        monkeypatch.setenv('ITK_IMAGE_DIGEST', 'X')
        k1 = cache.cache_key(_REPO, _SHA_A)
        monkeypatch.setenv('ITK_IMAGE_DIGEST', 'Y')
        k2 = cache.cache_key(_REPO, _SHA_A)
        assert k1 != k2, 'image digest bump must bust the key'

    def test_slug_shape(self, cache_dir):  # noqa: ARG002
        k = cache.cache_key(_REPO, _SHA_A)
        assert k == (
            'a2aproject_a2a-python@'
            + _SHA_A
            + '@test-digest@'
            + config.proto_digest()
        )

    def test_key_includes_proto_digest(self, cache_dir, tmp_path, monkeypatch):  # noqa: ARG002
        proto = tmp_path / 'protos' / 'instruction.proto'
        proto.parent.mkdir()
        proto.write_text('syntax = "proto3"; message A {}\n', encoding='utf-8')
        monkeypatch.setattr(config, '_repo_root', lambda: tmp_path)
        k1 = cache.cache_key(_REPO, _SHA_A)
        proto.write_text(
            'syntax = "proto3"; message A { string extra = 1; }\n',
            encoding='utf-8',
        )
        k2 = cache.cache_key(_REPO, _SHA_A)
        assert k1 != k2, 'proto content change must bust the key'


class TestCheckoutAndBuild:
    def test_first_call_fetches_and_builds(self, cache_dir):
        dir_ = cache.checkout_and_build(
            _REPO, _SHA_A,
            _fetcher=_fake_fetch_ok, _builder=_fake_build_ok,
        )
        assert dir_.exists()
        assert (dir_ / 'main.py').exists()
        assert (dir_ / '.built-marker').exists()
        key = cache.cache_key(_REPO, _SHA_A)
        assert (cache_dir / 'trees' / key / '.itk-built').exists()
        cache.release(_REPO, _SHA_A)

    def test_second_call_hits_cache(self, cache_dir):
        calls = {'fetch': 0, 'build': 0}

        def fetch(repo, sha, dst):
            calls['fetch'] += 1
            _fake_fetch_ok(repo, sha, dst)

        def build(repo, sha, d):
            calls['build'] += 1
            return _fake_build_ok(repo, sha, d)

        cache.checkout_and_build(_REPO, _SHA_A, _fetcher=fetch, _builder=build)
        cache.release(_REPO, _SHA_A)
        cache.checkout_and_build(_REPO, _SHA_A, _fetcher=fetch, _builder=build)
        cache.release(_REPO, _SHA_A)

        assert calls == {'fetch': 1, 'build': 1}, 'second call must not refetch/rebuild'

    def test_different_sha_misses(self, cache_dir):  # noqa: ARG002
        n = {'n': 0}

        def fetch(repo, sha, dst):
            n['n'] += 1
            _fake_fetch_ok(repo, sha, dst)

        cache.checkout_and_build(_REPO, _SHA_A, _fetcher=fetch, _builder=_fake_build_ok)
        cache.release(_REPO, _SHA_A)
        cache.checkout_and_build(_REPO, _SHA_B, _fetcher=fetch, _builder=_fake_build_ok)
        cache.release(_REPO, _SHA_B)
        assert n['n'] == 2

    def test_image_digest_bust(self, cache_dir, monkeypatch):  # noqa: ARG002
        monkeypatch.setenv('ITK_IMAGE_DIGEST', 'X')
        n = {'n': 0}
        def fetch(repo, sha, dst):
            n['n'] += 1
            _fake_fetch_ok(repo, sha, dst)
        cache.checkout_and_build(_REPO, _SHA_A, _fetcher=fetch, _builder=_fake_build_ok)
        cache.release(_REPO, _SHA_A)
        monkeypatch.setenv('ITK_IMAGE_DIGEST', 'Y')
        cache.checkout_and_build(_REPO, _SHA_A, _fetcher=fetch, _builder=_fake_build_ok)
        cache.release(_REPO, _SHA_A)
        assert n['n'] == 2, 'digest change must refetch'

    def test_proto_digest_bust(self, cache_dir, tmp_path, monkeypatch):  # noqa: ARG002
        proto = tmp_path / 'protos' / 'instruction.proto'
        proto.parent.mkdir()
        proto.write_text(
            'syntax = "proto3"; message Instruction {}\n',
            encoding='utf-8',
        )
        monkeypatch.setattr(config, '_repo_root', lambda: tmp_path)
        n = {'n': 0}

        def fetch(repo, sha, dst):
            n['n'] += 1
            _fake_fetch_ok(repo, sha, dst)

        cache.checkout_and_build(_REPO, _SHA_A, _fetcher=fetch, _builder=_fake_build_ok)
        cache.release(_REPO, _SHA_A)
        proto.write_text(
            'syntax = "proto3"; message Instruction { string extra = 1; }\n',
            encoding='utf-8',
        )
        cache.checkout_and_build(_REPO, _SHA_A, _fetcher=fetch, _builder=_fake_build_ok)
        cache.release(_REPO, _SHA_A)
        assert n['n'] == 2, 'proto change must refetch'


class TestFailureCleanup:
    def test_fetch_failure_removes_partial_tree(self, cache_dir):
        def bad_fetch(repo, sha, dst):
            _fake_fetch_ok(repo, sha, dst)
            raise InfraFailure(repo, sha, Stage.FETCH, message='network')

        with pytest.raises(InfraFailure):
            cache.checkout_and_build(
                _REPO, _SHA_A,
                _fetcher=bad_fetch, _builder=_fake_build_ok,
            )
        key = cache.cache_key(_REPO, _SHA_A)
        assert not (cache_dir / 'trees' / key).exists(), 'partial tree must be removed'

    def test_build_failure_removes_partial_tree(self, cache_dir):
        def bad_build(repo, sha, agent_dir):  # noqa: ARG001
            raise InfraFailure(repo, sha, Stage.BUILD, message='compile')

        with pytest.raises(InfraFailure) as e:
            cache.checkout_and_build(
                _REPO, _SHA_A,
                _fetcher=_fake_fetch_ok, _builder=bad_build,
            )
        assert e.value.stage is Stage.BUILD
        key = cache.cache_key(_REPO, _SHA_A)
        assert not (cache_dir / 'trees' / key).exists()

    def test_permanent_fetch_error_propagates(self, cache_dir):  # noqa: ARG002
        def bad_fetch(repo, sha, dst):  # noqa: ARG001
            raise PermanentError(repo, sha, Stage.FETCH, 'no such object')
        with pytest.raises(PermanentError):
            cache.checkout_and_build(
                _REPO, _SHA_A,
                _fetcher=bad_fetch, _builder=_fake_build_ok,
            )

    def test_arbitrary_fetch_exception_wrapped(self, cache_dir):  # noqa: ARG002
        def bad_fetch(repo, sha, dst):  # noqa: ARG001
            raise RuntimeError('surprise')
        with pytest.raises(InfraFailure) as e:
            cache.checkout_and_build(
                _REPO, _SHA_A,
                _fetcher=bad_fetch, _builder=_fake_build_ok,
            )
        assert e.value.stage is Stage.FETCH

    def test_arbitrary_build_exception_wrapped(self, cache_dir):  # noqa: ARG002
        def bad_build(repo, sha, agent_dir):  # noqa: ARG001
            raise RuntimeError('boom')
        with pytest.raises(InfraFailure) as e:
            cache.checkout_and_build(
                _REPO, _SHA_A,
                _fetcher=_fake_fetch_ok, _builder=bad_build,
            )
        assert e.value.stage is Stage.BUILD

    def test_missing_subdir_after_fetch(self, cache_dir):  # noqa: ARG002
        def no_subdir(repo, sha, dst):  # noqa: ARG001
            # Successful "fetch" that forgets to create itk/.
            dst.mkdir(parents=True, exist_ok=True)
            (dst / 'README.md').write_text('nope', encoding='utf-8')
        with pytest.raises(InfraFailure) as e:
            cache.checkout_and_build(
                _REPO, _SHA_A,
                _fetcher=no_subdir, _builder=_fake_build_ok,
            )
        assert 'missing after fetch' in str(e.value)

    def test_partial_tree_from_prior_run_is_cleaned(self, cache_dir):
        # Pre-populate the tree WITHOUT sentinel to simulate a killed prior run.
        key = cache.cache_key(_REPO, _SHA_A)
        tree = cache_dir / 'trees' / key
        (tree / 'itk').mkdir(parents=True)
        (tree / 'itk' / 'stale.txt').write_text('old', encoding='utf-8')
        assert not (tree / '.itk-built').exists()

        cache.checkout_and_build(
            _REPO, _SHA_A,
            _fetcher=_fake_fetch_ok, _builder=_fake_build_ok,
        )
        # Stale file must be gone; fresh fetch replaced it.
        assert not (tree / 'itk' / 'stale.txt').exists()
        assert (tree / '.itk-built').exists()
        cache.release(_REPO, _SHA_A)


class TestReleaseIdempotent:
    def test_release_without_pin_is_ok(self, cache_dir):  # noqa: ARG002
        # Never called checkout_and_build; release should be a no-op, not a crash.
        cache.release(_REPO, _SHA_A)

    def test_double_release(self, cache_dir):  # noqa: ARG002
        cache.checkout_and_build(
            _REPO, _SHA_A,
            _fetcher=_fake_fetch_ok, _builder=_fake_build_ok,
        )
        cache.release(_REPO, _SHA_A)
        cache.release(_REPO, _SHA_A)


class TestEviction:
    def test_evict_over_budget(self, cache_dir, monkeypatch):
        # Squeeze the budget so anything triggers eviction.
        monkeypatch.setenv('ITK_DISK_BUDGET_BYTES', '1')
        # A gigantic TTL so age isn't what causes eviction — the budget is.
        monkeypatch.setenv('ITK_TREE_TTL', '999999999')
        cache.checkout_and_build(
            _REPO, _SHA_A,
            _fetcher=_fake_fetch_ok, _builder=_fake_build_ok,
        )
        cache.release(_REPO, _SHA_A)
        evicted = cache.evict()
        key = cache.cache_key(_REPO, _SHA_A)
        assert key in evicted
        assert not (cache_dir / 'trees' / key).exists()

    def test_evict_skips_pinned(self, cache_dir, monkeypatch):
        monkeypatch.setenv('ITK_DISK_BUDGET_BYTES', '1')
        cache.checkout_and_build(
            _REPO, _SHA_A,
            _fetcher=_fake_fetch_ok, _builder=_fake_build_ok,
        )
        # Do NOT release — evict should refuse to touch this key.
        evicted = cache.evict()
        key = cache.cache_key(_REPO, _SHA_A)
        assert key not in evicted
        assert (cache_dir / 'trees' / key).exists()
        cache.release(_REPO, _SHA_A)

    def test_evict_expired_by_ttl(self, cache_dir, monkeypatch):
        # Budget is huge, but TTL is negative -> everything is expired.
        monkeypatch.setenv('ITK_DISK_BUDGET_BYTES', '999999999999')
        monkeypatch.setenv('ITK_TREE_TTL', '-1')
        cache.checkout_and_build(
            _REPO, _SHA_A,
            _fetcher=_fake_fetch_ok, _builder=_fake_build_ok,
        )
        cache.release(_REPO, _SHA_A)
        evicted = cache.evict()
        key = cache.cache_key(_REPO, _SHA_A)
        assert key in evicted

    def test_evict_within_budget_and_ttl_is_noop(self, cache_dir):  # noqa: ARG002
        cache.checkout_and_build(
            _REPO, _SHA_A,
            _fetcher=_fake_fetch_ok, _builder=_fake_build_ok,
        )
        cache.release(_REPO, _SHA_A)
        assert cache.evict() == []
