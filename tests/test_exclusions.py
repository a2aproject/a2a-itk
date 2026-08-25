"""Known failures: excluding combinations that are broken, visibly.

The rule these tests protect is that an exclusion is never silent. A skipped
scenario that nobody is told about is indistinguishable from coverage that
quietly disappeared, which is the exact failure this consolidation exists to
remove.
"""

from __future__ import annotations

import pytest

from test_suite.launcher.matrix import Matrix
from test_suite.scenarios.exclusions import (
    Exclusion,
    ExclusionError,
    KnownFailures,
)
from test_suite.scenarios.loader import parse_tests
from test_suite.scenarios.resolver import resolve_all


MATRIX = Matrix.from_dict({'sdks': {
    'python': {'v10': {'repo': 'a/py', 'ref': 'main'}},
    'go': {'v10': {'repo': 'a/go', 'ref': 'main'}},
}})


def _scenario(**over):
    base = {
        'schema': 'traversal/v1', 'name': 'S',
        'roles': {'peers': [{'sdk': 'go', 'line': 'v10'}]},
        'transports': ['jsonrpc', 'grpc'],
        'behavior': 'send_message',
    }
    base.update(over)
    return parse_tests([base])


def _match(exclusion, **over):
    kwargs = {
        'sdks': ['current', 'go_v10'], 'protocols': ['grpc'],
        'behavior': 'send_message', 'streaming': False, 'sut_sdk': 'go',
    }
    kwargs.update(over)
    return exclusion.matches(**kwargs)


class TestMatching:
    def test_agent_and_transport(self):
        e = Exclusion(reason='r', agents=frozenset({'go_v10'}),
                      transports=frozenset({'grpc'}))
        assert _match(e) is True
        assert _match(e, protocols=['jsonrpc']) is False
        assert _match(e, sdks=['current', 'python_v10']) is False

    def test_unset_fields_mean_any(self):
        e = Exclusion(reason='r', agents=frozenset({'go_v10'}))
        assert _match(e, protocols=['jsonrpc']) is True
        assert _match(e, behavior='resubscribe') is True

    def test_all_set_fields_must_match(self):
        e = Exclusion(reason='r', agents=frozenset({'go_v10'}),
                      behaviors=frozenset({'push_notification'}))
        assert _match(e) is False
        assert _match(e, behavior='push_notification') is True

    def test_streaming_is_matched_exactly(self):
        e = Exclusion(reason='r', streaming=True)
        assert _match(e, streaming=False) is False
        assert _match(e, streaming=True) is True

    def test_streaming_false_is_not_treated_as_unset(self):
        """`streaming: false` must mean non-streaming only, not 'any'."""
        e = Exclusion(reason='r', streaming=False)
        assert _match(e, streaming=True) is False
        assert _match(e, streaming=False) is True

    def test_any_transport_overlap_excludes(self):
        """A bundled scenario can't be partially skipped, so one known-bad
        transport takes the whole thing."""
        e = Exclusion(reason='r', transports=frozenset({'http_json'}))
        assert _match(e, protocols=['jsonrpc', 'http_json']) is True


class TestParsing:
    def test_empty_file_is_valid(self):
        assert len(KnownFailures.from_dict({'exclusions': []})) == 0

    def test_missing_key_is_valid(self):
        assert len(KnownFailures.from_dict({})) == 0

    def test_reason_is_required(self):
        """An exclusion nobody can explain is lost coverage wearing a hat."""
        with pytest.raises(ExclusionError, match='needs a `reason`'):
            KnownFailures.from_dict({'exclusions': [{'agents': ['go_v10']}]})

    def test_unknown_key_is_rejected(self):
        with pytest.raises(ExclusionError, match='unknown key'):
            KnownFailures.from_dict({'exclusions': [
                {'reason': 'r', 'transport': 'grpc'},  # singular typo
            ]})

    def test_missing_file_means_no_exclusions(self, tmp_path):
        assert len(KnownFailures.from_path(tmp_path / 'nope.yaml')) == 0

    def test_shipped_file_parses(self):
        KnownFailures.from_default()


class TestAppliedDuringResolution:
    def test_matching_scenario_is_excluded(self):
        known = KnownFailures.from_dict({'exclusions': [
            {'agents': ['go_v10'], 'transports': ['grpc'],
             'reason': 'go v1 grpc hop hangs'},
        ]})
        report = resolve_all(_scenario(), MATRIX, known_failures=known)
        assert [s.protocols[0] for s in report.scenarios] == ['jsonrpc']

    def test_exclusion_is_reported_not_silent(self):
        known = KnownFailures.from_dict({'exclusions': [
            {'agents': ['go_v10'], 'transports': ['grpc'],
             'reason': 'go v1 grpc hop hangs'},
        ]})
        report = resolve_all(_scenario(), MATRIX, known_failures=known)
        assert len(report.skipped) == 1
        name, why = report.skipped[0]
        assert 'grpc' in name
        assert 'known failure' in why
        assert 'go v1 grpc hop hangs' in why

    def test_issue_link_is_surfaced(self):
        known = KnownFailures.from_dict({'exclusions': [
            {'agents': ['go_v10'], 'reason': 'r', 'issue': 'http://b/123'},
        ]})
        report = resolve_all(_scenario(), MATRIX, known_failures=known)
        assert 'http://b/123' in report.skipped[0][1]

    def test_no_exclusions_changes_nothing(self):
        report = resolve_all(_scenario(), MATRIX, known_failures=KnownFailures())
        assert len(report.scenarios) == 2
        assert report.skipped == []

    def test_legacy_scenarios_are_not_excluded(self):
        """Legacy files are the baseline being migrated from; reinterpreting
        them would make the coverage comparison meaningless."""
        known = KnownFailures.from_dict({'exclusions': [
            {'agents': ['go_v10'], 'reason': 'everything go is broken'},
        ]})
        legacy = parse_tests({'tests': [{
            'name': 'old', 'sdks': ['current', 'go_v10'],
            'behavior': 'send_message', 'protocols': ['grpc'],
        }]})
        report = resolve_all(legacy, MATRIX, known_failures=known)
        assert len(report.scenarios) == 1

    def test_transport_granularity_needs_the_split(self):
        """Excluding one transport only works because scenarios carry one.
        Bundled, the rule would take the good transports down too."""
        known = KnownFailures.from_dict({'exclusions': [
            {'agents': ['go_v10'], 'transports': ['grpc'], 'reason': 'r'},
        ]})
        split = resolve_all(_scenario(), MATRIX, known_failures=known)
        bundled = resolve_all(
            _scenario(transports=None, transport_sets=[['jsonrpc', 'grpc']]),
            MATRIX, known_failures=known,
        )
        assert len(split.scenarios) == 1      # jsonrpc survives
        assert len(bundled.scenarios) == 0    # whole bundle goes


class TestPairwiseMatching:
    """Rules can name a (SUT, peer) pair, not just a line."""

    def test_sut_sdk_narrows_a_rule_to_one_sut(self):
        e = Exclusion(reason='r', agents=frozenset({'python_v03'}),
                      sut_sdk=frozenset({'java'}))
        assert _match(e, sdks=['current', 'python_v03'], sut_sdk='java') is True
        assert _match(e, sdks=['current', 'python_v03'], sut_sdk='go') is False

    def test_unless_sut_sdk_carves_one_out(self):
        e = Exclusion(reason='r', agents=frozenset({'ts_v03'}),
                      transports=frozenset({'grpc'}),
                      unless_sut_sdk=frozenset({'ts'}))
        assert _match(e, sdks=['current', 'ts_v03'], sut_sdk='java') is True
        assert _match(e, sdks=['current', 'ts_v03'], sut_sdk='ts') is False

    def test_an_unknown_sut_still_matches_a_plain_rule(self):
        """A local run passes no sut_sdk; rules that don't mention one apply."""
        e = Exclusion(reason='r', agents=frozenset({'go_v10'}))
        assert _match(e, sut_sdk=None) is True

    def test_sut_scoped_rule_does_not_fire_without_a_sut(self):
        e = Exclusion(reason='r', agents=frozenset({'go_v10'}),
                      sut_sdk=frozenset({'java'}))
        assert _match(e, sut_sdk=None) is False

    def test_both_sut_fields_at_once_is_rejected(self):
        with pytest.raises(ExclusionError, match='use one or the other'):
            KnownFailures.from_dict({'exclusions': [
                {'reason': 'r', 'sut_sdk': ['java'], 'unless_sut_sdk': ['ts']},
            ]})


class TestTrimmingVsSkipping:
    """A star loses the bad peer; a pair loses the whole scenario."""

    def _star(self, **over):
        base = {
            'schema': 'traversal/v1', 'name': 'Star',
            'roles': {'peers': [
                {'sdk': 'go', 'line': 'v10'}, {'sdk': 'python', 'line': 'v10'},
            ]},
            'transports': ['jsonrpc'], 'behavior': 'send_message',
        }
        base.update(over)
        return parse_tests([base])

    KNOWN = KnownFailures.from_dict({'exclusions': [
        {'agents': ['go_v10'], 'reason': 'go v1 is broken here'},
    ]})

    def test_star_keeps_running_without_the_bad_peer(self):
        """What the hand-written "No Go v03 - HTTP_JSON" scenarios did by
        omission. Killing the whole star would throw away python_v10 too."""
        report = resolve_all(self._star(), MATRIX, known_failures=self.KNOWN)
        [s] = report.scenarios
        assert s.sdks == ['current', 'python_v10']
        assert report.skipped == []

    def test_the_trim_is_reported(self):
        """One entry per removed agent, so a caller can group by cause instead
        of repeating the rationale once per scenario."""
        report = resolve_all(self._star(), MATRIX, known_failures=self.KNOWN)
        assert len(report.trimmed) == 1
        name, agent, why = report.trimmed[0]
        assert name == 'Star'
        assert agent == 'go_v10'
        assert 'go v1 is broken here' in why

    def test_edges_are_rebuilt_for_the_smaller_graph(self):
        """Edge indices are positional, so a trimmed star must be re-derived
        or it would wire up the wrong agents."""
        [s] = resolve_all(self._star(), MATRIX,
                          known_failures=self.KNOWN).scenarios
        assert s.edges == ['0->1', '1->0']

    def test_dropping_below_two_agents_skips_the_scenario(self):
        report = resolve_all(
            self._star(roles={'peers': [{'sdk': 'go', 'line': 'v10'}]}),
            MATRIX, known_failures=self.KNOWN,
        )
        assert report.scenarios == []
        assert len(report.skipped) == 1

    def test_explicit_edges_skip_rather_than_trim(self):
        """A hand-written edge list is indexed against the full agent list;
        removing one would silently rewire the graph."""
        report = resolve_all(
            self._star(edges=['0->1', '0->2', '1->0', '2->0']),
            MATRIX, known_failures=self.KNOWN,
        )
        assert report.scenarios == []
        assert 'known failure' in report.skipped[0][1]
