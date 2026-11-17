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
    NoApplicableTests,
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
            spec_version='1.0',
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
            spec_version='1.0',
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
                spec_version='1.0',
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
            spec_version='1.0',
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
                spec_version='1.0',
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


def _interface(version: str | None, url: str, binding: str = 'JSONRPC') -> dict:
    interface = {'protocolBinding': binding, 'url': url}
    if version is not None:
        interface['protocolVersion'] = version
    return interface


class TestWhichInterfaceARunDials:
    """`interface_for` picks the card's interface for the run version.

    The wrong pick is expensive in both directions: an interface for another
    version answers every request with a version error, which reads as a
    report full of conformance failures, and refusing a usable interface
    loses the run.
    """

    def pick(self, spec_version: str, *interfaces: dict) -> tuple[str, str | None]:
        return acts_runner.interface_for(
            {'supportedInterfaces': list(interfaces)},
            TransportBinding.JSONRPC,
            spec_version,
        )

    def test_the_exact_version_wins_wherever_it_is_listed(self):
        card = (
            _interface('0.3', 'http://sut/v03'),
            _interface('1.1', 'http://sut/v11'),
            _interface('1.0', 'http://sut/v10'),
        )
        assert self.pick('1.0', *card) == ('http://sut/v10', '1.0')
        assert self.pick('1.1', *card) == ('http://sut/v11', '1.1')

    def test_a_newer_minor_serves_an_older_run(self):
        """How a 1.0 run asks whether a 1.1 server still behaves as 1.0."""
        assert self.pick('1.0', _interface('1.1', 'http://sut/v11')) == (
            'http://sut/v11', '1.1',
        )

    def test_the_newest_of_several_newer_minors_is_used(self):
        card = (
            _interface('1.1', 'http://sut/v11'),
            _interface('1.10', 'http://sut/v110'),
            _interface('1.9', 'http://sut/v19'),
        )
        assert self.pick('1.0', *card) == ('http://sut/v110', '1.10')

    def test_an_older_minor_is_a_configuration_error(self):
        """A 1.0 interface answers a 1.1 request with a version error, so the
        run would report every test as a failure of the SUT."""
        with pytest.raises(ActsRunError) as exc:
            self.pick(
                '1.1',
                _interface('1.0', 'http://sut/v10'),
                _interface('0.3', 'http://sut/v03'),
            )
        assert 'no jsonrpc interface for A2A 1.1' in str(exc.value)
        assert '1.0, 0.3' in str(exc.value)

    def test_another_major_is_never_used(self):
        with pytest.raises(ActsRunError, match='no jsonrpc interface for A2A 1.0'):
            self.pick('1.0', _interface('0.3', 'http://sut/v03'))
        with pytest.raises(ActsRunError, match='no jsonrpc interface for A2A 1.0'):
            self.pick('1.0', _interface('2.0', 'http://sut/v20'))

    def test_an_interface_without_a_version_is_the_last_resort(self):
        card = (_interface(None, 'http://sut/any'), _interface('1.1', 'http://sut/v11'))
        assert self.pick('1.1', *card) == ('http://sut/v11', '1.1')
        assert self.pick('1.0', *card) == ('http://sut/v11', '1.1')
        assert self.pick('1.2', *card) == ('http://sut/any', None)

    def test_a_patch_component_on_the_card_is_ignored(self):
        card = (_interface(None, 'http://sut/any'), _interface('1.0.0', 'http://sut/v10'))
        assert self.pick('1.0', *card) == ('http://sut/v10', '1.0.0')

    @pytest.mark.parametrize('written', ['v1.0', '1', 'latest', '1.0-rc'])
    def test_a_version_that_cannot_be_read_is_never_used(self, written):
        """It is not the same as no version: the interface says it serves
        something, and nothing shows that it is the run version."""
        with pytest.raises(ActsRunError, match='no jsonrpc interface for A2A 1.0'):
            self.pick('1.0', _interface(written, 'http://sut/odd'))

    def test_only_the_requested_binding_is_considered(self):
        with pytest.raises(ActsRunError, match='advertises no jsonrpc interface'):
            self.pick('1.1', _interface('1.1', 'sut:50051', binding='GRPC'))


VERSIONED_SUITE = '''\
acts_version: "1.0"
spec_version: "{written_against}"
suites:
  - id: core
    name: Core
    tests:
      - id: T-EVERY
        name: every version
        level: must
        steps: [{{id: s, operation: get_task, params: {{id: x}}}}]
      - id: T-ONLY-10
        name: 1.0 only
        level: must
        max_spec_version: "1.0"
        steps: [{{id: s, operation: get_task, params: {{id: x}}}}]
      - id: T-FROM-11
        name: from 1.1
        level: must
        min_spec_version: "1.1"
        steps: [{{id: s, operation: get_task, params: {{id: x}}}}]
'''


class TestARunTargetsOneVersion:
    """`spec_version` is the one input that selects the tests, sets the
    header, picks the interface and names the version in the report."""

    @pytest.fixture
    def suite_path(self, tmp_path):
        path = tmp_path / 'versioned.acts.yaml'
        path.write_text(VERSIONED_SUITE.format(written_against='1.1'), encoding='utf-8')
        return path

    def _run(self, monkeypatch, suite_path, **kwargs):
        seen: dict[str, object] = {}

        async def fake_pass(suite, *, spec_version=None, **_):
            seen['ids'] = [t.id for t in suite.tests]
            seen['spec_version'] = spec_version
            return [], {}

        monkeypatch.setattr(acts_runner, '_run_pass', fake_pass)
        outcome = asyncio.run(acts_runner.run(
            transport=TransportBinding.JSONRPC,
            sut_url='http://external.sut:8788',
            suite_path=suite_path,
            gate_on_behaviors=False,
            **kwargs,
        ))
        return outcome, seen

    def test_the_default_is_1_0(self, monkeypatch, suite_path):
        outcome, seen = self._run(monkeypatch, suite_path)
        assert seen == {'ids': ['T-EVERY', 'T-ONLY-10'], 'spec_version': '1.0'}
        assert outcome.spec_version == '1.0'

    def test_a_1_1_run_gets_the_1_1_tests(self, monkeypatch, suite_path):
        outcome, seen = self._run(monkeypatch, suite_path, spec_version='1.1')
        assert seen == {'ids': ['T-EVERY', 'T-FROM-11'], 'spec_version': '1.1'}
        assert outcome.spec_version == '1.1'

    def test_a_test_outside_the_range_is_absent_from_the_report(
        self, monkeypatch, suite_path
    ):
        """Absent, not skipped: a 1.0 run must report the same tests after
        1.1 tests are added to the corpus as it did before."""
        outcome, _ = self._run(monkeypatch, suite_path)
        assert [t.id for t in outcome.suite.tests] == ['T-EVERY', 'T-ONLY-10']

    def test_a_selection_outside_the_range_is_refused(self, monkeypatch, suite_path):
        with pytest.raises(NoApplicableTests) as exc:
            self._run(monkeypatch, suite_path, test_ids=['T-FROM-11'])
        assert 'applies to A2A 1.0' in str(exc.value)
        assert 'T-FROM-11 (A2A 1.1 and newer)' in str(exc.value)

    def test_a_document_is_not_run_past_the_version_it_was_written_against(
        self, monkeypatch, tmp_path
    ):
        """An unbounded test claims to hold only up to its document's
        `spec_version`; nobody checked it against anything newer."""
        path = tmp_path / 'old.acts.yaml'
        path.write_text(VERSIONED_SUITE.format(written_against='1.0'), encoding='utf-8')
        with pytest.raises(ActsRunError) as exc:
            self._run(monkeypatch, path, spec_version='1.1')
        assert 'cannot run at A2A 1.1' in str(exc.value)
        assert 'old.acts.yaml (1.0)' in str(exc.value)

    def test_the_mirrored_corpus_refuses_a_version_it_does_not_cover(
        self, monkeypatch
    ):
        """Asked for a version newer than the corpus, the run stops before it
        starts a SUT instead of grading the SUT on tests for another version."""
        async def no_pass(*args, **kwargs):
            raise AssertionError('no pass may start')

        monkeypatch.setattr(acts_runner, '_run_pass', no_pass)
        with pytest.raises(ActsRunError, match='cannot run at A2A 9.0'):
            asyncio.run(acts_runner.run(
                transport=TransportBinding.JSONRPC,
                sut_url='http://external.sut:8788',
                spec_version='9.0',
            ))

    @pytest.mark.parametrize('bad', ['1', '1.1.0', 'latest', '', '01.0'])
    def test_a_run_version_that_is_not_a_version_is_refused(
        self, monkeypatch, suite_path, bad
    ):
        with pytest.raises(ActsRunError, match='spec_version'):
            self._run(monkeypatch, suite_path, spec_version=bad)

    def test_a_version_older_than_the_suite_covers_is_refused(
        self, monkeypatch, suite_path
    ):
        """An unbounded test holds from 1.0 on, not for 0.3."""
        with pytest.raises(ActsRunError, match='covers A2A 1.0 and newer'):
            self._run(monkeypatch, suite_path, spec_version='0.3')

    def test_a_deviation_pass_runs_at_the_same_version(self, monkeypatch, suite_path):
        versions: list[str | None] = []

        async def fake_pass(suite, *, spec_version=None, sut_url=None, **_):
            versions.append(spec_version)
            if sut_url == 'http://main.sut:1':
                return [skipped(
                    'T-EVERY', 'agent card capability streaming=True, needs False',
                )], {}
            return [TestResult(
                id='T-EVERY', name='x', level=Level.MUST,
                result=Outcome.PASS, duration_ms=1,
            )], {'capabilities': {'streaming': False}}

        monkeypatch.setattr(acts_runner, '_run_pass', fake_pass)
        asyncio.run(acts_runner.run(
            transport=TransportBinding.JSONRPC,
            spec_version='1.1',
            sut_url='http://main.sut:1',
            suite_path=suite_path,
            gate_on_behaviors=False,
            deviation_urls={REDUCED_CAPABILITIES_ENV: 'http://reduced.sut:2'},
        ))
        assert versions == ['1.1', '1.1']


class TestOnePassSpeaksTheRunVersion:
    """Inside a pass, the card fetch, the interface and the runner all take
    the same version."""

    def test_the_version_reaches_the_card_the_dispatcher_and_the_runner(
        self, monkeypatch
    ):
        import contextlib

        seen: dict[str, object] = {}

        async def fake_fetch_card(base_url, *, spec_version, **kwargs):
            seen['card'] = spec_version
            return {'supportedInterfaces': [
                _interface('1.0', 'http://sut/v10'),
                _interface('1.1', 'http://sut/v11'),
            ]}

        class _Dispatcher:
            binding = TransportBinding.JSONRPC

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

        def fake_for_binding(binding, url, **kwargs):
            seen['url'] = url
            return _Dispatcher()

        class _Runner:
            def __init__(self, dispatcher, *, spec_version, **kwargs):
                seen['runner'] = spec_version

            async def run_suite(self, suite):
                return []

        @contextlib.asynccontextmanager
        async def fake_webhook():
            yield None, None

        monkeypatch.setattr(acts_runner, 'fetch_agent_card', fake_fetch_card)
        monkeypatch.setattr(acts_runner, 'for_binding', fake_for_binding)
        monkeypatch.setattr(acts_runner, '_webhook_receiver', fake_webhook)
        monkeypatch.setattr(acts_runner, 'Runner', _Runner)

        asyncio.run(acts_runner._run_pass(
            load_suite(MANIFEST),
            transport=TransportBinding.JSONRPC,
            variables=None,
            capabilities=None,
            declared=None,
            log_dir=None,
            log_name='acts_sut',
            sut_url='http://external.sut:8788',
            spec_version='1.1',
        ))
        assert seen == {'card': '1.1', 'url': 'http://sut/v11', 'runner': '1.1'}

    def test_the_card_is_fetched_with_the_run_version(self, monkeypatch):
        import httpx

        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={'name': 'agent'})

        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            acts_runner.httpx, 'AsyncClient',
            lambda **kwargs: real_client(
                transport=httpx.MockTransport(handler), **kwargs
            ),
        )
        asyncio.run(acts_runner.fetch_agent_card('http://sut', spec_version='1.1'))
        assert [r.headers['A2A-Version'] for r in requests] == ['1.1']


class TestTheReportNamesTheVersion:
    def _report(self, *, spec_version: str, card: dict) -> dict:
        run = acts_runner.ActsRun(
            results=[TestResult(
                id='CORE-SEND-001', name='x', level=Level.MUST,
                result=Outcome.PASS, duration_ms=1,
            )],
            suite=load_suite(MANIFEST),
            transport=TransportBinding.JSONRPC,
            duration_ms=1,
            agent_card=card,
            spec_version=spec_version,
        )
        return acts_runner.to_report(run, sdk_name='a2a-x')

    def test_the_report_carries_the_run_version(self):
        card = {'supportedInterfaces': [_interface('1.1', 'http://sut/v11')]}
        report = self._report(spec_version='1.1', card=card)
        assert report['spec_version'] == '1.1'
        assert report['environment']['interface_protocol_version'] == '1.1'

    def test_the_interface_version_is_recorded_when_it_differs(self):
        """A 1.0 run against a 1.1 interface is a 1.0 result, and the report
        says which interface produced it."""
        card = {'supportedInterfaces': [_interface('1.1', 'http://sut/v11')]}
        report = self._report(spec_version='1.0', card=card)
        assert report['spec_version'] == '1.0'
        assert report['environment']['interface_protocol_version'] == '1.1'

    def test_a_run_without_a_card_records_no_interface(self):
        report = self._report(spec_version='1.0', card={})
        assert 'interface_protocol_version' not in report['environment']
