"""TargetSpec validation.

Locks down the fail-fast SHA policy — passing ``main`` (or any non-40-hex ref)
must raise on construction. That's the guard that stops intra-run drift of a
moving ref mixing versions across peers.
"""

from __future__ import annotations

import pytest

from test_suite.launcher.spec import Kind, TargetSpec


_VALID_SHA = 'abcdef0123456789abcdef0123456789abcdef01'


class TestMount:
    def test_bare(self):
        s = TargetSpec(kind=Kind.MOUNT)
        assert s.kind is Kind.MOUNT

    @pytest.mark.parametrize('field,value', [
        ('repo', 'x/y'), ('sha', _VALID_SHA),
    ])
    def test_rejects_extra_fields(self, field, value):
        with pytest.raises(ValueError, match='must not set'):
            TargetSpec(kind=Kind.MOUNT, **{field: value})


class TestCheckout:
    def test_ok(self):
        s = TargetSpec(
            kind=Kind.CHECKOUT,
            repo='a2aproject/a2a-python',
            sha=_VALID_SHA,
        )
        assert s.repo == 'a2aproject/a2a-python'
        assert s.sha == _VALID_SHA

    @pytest.mark.parametrize('bad', [
        'main',              # symbolic ref — the whole point of the rule
        'HEAD',
        'v1.0.0',            # tag
        'abc123',            # short SHA
        'ABCDEF' + '0' * 34, # uppercase — git canonicalises to lower
        '0' * 39,            # 39 chars
        '0' * 41,            # 41 chars
        'g' * 40,            # non-hex
    ])
    def test_rejects_non_sha(self, bad):
        with pytest.raises(ValueError, match='invalid sha'):
            TargetSpec(
                kind=Kind.CHECKOUT,
                repo='a2aproject/a2a-python',
                sha=bad,
            )

    def test_missing_sha(self):
        with pytest.raises(ValueError, match="requires 'sha'"):
            TargetSpec(
                kind=Kind.CHECKOUT,
                repo='a2aproject/a2a-python',
            )

    def test_missing_repo(self):
        with pytest.raises(ValueError, match="requires 'repo'"):
            TargetSpec(
                kind=Kind.CHECKOUT,
                sha=_VALID_SHA,
            )

    @pytest.mark.parametrize('bad', [
        'a2aproject', 'a2aproject/', '/a2a-python',
        'a2a project/a2a-python', 'a/b/c',
    ])
    def test_bad_repo(self, bad):
        with pytest.raises(ValueError, match='invalid repo'):
            TargetSpec(kind=Kind.CHECKOUT, repo=bad, sha=_VALID_SHA)


class TestCacheSlug:
    def test_checkout_slug(self):
        s = TargetSpec(
            kind=Kind.CHECKOUT,
            repo='a2aproject/a2a-python',
            sha=_VALID_SHA,
        )
        assert s.cache_slug() == f'a2aproject_a2a-python@{_VALID_SHA}'

    def test_mount_has_no_slug(self):
        s = TargetSpec(kind=Kind.MOUNT)
        with pytest.raises(ValueError):
            s.cache_slug()


class TestImmutable:
    def test_frozen(self):
        s = TargetSpec(kind=Kind.MOUNT)
        with pytest.raises(Exception):
            s.kind = Kind.CHECKOUT  # type: ignore[misc]


class TestNoLocalKind:
    """Regression: `Kind.LOCAL` was removed per reviewer feedback (PR #28).

    Baked baselines no longer exist — every agent, SUT included, is
    reached through MOUNT or CHECKOUT.
    """

    def test_local_kind_gone(self):
        with pytest.raises(AttributeError):
            _ = Kind.LOCAL  # type: ignore[attr-defined]
