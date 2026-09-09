"""The reduced-capability second pass in `acts_runner`.

Four corpus tests assert that an agent *lacking* a capability answers
`UnsupportedOperationError`, so their preconditions require the card not to
advertise it. Against a fully capable SUT they skip, and the protocol's
"unsupported" branch goes untested by anybody — which is what the second pass
exists to fix. What is pinned here is which skips it claims: picking up one it
cannot clear would cost a whole SUT startup for nothing, and missing one would
put the hole back.
"""

from __future__ import annotations

import os

from acts_runner import (
    REDUCED_CAPABILITIES_ENV,
    _capability_skips,
    _reduced_capabilities,
)
from test_suite.acts.runner import UNSATISFIABLE, Outcome, TestResult
from test_suite.acts.schema import Level


def skipped(test_id: str, reason: str) -> TestResult:
    return TestResult(
        id=test_id,
        name=test_id,
        level=Level.MUST,
        result=Outcome.SKIP,
        duration_ms=0,
        skip_reason=reason,
    )


class TestWhichSkipsTheSecondPassClaims:
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
            'SEC-AUTH-001',
            f"{UNSATISFIABLE}'authentication' is not a capability A2A defines",
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


class TestTheEnvironmentHandover:
    """The launcher gives the child no explicit env, so it inherits ours."""

    def test_it_is_set_inside_and_gone_after(self):
        assert REDUCED_CAPABILITIES_ENV not in os.environ
        with _reduced_capabilities():
            assert os.environ[REDUCED_CAPABILITIES_ENV] == '1'
        assert REDUCED_CAPABILITIES_ENV not in os.environ

    def test_an_existing_value_is_restored(self):
        os.environ[REDUCED_CAPABILITIES_ENV] = 'caller-set'
        try:
            with _reduced_capabilities():
                assert os.environ[REDUCED_CAPABILITIES_ENV] == '1'
            assert os.environ[REDUCED_CAPABILITIES_ENV] == 'caller-set'
        finally:
            os.environ.pop(REDUCED_CAPABILITIES_ENV, None)

    def test_it_is_restored_when_the_pass_raises(self):
        assert REDUCED_CAPABILITIES_ENV not in os.environ
        try:
            with _reduced_capabilities():
                raise RuntimeError('the reduced SUT failed to start')
        except RuntimeError:
            pass
        assert REDUCED_CAPABILITIES_ENV not in os.environ
