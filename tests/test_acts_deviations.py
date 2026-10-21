"""The deviation passes in `acts_runner`.

Some preconditions are mutually exclusive with the deployment the rest of the
corpus needs. Four tests assert that an agent *lacking* a capability answers
`UnsupportedOperationError`; five assert that an agent *requiring* a credential
rejects a request without one. Against one ordinary SUT both groups skip, and
those branches of the protocol go untested by anybody — which is what the extra
passes exist to fix.

What is pinned here is which skips each pass claims. Claiming one it cannot
clear costs a whole SUT startup for nothing; missing one puts the hole back;
and claiming *another pass's* skip would replace a verdict with one produced
under the wrong configuration. The two skip reasons are worded similarly enough
to be worth testing apart.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import fields
from pathlib import Path

import pytest

import acts_runner
from acts_runner import (
    AUTH_ENFORCED_ENV,
    DEVIATIONS,
    REDUCED_CAPABILITIES_ENV,
    ActsRunError,
    _capabilities_dropped,
    _capability_skips,
    _security_declared,
    _skips_matching,
    _sut_env,
)
from test_suite.acts import LoadedSuite, load_suite
from test_suite.acts.runner import UNSATISFIABLE, Outcome, TestResult
from test_suite.acts.schema import Level, TransportBinding


MANIFEST = (
    Path(__file__).resolve().parent.parent / 'scenarios' / 'acts' / 'suite.acts.yaml'
)

BY_ENV = {d.env_var: d for d in DEVIATIONS}
REDUCED = BY_ENV[REDUCED_CAPABILITIES_ENV]
AUTH = BY_ENV[AUTH_ENFORCED_ENV]


def skipped(test_id: str, reason: str) -> TestResult:
    return TestResult(
        id=test_id,
        name=test_id,
        level=Level.MUST,
        result=Outcome.SKIP,
        duration_ms=0,
        skip_reason=reason,
    )


def claimed(deviation, results: list[TestResult]) -> dict[str, str]:
    return _skips_matching(results, deviation.skip_pattern)


class TestWhichSkipsTheReducedPassClaims:
    def test_a_capability_the_card_advertises_is_claimed(self):
        results = [skipped('CORE-CAP-002', 'agent card capability streaming=True, needs False')]
        assert _capability_skips(results) == {'CORE-CAP-002': 'streaming'}

    def test_the_four_corpus_tests_are_all_recognised(self):
        results = [
            skipped('CORE-CAP-001', 'agent card capability pushNotifications=True, needs False'),
            skipped('CORE-CAP-002', 'agent card capability streaming=True, needs False'),
            skipped('SEC-EXTCARD-003', 'agent card capability extendedAgentCard=True, needs False'),
            skipped('PUSH-CFG-004', 'agent card capability pushNotifications=True, needs False'),
        ]
        assert set(_capability_skips(results)) == {
            'CORE-CAP-001', 'CORE-CAP-002', 'SEC-EXTCARD-003', 'PUSH-CFG-004',
        }

    def test_a_capability_the_test_wants_present_is_not_claimed(self):
        """`needs True` is the SUT genuinely lacking something. Diminishing it
        further cannot help."""
        results = [skipped('X-001', 'agent card capability pushNotifications=False, needs True')]
        assert _capability_skips(results) == {}

    def test_an_unsatisfiable_precondition_is_not_claimed(self):
        """No agent can advertise a capability A2A does not define, so no
        second pass clears it — it is a corpus defect, and re-running would
        buy a startup and change nothing."""
        results = [skipped(
            'X-001',
            f"{UNSATISFIABLE}'telepathy' is not a capability A2A defines",
        )]
        assert _capability_skips(results) == {}

    def test_a_transport_skip_is_not_claimed(self):
        results = [skipped('GRPC-STATUS-001', 'targets grpc; this runner speaks jsonrpc')]
        assert _capability_skips(results) == {}

    def test_a_passing_test_is_not_claimed(self):
        passed = TestResult(
            id='CORE-SEND-001', name='x', level=Level.MUST,
            result=Outcome.PASS, duration_ms=1,
        )
        assert _capability_skips([passed]) == {}


class TestWhichSkipsTheAuthPassClaims:
    def test_a_card_declaring_no_security_is_claimed(self):
        results = [skipped('SEC-AUTH-001', 'agent card authentication=False, needs True')]
        assert set(claimed(AUTH, results)) == {'SEC-AUTH-001'}

    def test_the_five_corpus_tests_are_all_recognised(self):
        reason = 'agent card authentication=False, needs True'
        results = [
            skipped(t, reason) for t in (
                'SEC-AUTH-001', 'SEC-AUTH-002', 'SEC-AUTH-003',
                'SEC-AUTH-004', 'SEC-AUTH-006',
            )
        ]
        assert set(claimed(AUTH, results)) == {
            'SEC-AUTH-001', 'SEC-AUTH-002', 'SEC-AUTH-003',
            'SEC-AUTH-004', 'SEC-AUTH-006',
        }

    def test_a_test_wanting_an_open_agent_is_not_claimed(self):
        """`needs False` wants an agent requiring nothing, which is the default
        SUT — enforcing auth is the wrong direction."""
        results = [skipped('X-001', 'agent card authentication=True, needs False')]
        assert claimed(AUTH, results) == {}

    def test_a_transport_skip_is_not_claimed(self):
        """A `Runner` handed the unnarrowed corpus still emits these, and no
        SUT start can clear one."""
        results = [skipped('SEC-AUTH-001', 'targets jsonrpc; this runner speaks grpc')]
        assert claimed(AUTH, results) == {}


class TestThePassesNeverClaimEachOthersSkips:
    """The two reasons differ by one word; a sloppy regex would merge them."""

    def test_the_reduced_pass_ignores_an_authentication_skip(self):
        results = [skipped('SEC-AUTH-001', 'agent card authentication=False, needs True')]
        assert claimed(REDUCED, results) == {}

    def test_the_auth_pass_ignores_a_capability_skip(self):
        results = [skipped('CORE-CAP-002', 'agent card capability streaming=True, needs False')]
        assert claimed(AUTH, results) == {}

    def test_every_deviation_has_its_own_log_name(self):
        """Two passes writing one log make a failure unattributable."""
        names = [d.log_name for d in DEVIATIONS]
        assert len(set(names)) == len(names)
        assert 'acts_sut' not in names, 'that is the first pass'


class TestVerifyingTheDeviationTookEffect:
    def test_a_reduced_card_that_dropped_the_capability_passes(self):
        assert _capabilities_dropped({'capabilities': {}}, {'streaming'}) is None

    def test_a_reduced_card_still_advertising_it_is_reported(self):
        refused = _capabilities_dropped(
            {'capabilities': {'streaming': True}}, {'streaming'}
        )
        assert refused is not None and 'streaming' in refused

    def test_a_card_declaring_both_security_fields_passes(self):
        card = {
            'securitySchemes': {'bearerAuth': {}},
            'securityRequirements': [{'schemes': {'bearerAuth': {'list': []}}}],
        }
        assert _security_declared(card, set()) is None

    def test_a_card_declaring_neither_is_reported(self):
        refused = _security_declared({}, set())
        assert refused is not None
        assert 'securitySchemes' in refused and 'securityRequirements' in refused

    def test_schemes_without_requirements_is_reported(self):
        """The half-configured fixture: it claims a scheme but requires none,
        so the `SEC-AUTH-*` preconditions still would not be met."""
        refused = _security_declared(
            {'securitySchemes': {'bearerAuth': {}}, 'securityRequirements': []}, set()
        )
        assert refused is not None
        assert 'securityRequirements' in refused
        assert 'securitySchemes' not in refused

    def test_enforcement_itself_is_not_checked_here(self):
        """A card claiming a scheme it does not enforce passes this gate on
        purpose: whether the claim is true is what `SEC-AUTH-001` is for, and
        checking it here would duplicate the test the pass exists to run."""
        card = {'securitySchemes': {'x': {}}, 'securityRequirements': [{}]}
        assert _security_declared(card, set()) is None


class TestASplicedVerdictSaysWhichConfigurationProducedIt:
    """Spec §12.8 requires it, and §13.3 carries it.

    A pass obtained from an agent configured differently from the one the rest
    of the run tested is not interchangeable with an ordinary pass, and a
    report that does not say so overstates what was covered.
    """

    def _spliced(self, monkeypatch) -> list[TestResult]:
        rerun = [TestResult(
            id='SEC-AUTH-001', name='SEC-AUTH-001', level=Level.MUST,
            result=Outcome.PASS, duration_ms=1,
        )]

        async def pass_ran(*args, **kwargs):
            return rerun, {
                'securitySchemes': {'bearerAuth': {}},
                'securityRequirements': [{'schemes': {'bearerAuth': {}}}],
            }

        monkeypatch.setattr(acts_runner, '_run_pass', pass_ran)
        before = [
            skipped('SEC-AUTH-001', 'agent card authentication=False, needs True'),
            TestResult(
                id='CORE-SEND-001', name='x', level=Level.MUST,
                result=Outcome.PASS, duration_ms=1,
            ),
        ]
        return asyncio.run(acts_runner._rerun_deviation(
            before, load_suite(MANIFEST),
            deviation=AUTH, transport=TransportBinding.JSONRPC,
            variables=None, capabilities=None, declared=None, log_dir=None,
        ))

    def test_the_spliced_result_names_the_deviation(self, monkeypatch):
        after = {r.id: r for r in self._spliced(monkeypatch)}
        assert after['SEC-AUTH-001'].result is Outcome.PASS
        assert after['SEC-AUTH-001'].configuration == AUTH_ENFORCED_ENV

    def test_the_default_suts_results_are_left_unstamped(self, monkeypatch):
        """Only the spliced ids carry it; everything else ran normally."""
        after = {r.id: r for r in self._spliced(monkeypatch)}
        assert after['CORE-SEND-001'].configuration is None

    def test_the_report_carries_it(self, monkeypatch):
        from test_suite.acts.report import _test_result

        after = {r.id: r for r in self._spliced(monkeypatch)}
        assert _test_result(after['SEC-AUTH-001'])['configuration'] == (
            AUTH_ENFORCED_ENV
        )
        assert 'configuration' not in _test_result(after['CORE-SEND-001'])


class TestAFailedPassDoesNotLoseTheReport:
    """A deviated SUT that will not start is worth a warning, not the run.

    `_run_pass` raises `ActsRunError` when the agent fails to come up or its
    card cannot be read. Unguarded, that propagates out of `run()` and
    `run_acts.py` prints `error (jsonrpc): …` and exits — the whole binding's
    report lost because an *extra* pass failed. With two deviations the
    exposure doubles, so it is pinned.
    """

    def _blocked(self) -> list[TestResult]:
        return [
            skipped('SEC-AUTH-001', 'agent card authentication=False, needs True'),
            TestResult(
                id='CORE-SEND-001', name='x', level=Level.MUST,
                result=Outcome.PASS, duration_ms=1,
            ),
        ]

    def test_the_original_results_survive_a_sut_that_will_not_start(self, monkeypatch):
        async def explode(*args, **kwargs):
            raise ActsRunError('the code under test failed to start')

        monkeypatch.setattr(acts_runner, '_run_pass', explode)
        before = self._blocked()

        after = asyncio.run(acts_runner._rerun_deviation(
            before, load_suite(MANIFEST),
            deviation=AUTH, transport=TransportBinding.JSONRPC,
            variables=None, capabilities=None, declared=None, log_dir=None,
        ))

        assert after == before

    def test_an_unrelated_exception_is_not_swallowed(self, monkeypatch):
        """Only `ActsRunError` means "this pass did not happen". A bug in the
        runner must still surface."""
        async def explode(*args, **kwargs):
            raise ZeroDivisionError('a real bug')

        monkeypatch.setattr(acts_runner, '_run_pass', explode)

        with pytest.raises(ZeroDivisionError):
            asyncio.run(acts_runner._rerun_deviation(
                self._blocked(), load_suite(MANIFEST),
                deviation=AUTH, transport=TransportBinding.JSONRPC,
                variables=None, capabilities=None, declared=None, log_dir=None,
            ))

    def test_no_sut_is_started_when_nothing_is_blocked(self, monkeypatch):
        """The early-out, so an SDK already in the deviated shape pays nothing."""
        async def explode(*args, **kwargs):
            raise AssertionError('a pass was started with no blocked tests')

        monkeypatch.setattr(acts_runner, '_run_pass', explode)
        results = [TestResult(
            id='CORE-SEND-001', name='x', level=Level.MUST,
            result=Outcome.PASS, duration_ms=1,
        )]

        assert asyncio.run(acts_runner._rerun_deviation(
            results, load_suite(MANIFEST),
            deviation=AUTH, transport=TransportBinding.JSONRPC,
            variables=None, capabilities=None, declared=None, log_dir=None,
        )) == results


class TestTheEnvironmentHandover:
    """The launcher gives the child no explicit env, so it inherits ours."""

    def test_it_is_set_inside_and_gone_after(self):
        for name in (REDUCED_CAPABILITIES_ENV, AUTH_ENFORCED_ENV):
            assert name not in os.environ
            with _sut_env(name):
                assert os.environ[name] == '1'
            assert name not in os.environ

    def test_an_existing_value_is_restored(self):
        os.environ[REDUCED_CAPABILITIES_ENV] = 'caller-set'
        try:
            with _sut_env(REDUCED_CAPABILITIES_ENV):
                assert os.environ[REDUCED_CAPABILITIES_ENV] == '1'
            assert os.environ[REDUCED_CAPABILITIES_ENV] == 'caller-set'
        finally:
            os.environ.pop(REDUCED_CAPABILITIES_ENV, None)

    def test_it_is_restored_when_the_pass_raises(self):
        assert AUTH_ENFORCED_ENV not in os.environ
        try:
            with _sut_env(AUTH_ENFORCED_ENV):
                raise RuntimeError('the deviated SUT failed to start')
        except RuntimeError:
            pass
        assert AUTH_ENFORCED_ENV not in os.environ

    def test_one_deviation_does_not_leak_into_the_next(self):
        """They run back to back in `run()`, against separate SUTs."""
        with _sut_env(REDUCED_CAPABILITIES_ENV):
            pass
        with _sut_env(AUTH_ENFORCED_ENV):
            assert REDUCED_CAPABILITIES_ENV not in os.environ
        assert AUTH_ENFORCED_ENV not in os.environ


class TestTheNarrowedSuiteIsBuildable:
    """Each pass rebuilds a `LoadedSuite` by hand; keep it constructible.

    `_rerun_deviation` and `run` both narrow a suite by constructing a new
    `LoadedSuite` from the fields of the old one. Nothing else in the package
    does that, so a field added to or removed from the dataclass breaks these
    two call sites and nothing else — at runtime, after a SUT has already been
    started, which is the most expensive place to find out.
    """

    def test_the_narrowed_suite_keeps_only_the_blocked_tests(self):
        suite = load_suite(MANIFEST)
        blocked = {'CORE-CAP-001', 'PUSH-CFG-004'}

        reduced = LoadedSuite(
            tests=[t for t in suite.tests if t.id in blocked],
            variables=suite.variables,
            sources=suite.sources,
        )

        assert {t.id for t in reduced.tests} == blocked
        assert reduced.variables == suite.variables
        assert reduced.errors == []

    def test_acts_runner_passes_every_field_it_must(self):
        """Positional-by-name construction, so an unset field is a silent
        default rather than an error. Only `tests` may legitimately differ."""
        suite = load_suite(MANIFEST)
        supplied = {'tests', 'variables', 'sources'}
        defaulted = {f.name for f in fields(LoadedSuite)} - supplied

        narrowed = LoadedSuite(
            tests=suite.tests[:1], variables=suite.variables, sources=suite.sources
        )
        for name in defaulted:
            assert not getattr(narrowed, name), (
                f'{name!r} is dropped when acts_runner narrows a suite; either '
                f'carry it at both call sites or confirm an empty default is right'
            )


class TestExternalSutUrl:
    """When `sut_url` is set, no `Cluster` is spawned and deviations are skipped."""

    def test_run_pass_skips_cluster_when_sut_url_is_set(self, monkeypatch):
        import contextlib

        def no_cluster(*args, **kwargs):
            raise AssertionError('Cluster must not be instantiated when sut_url is set')

        async def fake_fetch_card(base_url, **kwargs):
            assert base_url == 'http://external.sut:8788'
            return {'supportedInterfaces': [{'protocolBinding': 'JSONRPC', 'url': base_url}]}

        class _DummyDispatcher:
            binding = TransportBinding.JSONRPC

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

        @contextlib.asynccontextmanager
        async def fake_webhook():
            yield None, None

        class _DummyRunner:
            def __init__(self, *args, **kwargs):
                pass

            async def run_suite(self, suite):
                return [
                    TestResult(
                        id='CORE-SEND-001',
                        name='x',
                        level=Level.MUST,
                        result=Outcome.PASS,
                        duration_ms=1,
                    )
                ]

        monkeypatch.setattr(acts_runner, 'Cluster', no_cluster)
        monkeypatch.setattr(acts_runner, 'fetch_agent_card', fake_fetch_card)
        monkeypatch.setattr(
            acts_runner, 'build_dispatcher', lambda *a, **kw: _DummyDispatcher()
        )
        monkeypatch.setattr(acts_runner, '_webhook_receiver', fake_webhook)
        monkeypatch.setattr(acts_runner, 'Runner', _DummyRunner)

        suite = load_suite(MANIFEST)
        results, card = asyncio.run(
            acts_runner._run_pass(
                suite,
                transport=TransportBinding.JSONRPC,
                variables=None,
                capabilities=None,
                declared=None,
                log_dir=None,
                log_name='acts_sut',
                sut_url='http://external.sut:8788',
            )
        )
        assert len(results) == 1
        assert card['supportedInterfaces'][0]['url'] == 'http://external.sut:8788'

    def test_run_skips_deviations_and_mount_dir_when_sut_url_is_set(
        self, monkeypatch, caplog
    ):
        import logging

        calls: list[str | None] = []

        async def fake_pass(suite, *, sut_url=None, **kwargs):
            calls.append(sut_url)
            return [
                skipped('CORE-CAP-002', 'agent card capability streaming=True, needs False'),
                skipped('SEC-AUTH-001', 'agent card authentication=False, needs True'),
            ], {}

        def no_repo_root():
            raise AssertionError('sut_repo_root must not be called when sut_url is set')

        monkeypatch.setattr(acts_runner, '_run_pass', fake_pass)
        monkeypatch.setattr(acts_runner, 'sut_repo_root', no_repo_root)

        # At WARNING, like a spawned deviated SUT that fails to start: the
        # run covers less than it could either way.
        with caplog.at_level(logging.WARNING, logger='acts_runner'):
            outcome = asyncio.run(
                acts_runner.run(
                    transport=TransportBinding.JSONRPC,
                    sut_url='http://external.sut:8788',
                    test_ids=['CORE-SEND-001'],
                )
            )

        assert calls == ['http://external.sut:8788']
        assert outcome.declared_behaviors is None
        assert 'CORE-CAP-002' in caplog.text
        assert 'SEC-AUTH-001' in caplog.text

    def test_run_reads_behaviors_path_directly(self, monkeypatch, tmp_path):
        behaviors_file = tmp_path / 'sut-behaviors.yaml'
        behaviors_file.write_text(
            'acts_version: "1.0"\n'
            'behaviors:\n'
            '  - prefix: "tck-complete-task"\n'
            '    description: "complete"\n',
            encoding='utf-8',
        )
        seen_declared: list[frozenset[str] | None] = []

        async def fake_pass(suite, *, declared=None, **kwargs):
            seen_declared.append(declared)
            return [], {}

        monkeypatch.setattr(acts_runner, '_run_pass', fake_pass)

        outcome = asyncio.run(
            acts_runner.run(
                transport=TransportBinding.JSONRPC,
                sut_url='http://external.sut:8788',
                test_ids=['CORE-SEND-001'],
                behaviors_path=behaviors_file,
            )
        )
        assert seen_declared == [frozenset({'tck-complete-task'})]
        assert outcome.declared_behaviors == frozenset({'tck-complete-task'})

    def test_ungated_external_sut_is_announced(self, monkeypatch, caplog):
        """Without --behaviors an external SUT runs ungated; say so, as the
        mounted path does for a checkout with no contract file."""
        import logging

        async def fake_pass(suite, **kwargs):
            return [], {}

        monkeypatch.setattr(acts_runner, '_run_pass', fake_pass)

        with caplog.at_level(logging.WARNING, logger='acts_runner'):
            asyncio.run(acts_runner.run(
                transport=TransportBinding.JSONRPC,
                sut_url='http://external.sut:8788',
                test_ids=['CORE-SEND-001'],
            ))

        assert 'gating is off' in caplog.text
        assert 'external SUT' in caplog.text


class TestDeviationInstancesForAnExternalSut:
    """An external SUT cannot be restarted, so the caller supplies the
    deviated instances. They must be used exactly as a spawned one would be:
    same claimed skips, same card check, same stamp on the verdict."""

    MAIN = 'http://main.sut:1'
    REDUCED_URL = 'http://reduced.sut:2'

    def _run(self, monkeypatch, *, reduced_card, deviation_urls=None):
        calls: list[tuple[str | None, str | None]] = []

        async def fake_pass(suite, *, sut_url=None, **kwargs):
            calls.append((sut_url, os.environ.get(REDUCED_CAPABILITIES_ENV)))
            if sut_url == self.MAIN:
                return [skipped(
                    'CORE-CAP-002',
                    'agent card capability streaming=True, needs False',
                )], {}
            return [TestResult(
                id='CORE-CAP-002', name='x', level=Level.MUST,
                result=Outcome.PASS, duration_ms=1,
            )], reduced_card

        monkeypatch.setattr(acts_runner, '_run_pass', fake_pass)
        outcome = asyncio.run(acts_runner.run(
            transport=TransportBinding.JSONRPC,
            sut_url=self.MAIN,
            test_ids=['CORE-CAP-002'],
            gate_on_behaviors=False,
            deviation_urls=(
                {REDUCED_CAPABILITIES_ENV: self.REDUCED_URL}
                if deviation_urls is None else deviation_urls
            ),
        ))
        return outcome, calls

    def test_the_given_instance_is_dialed_and_nothing_is_spawned(self, monkeypatch):
        outcome, calls = self._run(
            monkeypatch, reduced_card={'capabilities': {'streaming': False}},
        )
        # The variable stays out of our environment: the caller set it on the
        # instance, and setting it here would reach nothing.
        assert calls == [(self.MAIN, None), (self.REDUCED_URL, None)]
        (verdict,) = outcome.results
        assert verdict.result is Outcome.PASS
        assert verdict.configuration == REDUCED_CAPABILITIES_ENV

    def test_an_instance_that_did_not_deviate_keeps_the_skip(self, monkeypatch):
        outcome, _ = self._run(
            monkeypatch, reduced_card={'capabilities': {'streaming': True}},
        )
        (verdict,) = outcome.results
        assert verdict.result is Outcome.SKIP
        assert verdict.configuration is None

    def test_deviation_urls_need_an_external_sut(self, monkeypatch):
        async def no_pass(*args, **kwargs):
            raise AssertionError('no pass may start for an inconsistent request')

        monkeypatch.setattr(acts_runner, '_run_pass', no_pass)
        with pytest.raises(ActsRunError, match='pass sut_url too'):
            asyncio.run(acts_runner.run(
                transport=TransportBinding.JSONRPC,
                deviation_urls={REDUCED_CAPABILITIES_ENV: self.REDUCED_URL},
            ))

    def test_an_unknown_deviation_is_refused(self, monkeypatch):
        with pytest.raises(ActsRunError, match='ITK_NO_SUCH_MODE'):
            asyncio.run(acts_runner.run(
                transport=TransportBinding.JSONRPC,
                sut_url=self.MAIN,
                deviation_urls={'ITK_NO_SUCH_MODE': self.REDUCED_URL},
            ))


class TestAnExternalRunIsMarkedInTheReport:
    def _report(self, *, external: bool) -> dict:
        run = acts_runner.ActsRun(
            results=[TestResult(
                id='CORE-SEND-001', name='x', level=Level.MUST,
                result=Outcome.PASS, duration_ms=1,
            )],
            suite=load_suite(MANIFEST),
            transport=TransportBinding.JSONRPC,
            duration_ms=1,
            external_sut=external,
        )
        return acts_runner.to_report(run, sdk_name='a2a-x')

    def test_an_external_sut_is_named_as_such(self):
        assert self._report(external=True)['environment']['sut'] == 'external'

    def test_a_spawned_sut_carries_no_marker(self):
        assert 'sut' not in self._report(external=False)['environment']
