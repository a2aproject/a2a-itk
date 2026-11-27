"""Coverage comparison between the legacy per-repo sets and the shared set.

Two things are tested here: that the comparison itself is sound, and — in
:class:`TestShippingCorpus` — that the shared set actually covers every hop
the five repos exercise today. The second is the acceptance gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

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
        7 unrestricted lines x 3 transports, plus go_v03 x 2 and ts_v03 x 1
        = 24
        (peer, transport) pairs. Times four behaviour/streaming combinations —
        send_message non-streaming and streaming, push notification,
        resubscribe — gives 96.
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

    @pytest.mark.parametrize(('path', 'expected_peers'), [
        (SHARED_PR, {'current', 'dotnet_v10', 'go_v10', 'python_v10'}),
        (SHARED_NIGHTLY, {
            'current', 'dotnet_v10', 'go_v10', 'java_v10',
            'python_v10', 'rust_v10', 'ts_v10',
        }),
    ])
    def test_dotnet_push_keeps_only_v1_peers(self, path, expected_peers):
        scenarios = resolve(
            load_file(path), Matrix.from_default(), sut_sdk='dotnet',
        )
        push = [s for s in scenarios if s.behavior == 'push_notification']

        assert {agent for s in push for agent in s.sdks} == expected_peers
        assert {s.protocols[0] for s in push} == {
            'jsonrpc', 'grpc', 'http_json',
        }

    @pytest.mark.parametrize('path', [SHARED_PR, SHARED_NIGHTLY])
    def test_dotnet_retains_v03_non_push_coverage(self, path):
        scenarios = resolve(
            load_file(path), Matrix.from_default(), sut_sdk='dotnet',
        )
        legacy_variants = {
            (s.behavior, s.streaming)
            for s in scenarios
            if 'python_v03' in s.sdks and s.protocols == ['jsonrpc']
        }

        assert legacy_variants == {
            ('send_message', False),
            ('send_message', True),
            ('resubscribe', True),
        }

    @pytest.mark.parametrize('path', [SHARED_PR, SHARED_NIGHTLY])
    def test_python_retains_v03_push_coverage(self, path):
        scenarios = resolve(
            load_file(path), Matrix.from_default(), sut_sdk='python',
        )
        push_peers = {
            agent
            for s in scenarios
            if s.behavior == 'push_notification' and s.protocols == ['jsonrpc']
            for agent in s.sdks
        }

        assert {'python_v03', 'go_v03'} <= push_peers


class TestSharedSetsAt11:
    """The A2A 1.1 entries of the shared sets.

    For an SDK that speaks only 1.0 they resolve to nothing, so its runs do
    not change. For one that speaks 1.1 they add the relay chains now, and
    the scenarios between agents that speak 1.1 once the matrix has a line
    that does.
    """

    SPEAKS_1_1 = ('1.1', '1.0', '0.3')

    @staticmethod
    def _with_python_v11():
        data = yaml.safe_load((ROOT / 'matrix.yaml').read_text(encoding='utf-8'))
        data['sdks']['python']['v11'] = {
            'repo': 'a2aproject/a2a-python', 'ref': 'c' * 40,
            'protocol_versions': ['1.1', '1.0', '0.3'],
        }
        return Matrix.from_dict(data)

    @staticmethod
    def _without_the_1_1_entries(path, tmp_path):
        data = yaml.safe_load(path.read_text(encoding='utf-8'))
        data['tests'] = [t for t in data['tests'] if 'min_protocol_version' not in t]
        old = tmp_path / path.name
        old.write_text(yaml.safe_dump(data), encoding='utf-8')
        return old

    @pytest.mark.parametrize('path', [SHARED_PR, SHARED_NIGHTLY])
    @pytest.mark.parametrize('sdk', ['python', 'go', 'ts', 'java', 'rust', 'dotnet'])
    @pytest.mark.parametrize('sut_versions', [None, ('1.0',), ('0.3',)])
    def test_an_sdk_that_does_not_speak_1_1_runs_what_it_ran_before(
        self, path, sdk, sut_versions, tmp_path,
    ):
        before = resolve(
            load_file(self._without_the_1_1_entries(path, tmp_path)), Matrix.from_default(),
            sut_sdk=sdk, sut_protocol_versions=sut_versions,
        )
        now = resolve(
            load_file(path), Matrix.from_default(),
            sut_sdk=sdk, sut_protocol_versions=sut_versions,
        )
        assert now == before
        assert now

    def test_a_v11_line_joins_an_older_sdks_nightly_but_not_its_pr_stars(self):
        """`peers: all` picks it up, as for any new line; the PR stars name
        their peers, and own lines are held back until the SUT speaks 1.1."""
        matrix = self._with_python_v11()
        nightly = resolve(load_file(SHARED_NIGHTLY), matrix, sut_sdk='go')
        pr = resolve(load_file(SHARED_PR), matrix, sut_sdk='python')
        assert any('python_v11' in s.sdks for s in nightly)
        assert not any('python_v11' in s.sdks for s in pr)

    def test_a_1_1_sdk_also_runs_the_relay_chains(self):
        scenarios = resolve(
            load_file(SHARED_NIGHTLY), Matrix.from_default(),
            sut_sdk='go', sut_protocol_versions=self.SPEAKS_1_1,
        )
        relays = {
            (s.name, tuple(s.sdks), s.streaming) for s in scenarios if 'relay' in s.name
        }
        python_chain = ('python_v03', 'current', 'go_v10')
        ts_chain = ('ts_v03', 'current', 'python_v10')
        assert relays == {
            *(
                (f'Nightly - relay python_v03 -> current -> go_v10 - {t} - {mode}',
                 python_chain, mode == 'streaming')
                for t in ('jsonrpc', 'grpc', 'http_json')
                for mode in ('non-streaming', 'streaming')
            ),
            # ts_v03 serves grpc and http_json only to a TypeScript client.
            *(
                (f'Nightly - relay ts_v03 -> current -> python_v10 - jsonrpc - {mode}',
                 ts_chain, mode == 'streaming')
                for mode in ('non-streaming', 'streaming')
            ),
        }

    def test_with_a_v11_line_a_1_1_sdk_runs_the_1_1_pairs(self):
        scenarios = resolve(
            load_file(SHARED_NIGHTLY), self._with_python_v11(),
            sut_sdk='go', sut_protocol_versions=self.SPEAKS_1_1,
        )
        pairs = {
            s.name for s in scenarios
            if 'from generation' in s.name or 'at 1.0' in s.name
        }
        assert pairs == {
            f'Nightly - current vs python_v11 - {what} - {t}'
            for what in ('resubscribe from generation', 'send message at 1.0')
            for t in ('jsonrpc', 'grpc', 'http_json')
        }
        forced = {s.hop_protocol_version for s in scenarios if 'at 1.0' in s.name}
        assert forced == {'1.0'}

    @pytest.mark.parametrize(('sut_versions', 'has_v11'), [
        (None, False),
        (SPEAKS_1_1, True),
    ])
    def test_the_sdks_own_v11_line_joins_its_pr_stars_only_once_it_speaks_1_1(
        self, sut_versions, has_v11,
    ):
        scenarios = resolve(
            load_file(SHARED_PR), self._with_python_v11(),
            sut_sdk='python', sut_protocol_versions=sut_versions,
        )
        assert scenarios
        assert all(('python_v11' in s.sdks) is has_v11 for s in scenarios)
