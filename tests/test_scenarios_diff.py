"""Coverage comparison between the legacy per-repo sets and the shared set.

Two things are tested here: that the comparison itself is sound, and — in
:class:`TestShippingCorpus` — that the shared set actually covers every hop
the five repos exercise today. The second is the acceptance gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.scenarios_diff import atoms, explain_drop, main
from test_suite.launcher.matrix import Matrix
from test_suite.scenarios.exclusions import KnownFailures
from test_suite.scenarios.loader import load_file, parse_tests
from test_suite.scenarios.resolver import resolve


ROOT = Path(__file__).resolve().parents[1]
SHARED_PR = ROOT / 'scenarios' / 'traversal' / 'pr.yaml'
SHARED_NIGHTLY = ROOT / 'scenarios' / 'traversal' / 'nightly.yaml'

# The five SDK checkouts live beside a2a-itk in a dev tree, but not in CI.
SDK_ROOT = ROOT.parent
SDKS = [
    ('a2a-python', 'python'),
    ('a2a-go', 'go'),
    ('a2a-js', 'ts'),
    ('a2a-java', 'java'),
    ('a2a-rs', 'rust'),
]


def _legacy(name, sdks, behavior, protocols, edges=None, streaming=False):
    return parse_tests({'tests': [{
        'name': name, 'sdks': sdks, 'behavior': behavior,
        'protocols': protocols, 'edges': edges, 'streaming': streaming,
    }]})[0]


def _resolved(*args, **kwargs):
    return resolve([_legacy(*args, **kwargs)], Matrix.from_default())[0]


class TestAtoms:
    def test_one_hop_per_edge_per_transport(self):
        s = _resolved('x', ['current', 'go_v10'], 'send_message',
                      ['jsonrpc', 'grpc'], ['0->1', '1->0'])
        assert atoms(s) == {
            ('current', 'go_v10', 'jsonrpc', 'send_message', False),
            ('current', 'go_v10', 'grpc', 'send_message', False),
            ('go_v10', 'current', 'jsonrpc', 'send_message', False),
            ('go_v10', 'current', 'grpc', 'send_message', False),
        }

    def test_absent_edges_mean_a_complete_digraph(self):
        """What the traversal engine builds when handed no edges."""
        s = _resolved('x', ['current', 'go_v10', 'python_v10'],
                      'send_message', ['jsonrpc'], None)
        assert len(atoms(s)) == 6  # 3 nodes, all ordered pairs

    def test_streaming_is_part_of_the_identity(self):
        base = _resolved('x', ['current', 'go_v10'], 'send_message',
                         ['jsonrpc'], ['0->1', '1->0'])
        stream = _resolved('x', ['current', 'go_v10'], 'send_message',
                           ['jsonrpc'], ['0->1', '1->0'], streaming=True)
        assert not (atoms(base) & atoms(stream))

    def test_more_transports_is_a_strict_superset(self):
        """Why a shared scenario on all three transports covers a repo that
        only listed one."""
        one = _resolved('x', ['current', 'go_v10'], 'send_message',
                        ['jsonrpc'], ['0->1', '1->0'])
        three = _resolved('x', ['current', 'go_v10'], 'send_message',
                          ['jsonrpc', 'grpc', 'http_json'], ['0->1', '1->0'])
        assert atoms(one) < atoms(three)

    def test_a_bigger_star_covers_a_smaller_one(self):
        """A star's edges are all SUT-to-peer, so widening it only adds."""
        small = _resolved('x', ['current', 'go_v10'], 'send_message',
                          ['jsonrpc'], ['0->1', '1->0'])
        big = _resolved('x', ['current', 'go_v10', 'python_v10'],
                        'send_message', ['jsonrpc'],
                        ['0->1', '0->2', '1->0', '2->0'])
        assert atoms(small) < atoms(big)


class TestCli:
    def _write(self, tmp_path, name, tests):
        p = tmp_path / name
        p.write_text(json.dumps({'tests': tests}), encoding='utf-8')
        return p

    _PAIR = {
        'name': 'p', 'sdks': ['current', 'go_v10'], 'behavior': 'send_message',
        'protocols': ['jsonrpc'], 'edges': ['0->1', '1->0'],
    }

    def test_identical_sets_pass(self, tmp_path, capsys):
        a = self._write(tmp_path, 'a.json', [self._PAIR])
        b = self._write(tmp_path, 'b.json', [self._PAIR])
        assert main(['--old', str(a), '--new', str(b)]) == 0
        assert 'all legacy coverage retained' in capsys.readouterr().out

    def test_added_coverage_passes(self, tmp_path, capsys):
        a = self._write(tmp_path, 'a.json', [self._PAIR])
        b = self._write(tmp_path, 'b.json', [
            {**self._PAIR, 'protocols': ['jsonrpc', 'grpc']},
        ])
        assert main(['--old', str(a), '--new', str(b)]) == 0
        assert 'EXTRA' in capsys.readouterr().out

    def test_lost_coverage_fails(self, tmp_path, capsys):
        a = self._write(tmp_path, 'a.json', [
            {**self._PAIR, 'protocols': ['jsonrpc', 'grpc']},
        ])
        b = self._write(tmp_path, 'b.json', [self._PAIR])
        assert main(['--old', str(a), '--new', str(b)]) == 1
        out = capsys.readouterr().out
        assert 'MISSING' in out
        assert 'grpc' in out

    def test_dropping_a_peer_fails(self, tmp_path):
        a = self._write(tmp_path, 'a.json', [
            {**self._PAIR, 'sdks': ['current', 'go_v10', 'python_v10'],
             'edges': ['0->1', '0->2', '1->0', '2->0']},
        ])
        b = self._write(tmp_path, 'b.json', [self._PAIR])
        assert main(['--old', str(a), '--new', str(b)]) == 1


@pytest.mark.parametrize(('repo', 'sdk'), SDKS)
class TestShippingCorpus:
    """The shared set must cover every hop the five repos exercise today.

    Skipped when the SDK checkouts aren't beside a2a-itk, which is the case
    in CI. The comparison is reproducible from a dev tree via
    ``scripts/scenarios_diff.py``.
    """

    def _run(self, repo, legacy_name, shared, sdk):
        legacy = SDK_ROOT / repo / 'itk' / legacy_name
        if not legacy.is_file():
            pytest.skip(f'{repo} not checked out beside a2a-itk')

        matrix = Matrix.from_default()
        known = KnownFailures.from_default()
        old = resolve(load_file(legacy), matrix, known_failures=KnownFailures())
        new = resolve(load_file(shared), matrix, sut_sdk=sdk, known_failures=known)

        old_atoms = set().union(*(atoms(s) for s in old))
        new_atoms = set().union(*(atoms(s) for s in new))

        # Coverage may shrink only where something says why: a capability
        # limit in matrix.yaml, or a recorded defect in known_failures.yaml.
        unexplained = [
            a for a in old_atoms - new_atoms
            if explain_drop(a, matrix, known) is None
        ]
        assert not unexplained, (
            f'{repo}/{legacy_name}: {len(unexplained)} hop(s) would stop being '
            f'tested with no reason recorded, e.g. {sorted(unexplained)[:3]}'
        )

    def test_pr_coverage_retained(self, repo, sdk):
        self._run(repo, 'scenarios.json', SHARED_PR, sdk)

    def test_nightly_coverage_retained(self, repo, sdk):
        self._run(repo, 'scenarios_full.json', SHARED_NIGHTLY, sdk)


class TestSharedSetIsValid:
    """Cheap guards that run everywhere, checkouts or not."""

    @pytest.mark.parametrize('path', [SHARED_PR, SHARED_NIGHTLY])
    def test_resolves(self, path):
        assert resolve(load_file(path), Matrix.from_default())

    def test_nightly_expands_to_the_full_pairwise_product(self):
        """From three declarations.

        Transports split, and each peer only gets the ones it speaks:
        7 unrestricted lines x 3 transports, plus go_v03 x 2, plus ts_v03 x 1
        = 24 (peer, transport) pairs. Times four behaviour/streaming
        combinations — send_message non-streaming and streaming, push
        notification, resubscribe — gives 96.
        """
        out = resolve(load_file(SHARED_NIGHTLY), Matrix.from_default())
        assert len(out) == 24 * 4

    def test_every_nightly_scenario_is_single_transport(self):
        """One transport per scenario is what makes a failure name itself."""
        for s in resolve(load_file(SHARED_NIGHTLY), Matrix.from_default()):
            assert len(s.protocols) == 1, s.name

    def test_ts_v03_is_jsonrpc_only(self):
        """Regression: the first shared nightly failed on ts_v03 over grpc and
        http_json, which a2a-js's file had claimed worked."""
        for s in resolve(load_file(SHARED_NIGHTLY), Matrix.from_default()):
            if 'ts_v03' in s.sdks:
                assert s.protocols == ['jsonrpc'], s.name

    def test_go_v03_never_gets_http_json(self):
        """The one uncontested capability limit in the corpus."""
        for path in (SHARED_PR, SHARED_NIGHTLY):
            for s in resolve(load_file(path), Matrix.from_default()):
                if 'go_v03' in s.sdks:
                    assert 'http_json' not in (s.protocols or []), s.name
