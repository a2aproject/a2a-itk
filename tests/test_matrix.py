"""Matrix: parser, resolver, TargetSpec conversion.

All tests are pure (no network) — matrix deliberately does not resolve
refs to SHAs; that's the caller's job via ``fetch.resolve_ref``.
"""

from __future__ import annotations

import pytest

from test_suite.launcher.matrix import (
    ALL_TRANSPORTS,
    Matrix,
    MatrixEntry,
    MatrixError,
)
from test_suite.launcher.spec import Kind


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_MINIMAL = {
    'sdks': {
        'python': {'v10': {'repo': 'a2aproject/a2a-python', 'ref': 'main'}},
        'go':     {'v10': {'repo': 'a2aproject/a2a-go',     'ref': 'main'}},
        'ts':     {'v10': {'repo': 'a2aproject/a2a-js',     'ref': 'main'}},
    },
}


@pytest.fixture
def matrix() -> Matrix:
    return Matrix.from_dict(_MINIMAL)


# ---------------------------------------------------------------------------
# from_dict validation
# ---------------------------------------------------------------------------


class TestFromDict:
    def test_minimal(self):
        m = Matrix.from_dict(_MINIMAL)
        assert len(m) == 3
        assert ('python', 'v10') in m.keys()

    @pytest.mark.parametrize('data', [None, 42, 'string', ['list']])
    def test_non_mapping_top_level(self, data):
        with pytest.raises(MatrixError, match='must be a mapping'):
            Matrix.from_dict(data)

    def test_missing_sdks_key(self):
        with pytest.raises(MatrixError, match='missing top-level `sdks:`'):
            Matrix.from_dict({})

    def test_sdks_not_mapping(self):
        with pytest.raises(MatrixError, match='`sdks:` must be a mapping'):
            Matrix.from_dict({'sdks': ['python', 'go']})

    def test_line_not_mapping(self):
        with pytest.raises(MatrixError, match='sdks.python: must be a mapping'):
            Matrix.from_dict({'sdks': {'python': 'not-a-dict'}})

    def test_cfg_not_mapping(self):
        with pytest.raises(MatrixError, match='must be a mapping with repo'):
            Matrix.from_dict({'sdks': {'python': {'v10': 'not-a-dict'}}})

    @pytest.mark.parametrize('cfg', [
        {},
        {'repo': 'a/b'},           # missing ref
        {'ref': 'main'},           # missing repo
        {'repo': '', 'ref': 'main'},  # empty repo
        {'repo': 'a/b', 'ref': ''},   # empty ref
    ])
    def test_missing_or_empty_repo_ref(self, cfg):
        with pytest.raises(MatrixError, match='needs both `repo` and `ref`'):
            Matrix.from_dict({'sdks': {'python': {'v10': cfg}}})

    @pytest.mark.parametrize('cfg', [
        {'repo': 42, 'ref': 'main'},
        {'repo': 'a/b', 'ref': 42},
    ])
    def test_non_string_repo_ref(self, cfg):
        with pytest.raises(MatrixError, match='must be strings'):
            Matrix.from_dict({'sdks': {'python': {'v10': cfg}}})


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------


class TestResolve:
    def test_plain(self, matrix):
        entry = matrix.resolve('python_v10')
        assert entry == MatrixEntry(
            sdk='python', line='v10',
            repo='a2aproject/a2a-python', ref='main',
        )

    def test_second_instance_maps_to_same_entry(self, matrix):
        # `python_v10_2` is a second instance of `python_v10`; must resolve
        # to the same source. Distinct ports are the Cluster's job.
        assert matrix.resolve('python_v10') == matrix.resolve('python_v10_2')
        assert matrix.resolve('go_v10') == matrix.resolve('go_v10_3')

    def test_current_rejected(self, matrix):
        with pytest.raises(MatrixError, match="'current' is the SUT"):
            matrix.resolve('current')

    @pytest.mark.parametrize('bad', [
        '', 'python', 'v10', 'PYTHON_V10',   # missing pieces / wrong case
        'python_10', 'python_v', 'python_vX',  # bad line
        'python-v10', 'python v10',            # bad separator
        '2_v10',                                # sdk starts with digit
    ])
    def test_malformed(self, matrix, bad):
        with pytest.raises(MatrixError, match='invalid agent id'):
            matrix.resolve(bad)

    def test_unknown_sdk(self, matrix):
        with pytest.raises(MatrixError, match='unknown agent'):
            matrix.resolve('rust_v10')  # not in _MINIMAL

    def test_unknown_line(self, matrix):
        with pytest.raises(MatrixError, match='unknown agent'):
            matrix.resolve('python_v99')

    def test_unknown_error_lists_known(self, matrix):
        # Aid CI diagnostics: an unknown agent surfaces the whole known set
        # so the operator immediately sees what IS in matrix.yaml.
        with pytest.raises(MatrixError) as e:
            matrix.resolve('rust_v10')
        msg = str(e.value)
        assert 'python_v10' in msg
        assert 'go_v10' in msg
        assert 'ts_v10' in msg


# ---------------------------------------------------------------------------
# make_spec() — matrix → TargetSpec
# ---------------------------------------------------------------------------


class TestMakeSpec:
    _SHA = 'a' * 40

    def test_checkout(self, matrix):
        spec = matrix.make_spec('python_v10', self._SHA)
        assert spec.kind is Kind.CHECKOUT
        assert spec.repo == 'a2aproject/a2a-python'
        assert spec.sha == self._SHA

    def test_current_returns_mount(self, matrix):
        # Special-case: 'current' shortcuts to MOUNT, sha argument ignored.
        spec = matrix.make_spec('current', 'anything')
        assert spec.kind is Kind.MOUNT
        assert spec.repo is None
        assert spec.sha is None

    def test_unknown_agent_raises(self, matrix):
        with pytest.raises(MatrixError):
            matrix.make_spec('rust_v10', self._SHA)


# ---------------------------------------------------------------------------
# __contains__, __len__, keys()
# ---------------------------------------------------------------------------


class TestConvenience:
    def test_contains(self, matrix):
        assert 'python_v10' in matrix
        assert 'python_v10_2' in matrix   # second instance
        assert 'rust_v10' not in matrix
        assert 'current' not in matrix     # rejected by resolve
        assert 'malformed' not in matrix

    def test_len(self, matrix):
        assert len(matrix) == 3

    def test_keys_sorted(self, matrix):
        assert matrix.keys() == [('go', 'v10'), ('python', 'v10'), ('ts', 'v10')]


# ---------------------------------------------------------------------------
# from_path / from_default (round-trip through YAML)
# ---------------------------------------------------------------------------


class TestFromPath:
    def test_missing_file(self, tmp_path):
        with pytest.raises(MatrixError, match='matrix file not found'):
            Matrix.from_path(tmp_path / 'does-not-exist.yaml')

    def test_round_trip(self, tmp_path):
        p = tmp_path / 'matrix.yaml'
        p.write_text(
            'sdks:\n'
            '  python:\n'
            '    v10: {repo: a2aproject/a2a-python, ref: main}\n'
            '  rust:\n'
            '    v10: {repo: a2aproject/a2a-rs, ref: main}\n',
            encoding='utf-8',
        )
        m = Matrix.from_path(p)
        assert len(m) == 2
        assert m.resolve('python_v10').repo == 'a2aproject/a2a-python'
        assert m.resolve('rust_v10').repo == 'a2aproject/a2a-rs'

    def test_empty_file_rejected(self, tmp_path):
        p = tmp_path / 'matrix.yaml'
        p.write_text('', encoding='utf-8')
        with pytest.raises(MatrixError, match='missing top-level'):
            Matrix.from_path(p)

    def test_yaml_supports_comments(self, tmp_path):
        # Regression: humans edit matrix.yaml; comments must be tolerated.
        p = tmp_path / 'matrix.yaml'
        p.write_text(
            '# top-level comment\n'
            'sdks:\n'
            '  python:  # inline comment\n'
            '    v10: {repo: a2aproject/a2a-python, ref: main}\n',
            encoding='utf-8',
        )
        m = Matrix.from_path(p)
        assert m.resolve('python_v10').ref == 'main'


class TestFromDefault:
    def test_default_matrix_loads(self):
        """The repo-root matrix.yaml must parse and contain the live entries.

        Currently: v10 across all 5 SDKs, plus v03 overlays for python/go/ts.
        java and rust have no v03 baseline. If new SDKs or lines land, this
        list needs updating — that's the point.
        """
        m = Matrix.from_default()
        expected = [
            ('go', 'v03'), ('go', 'v10'),
            ('java', 'v10'),
            ('python', 'v03'), ('python', 'v10'),
            ('rust', 'v10'),
            ('ts', 'v03'), ('ts', 'v10'),
        ]
        assert m.keys() == expected

    def test_default_matrix_v03_pins_overlay_tags(self):
        """v03 entries must point at `+itk` overlay tags, not `main`.

        Regression: a copy-paste that sets v03 to `main` would resolve to
        the SDK's v1 tip (no v0.3 code at all) and every v03 peer would
        fail to build with a version-mismatched SDK dep.
        """
        m = Matrix.from_default()
        for sdk in ('python', 'go', 'ts'):
            entry = m.resolve(f'{sdk}_v03')
            assert '+itk' in entry.ref, (
                f'{sdk}_v03 must pin an overlay tag (contains "+itk"), '
                f'got {entry.ref!r}'
            )


class TestTransports:
    """Per-line transport capability, used to expand the `peers: all` macro."""

    def test_defaults_to_all_three_when_omitted(self):
        m = Matrix.from_dict({
            'sdks': {'python': {'v10': {'repo': 'a/b', 'ref': 'main'}}}
        })
        assert m.resolve('python_v10').transports == ALL_TRANSPORTS

    def test_explicit_subset_is_read(self):
        m = Matrix.from_dict({'sdks': {'go': {'v03': {
            'repo': 'a/b', 'ref': 'v0.3.15+itk',
            'transports': ['jsonrpc', 'grpc'],
        }}}})
        assert m.resolve('go_v03').transports == frozenset({'jsonrpc', 'grpc'})

    def test_supports_checks_every_requested_transport(self):
        entry = MatrixEntry(
            sdk='go', line='v03', repo='a/b', ref='x',
            transports=frozenset({'jsonrpc', 'grpc'}),
        )
        assert entry.supports(['jsonrpc']) is True
        assert entry.supports(['jsonrpc', 'grpc']) is True
        assert entry.supports(['jsonrpc', 'http_json']) is False

    def test_unknown_transport_is_rejected(self):
        """A typo would silently shrink which peers `peers: all` selects."""
        with pytest.raises(MatrixError, match='unknown transport'):
            Matrix.from_dict({'sdks': {'go': {'v10': {
                'repo': 'a/b', 'ref': 'main', 'transports': ['jsonrpc', 'grcp'],
            }}}})

    def test_empty_transport_list_is_rejected(self):
        with pytest.raises(MatrixError, match='must not be empty'):
            Matrix.from_dict({'sdks': {'go': {'v10': {
                'repo': 'a/b', 'ref': 'main', 'transports': [],
            }}}})

    def test_non_list_is_rejected(self):
        with pytest.raises(MatrixError, match='must be a list of strings'):
            Matrix.from_dict({'sdks': {'go': {'v10': {
                'repo': 'a/b', 'ref': 'main', 'transports': 'jsonrpc',
            }}}})

    def test_only_go_v03_is_restricted_here(self):
        """`transports` is only for a line that cannot speak one from
        anywhere. go_v03 is the sole such case; ts_v03's grpc/http_json limit
        is pairwise and lives in known_failures.yaml.
        """
        m = Matrix.from_default()
        restricted = {
            e.agent_id: sorted(e.transports)
            for e in m.entries() if e.transports != ALL_TRANSPORTS
        }
        assert restricted == {'go_v03': ['grpc', 'jsonrpc']}


class TestEntries:
    def test_is_sorted_for_stable_peer_ordering(self):
        """`peers: all` expands in this order, and edge indices are positional
        — an unstable order would make runs incomparable."""
        m = Matrix.from_default()
        ids = [e.agent_id for e in m.entries()]
        assert ids == sorted(ids)

    def test_covers_every_key(self):
        m = Matrix.from_default()
        assert len(m.entries()) == len(m.keys()) == len(m)
