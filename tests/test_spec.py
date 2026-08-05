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
        ('sdk', 'python'), ('line', 'v10'),
        ('repo', 'x/y'), ('sha', _VALID_SHA),
    ])
    def test_rejects_extra_fields(self, field, value):
        with pytest.raises(ValueError, match='must not set'):
            TargetSpec(kind=Kind.MOUNT, **{field: value})


class TestLocal:
    def test_ok(self):
        s = TargetSpec(kind=Kind.LOCAL, sdk='python', line='v10')
        assert s.sdk == 'python'
        assert s.line == 'v10'

    def test_missing_sdk(self):
        with pytest.raises(ValueError, match="requires 'sdk'"):
            TargetSpec(kind=Kind.LOCAL, line='v10')

    def test_missing_line(self):
        with pytest.raises(ValueError, match="requires 'line'"):
            TargetSpec(kind=Kind.LOCAL, sdk='python')

    def test_rejects_repo(self):
        with pytest.raises(ValueError, match='must not set'):
            TargetSpec(kind=Kind.LOCAL, sdk='python', line='v10', repo='x/y')

    def test_rejects_sha(self):
        with pytest.raises(ValueError, match='must not set'):
            TargetSpec(kind=Kind.LOCAL, sdk='python', line='v10', sha=_VALID_SHA)

    @pytest.mark.parametrize('bad', ['V10', 'v1.0', '1.0', 'main', 'v10a'])
    def test_bad_line(self, bad):
        with pytest.raises(ValueError, match='invalid line'):
            TargetSpec(kind=Kind.LOCAL, sdk='python', line=bad)

    @pytest.mark.parametrize('bad', ['Python', 'py 3', 'py/thon', ''])
    def test_bad_sdk(self, bad):
        with pytest.raises(ValueError, match='invalid sdk|requires'):
            TargetSpec(kind=Kind.LOCAL, sdk=bad, line='v10')

    @pytest.mark.parametrize('ok', ['v10', 'v03', 'v0', 'v123'])
    def test_line_tokens(self, ok):
        TargetSpec(kind=Kind.LOCAL, sdk='python', line=ok)


class TestCheckout:
    def test_ok(self):
        s = TargetSpec(
            kind=Kind.CHECKOUT,
            sdk='python',
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
                sdk='python',
                repo='a2aproject/a2a-python',
                sha=bad,
            )

    def test_missing_sha(self):
        with pytest.raises(ValueError, match="requires 'sha'"):
            TargetSpec(
                kind=Kind.CHECKOUT,
                sdk='python',
                repo='a2aproject/a2a-python',
            )

    def test_missing_repo(self):
        with pytest.raises(ValueError, match="requires 'repo'"):
            TargetSpec(
                kind=Kind.CHECKOUT,
                sdk='python',
                sha=_VALID_SHA,
            )

    def test_missing_sdk(self):
        with pytest.raises(ValueError, match="requires 'sdk'"):
            TargetSpec(
                kind=Kind.CHECKOUT,
                repo='a2aproject/a2a-python',
                sha=_VALID_SHA,
            )

    @pytest.mark.parametrize('bad', [
        'a2aproject', 'a2aproject/', '/a2a-python',
        'a2a project/a2a-python', 'a/b/c',
    ])
    def test_bad_repo(self, bad):
        with pytest.raises(ValueError, match='invalid repo'):
            TargetSpec(kind=Kind.CHECKOUT, sdk='python', repo=bad, sha=_VALID_SHA)

    def test_rejects_line(self):
        # line is orthogonal to CHECKOUT today (SHA fully identifies the version).
        with pytest.raises(ValueError, match='must not set'):
            TargetSpec(
                kind=Kind.CHECKOUT,
                sdk='python', line='v10',
                repo='a2aproject/a2a-python', sha=_VALID_SHA,
            )


class TestCacheSlug:
    def test_checkout_slug(self):
        s = TargetSpec(
            kind=Kind.CHECKOUT,
            sdk='python',
            repo='a2aproject/a2a-python',
            sha=_VALID_SHA,
        )
        assert s.cache_slug() == f'a2aproject_a2a-python@{_VALID_SHA}'

    def test_mount_has_no_slug(self):
        s = TargetSpec(kind=Kind.MOUNT)
        with pytest.raises(ValueError):
            s.cache_slug()

    def test_local_has_no_slug(self):
        s = TargetSpec(kind=Kind.LOCAL, sdk='python', line='v10')
        with pytest.raises(ValueError):
            s.cache_slug()


class TestImmutable:
    def test_frozen(self):
        s = TargetSpec(kind=Kind.MOUNT)
        with pytest.raises(Exception):
            s.kind = Kind.LOCAL  # type: ignore[misc]
