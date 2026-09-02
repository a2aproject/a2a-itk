"""The pinned ACTS corpus in ``scenarios/acts/`` loads, and stays as pinned.

Two things are being asserted:

1. the schema and loader handle the real corpus, not just fixtures; and
2. the compat rules that make it loadable fire on exactly the sites they are
   documented to fire on.

The second is what lets the corpus stay verbatim *and* runnable: the gap
between what upstream ships and what we can execute is stated as a number, so
it cannot widen unnoticed.

A refresh should make these fail. That is the prompt to re-read
``PROVENANCE.md``, and — where a site count has dropped to zero — to delete
the corresponding compat rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_suite.acts import (
    EXPECTED_SITES,
    Level,
    Operation,
    StepKind,
    TransportBinding,
    load_suite,
    site_counts,
)


CORPUS = Path(__file__).resolve().parent.parent / 'scenarios' / 'acts'
MANIFEST = CORPUS / 'suite.acts.yaml'


@pytest.fixture(scope='module')
def corpus():
    """The whole corpus, loaded strictly, with compat rules applied.

    Strict on purpose: with the rules in ``compat.py`` the corpus is fully
    valid, and the day it stops being so is the day we want to hear about it.
    """
    return load_suite(MANIFEST)


@pytest.fixture(scope='module')
def verbatim():
    """The corpus exactly as upstream ships it — no compat, so not strict.

    Every assertion made against this fixture is a statement about a defect
    that is still open upstream. When one is fixed, the assertion fails, and
    that is the signal to drop both it and the rule that worked around it.
    """
    return load_suite(MANIFEST, strict=False, compat=False)


class TestCorpusLoads:
    def test_loads_strictly_with_no_errors(self, corpus):
        assert corpus.errors == []

    def test_expected_test_count(self, corpus):
        """111 tests, per the PR description. A change here means the corpus
        moved; update the pin in PROVENANCE.md deliberately."""
        assert len(corpus) == 111

    def test_every_included_file_is_read(self, corpus):
        """14 suite files plus the manifest itself."""
        assert len(corpus.sources) == 15
        assert corpus.sources[0].name == 'suite.acts.yaml'

    def test_manifest_includes_every_suite_file_on_disk(self, corpus):
        """A file added to the directory but not to `include:` would sit
        there looking like coverage while never running."""
        on_disk = {p.name for p in CORPUS.glob('*.acts.yaml')}
        assert on_disk == {p.name for p in corpus.sources}

    def test_suite_ids(self, corpus):
        assert corpus.suite_ids() == [
            'discovery', 'core-operations', 'history', 'multi-turn',
            'streaming', 'polling', 'error-handling', 'auth-security',
            'version-negotiation', 'wire-format', 'data-types',
            'push-notifications', 'jsonrpc-transport', 'rest-transport',
            'grpc-transport', 'client-parsing',
        ]

    def test_level_breakdown(self, corpus):
        """Feeds the report's `by_level` summary (spec §13.2)."""
        assert {lv.value: len(corpus.by_level(lv)) for lv in Level} == {
            'must': 65, 'should': 33, 'may': 13,
        }

    def test_variables_come_from_the_manifest_and_suites(self, corpus):
        assert corpus.variables == {
            'baseUrl': '{{SUT_BASE_URL}}',
            'webhookUrl': 'https://example.com/webhooks/a2a-tests',
        }


class TestCorpusShape:
    def test_every_test_has_at_least_one_step(self, corpus):
        assert all(entry.test.steps for entry in corpus)

    def test_every_step_has_a_resolvable_kind(self, corpus):
        counts = {k: 0 for k in StepKind}
        for entry in corpus:
            for step in entry.test.steps:
                counts[step.kind()] += 1
        assert counts == {
            StepKind.OPERATION: 139,
            StepKind.RAW: 21,
            StepKind.CLIENT: 9,
            StepKind.ASSERTION: 0,
        }

    def test_transport_restricted_tests_are_a_minority(self, corpus):
        """Most tests are transport-agnostic; that is what makes one corpus
        runnable against all three bindings."""
        restricted = [e for e in corpus if e.test.transport]
        assert len(restricted) == 25
        assert len(corpus.for_transport(TransportBinding.JSONRPC)) == 101
        assert len(corpus.for_transport(TransportBinding.GRPC)) == 89
        assert len(corpus.for_transport(TransportBinding.REST)) == 93

    def test_every_step_reference_names_a_real_step(self, corpus):
        """A dotted `{{step.var}}` is a capture reference.

        One naming a step that does not exist can only fail at run time, as a
        missing-variable error rather than the typo it is.
        """
        for entry in corpus:
            step_ids = {s.id for s in entry.test.steps}
            for step in entry.test.steps:
                for ref in _step_references(step):
                    if '.' not in ref:
                        continue
                    prefix = ref.split('.', 1)[0]
                    assert prefix in step_ids, (
                        f'{entry.id} step {step.id}: {{{{{ref}}}}} names no '
                        f'step in this test (have {sorted(step_ids)})'
                    )

    def test_runner_supplied_variables(self, corpus):
        """Undotted `{{name}}` references that no document variable defines.

        The runner has to inject these, and an unnoticed addition to the
        list would surface as an unsubstituted `{{...}}` going out on the
        wire.
        """
        bare = {
            ref
            for entry in corpus
            for step in entry.test.steps
            for ref in _step_references(step)
            if '.' not in ref
        }
        assert sorted(bare - set(corpus.variables)) == [
            'insufficientAuthToken',
            'otherUserTaskId',
        ]


class TestBehaviorContract:
    """The `tck-*` set each SDK's agent has to implement."""

    def test_required_behaviors(self, corpus):
        assert sorted(corpus.required_behaviors()) == [
            'tck-artifact-data',
            'tck-artifact-file',
            'tck-artifact-file-url',
            'tck-artifact-text',
            'tck-auth-required',
            'tck-cancel',
            'tck-complete-task',
            'tck-long-running',
            'tck-message-response',
            'tck-multi-turn',
            'tck-stream-basic',
            'tck-stream-chunked',
            'tck-task-failure',
        ]

    def test_every_behavior_uses_the_tck_prefix(self, corpus):
        assert all(b.startswith('tck-') for b in corpus.required_behaviors())

    def test_how_many_tests_need_a_behavior(self, corpus):
        """A test needing no behavior exercises stock protocol handling; one
        that does needs the SUT to recognise the `tck-*` prefix and play along.

        Note the corpus also writes `requires_behaviors: []` explicitly on
        some tests, so "declares the key" (80) is not "needs a behavior" (70).
        """
        assert len([e for e in corpus if e.test.behaviors()]) == 70
        assert len([e for e in corpus if e.test.requires_behaviors is not None]) == 80


class TestVerbatimCorpusIsStillBroken:
    """PROVENANCE §A — the corpus as upstream ships it, load-blocking defects.

    Each assertion here describes a defect that is **still open upstream**.
    They are written to fail when it is fixed, because that is precisely when
    the matching compat rule should be deleted.
    """

    def test_twenty_six_tests_fail_the_cddl(self, verbatim):
        assert len(verbatim.errors) == 26
        assert len(verbatim.tests) == 85

    def test_the_whole_push_suite_is_unloadable(self, verbatim):
        """The most expensive consequence: no push-notification coverage at
        all without compat, which is why the rules exist rather than the 26
        tests simply being skipped."""
        loaded = {entry.id for entry in verbatim}
        assert not [tid for tid in loaded if tid.startswith('PUSH-')]

    def test_strict_load_of_the_verbatim_corpus_raises(self):
        from test_suite.acts import ActsFileError

        with pytest.raises(ActsFileError, match='expect_error.code'):
            load_suite(MANIFEST, compat=False)


class TestCompatRulesFireWherePinned:
    """PROVENANCE §A — the rewrite table, asserted site by site.

    A count that moves means the corpus moved. A count that reaches zero
    means upstream fixed that defect and the rule is now dead code.
    """

    def test_site_counts_match_the_pinned_table(self, corpus):
        assert site_counts(corpus.rewrites) == EXPECTED_SITES

    def test_every_rewrite_names_a_real_test(self, corpus):
        known = {entry.id for entry in corpus}
        for rewrite in corpus.rewrites:
            test_id = rewrite.where.split('.', 1)[0]
            assert test_id in known, rewrite

    def test_no_push_notification_config_operation_names_survive(self, corpus):
        """The spec's abstract-operation enum has no `*_push_notification_config`
        member, so a surviving one would be undispatchable."""
        used = {
            step.operation for entry in corpus for step in entry.test.steps
            if step.operation is not None
        }
        assert Operation.CREATE_PUSH_CONFIG in used
        assert all('push_notification' not in op.value for op in used)

    def test_failure_assertions_become_expect_error(self, corpus):
        """`expect: {error: ...}` becomes a bare `expect_error` — "some A2A
        error". Deliberately not a *named* error: picking which one would be
        us writing the test rather than running it."""
        for test_id, step_id in [
            ('CORE-MULTI-006', 'turn2'),
            ('STREAM-SUB-003', 'subscribe'),
        ]:
            entry = corpus.by_id(test_id)
            assert entry is not None, test_id
            step = next(s for s in entry.test.steps if s.id == step_id)
            assert step.expect is None
            assert step.expect_error is not None
            assert step.expect_error.error_type is None

    def test_response_assertions_move_under_expect_body(self, corpus):
        for test_id in ('STREAM-SUB-001', 'STREAM-SUB-003'):
            entry = corpus.by_id(test_id)
            first = entry.test.steps[0]
            assert first.expect is not None
            assert 'task' in first.expect.body


class TestKnownDefectsLeftInPlace:
    """PROVENANCE §B — defects that parse, so they are simply left wrong.

    Compensating for these in code would mean deciding what a conformance
    test *meant*. They stay broken, visibly, until upstream fixes them.
    """

    def test_version_negotiation_uses_the_normative_jsonrpc_code(self, corpus):
        """Not a defect, despite a review comment on #1882 calling it one.

        `VER-NEG-001` asserts -32009, matching A2A §5.4 and the reference SDK.
        ACTS §6.2 says -32006 and
        [r3305157228](https://github.com/a2aproject/A2A/pull/1882#discussion_r3305157228)
        asks the corpus to follow it — but -32006 is
        `InvalidAgentResponseError`, and the ACTS table's own footnote defers
        to A2A. Pinned so nobody "corrects" this into being wrong.
        """
        entry = corpus.by_id('VER-NEG-001')
        assert entry.test.steps[0].expect.body['error']['code'] == -32009

    def test_inline_file_part_uses_bytes_not_raw(self, corpus):
        """The `Part` proto calls the base64 field `raw`."""
        entry = corpus.by_id('CLIENT-PARSE-006')
        payload = entry.test.steps[0].client_response.wire_payload
        files = list(_find_key(payload, 'file'))
        assert files, 'expected an inline file part in the golden payload'
        assert any('bytes' in f for f in files)
        assert not any('raw' in f for f in files)

    def test_rest_problem_details_asserts_a_format_a2a_does_not_use(self, corpus):
        """A2A §11.6 mandates the `google.rpc.Status` shape, not RFC 7807."""
        step = corpus.by_id('REST-PD-001').test.steps[0]
        assert set(step.expect.body) == {'type', 'title', 'status'}

    def test_a_grpc_only_test_asserts_an_http_status(self, corpus):
        """gRPC has no HTTP status, so both of these are silent no-ops."""
        entry = corpus.by_id('GRPC-STREAM-002')
        assert entry.test.transport == [TransportBinding.GRPC]
        assert [s.expect.status for s in entry.test.steps] == [200, 200]

    def test_runner_requirements_is_never_used(self, corpus):
        """The spec field for "this test needs a runner capability" — and 23
        tests that need one say so only in prose, tagged `runner-special`."""
        assert all(e.test.runner_requirements is None for e in corpus)
        special = [e for e in corpus if 'runner-special' in (e.test.tags or [])]
        assert len(special) == 23


class TestKnownDivergencesStillPresent:
    """PROVENANCE §C — shapes that are legal, or arguably so.

    Pinned so that "we decided not to touch this" stays a decision on the
    record rather than something a later reader assumes was an oversight.
    """

    def test_error_assertions_that_do_not_name_an_error_type(self, corpus):
        """Five come from the corpus, two from the `expect.error` compat
        rule; all seven mean "some A2A error, don't constrain which"."""
        unconstrained = [
            (entry.id, step.id)
            for entry in corpus for step in entry.test.steps
            if step.expect_error is not None and step.expect_error.error_type is None
        ]
        assert sorted(unconstrained) == [
            ('CORE-CTX-001', 'send'),
            ('CORE-ERR-009', 'get-missing'),
            ('CORE-MULTI-003', 'mismatch'),
            ('CORE-MULTI-006', 'turn2'),
            ('SEC-AUTH-003', 'get-missing-task'),
            ('SEC-AUTH-003', 'get-other-user-task'),
            ('STREAM-SUB-003', 'subscribe'),
        ]

    def test_one_error_assertion_is_an_assertion_object(self, corpus):
        step = corpus.by_id('CORE-ERR-002').test.steps[0]
        assert step.expect_error.literal_error_type() is None
        assert step.expect_error.error_type == {
            'one_of': ['TaskNotFoundError', 'TaskNotCancelableError'],
        }

    def test_extended_card_is_a_param_not_a_separate_operation(self, corpus):
        entry = corpus.by_id('CARD-EXT-001')
        step = entry.test.steps[0]
        assert step.operation is Operation.GET_AGENT_CARD
        assert step.params == {'extended': True}


def _step_references(step):
    """Every ``{{...}}`` reference anywhere in a step's inputs.

    Params, raw request parts and expect blocks all substitute, so all three
    are walked.
    """
    sources = [step.params]
    if step.raw is not None:
        sources += [step.raw.path, step.raw.headers, step.raw.body, step.raw.body_raw]
    if step.expect is not None:
        sources += [step.expect.status, step.expect.body]
    for source in sources:
        yield from _template_refs(source)


def _template_refs(value):
    """Every ``{{...}}`` reference inside a nested value."""
    if isinstance(value, str):
        rest = value
        while '{{' in rest:
            _, _, rest = rest.partition('{{')
            ref, _, rest = rest.partition('}}')
            yield ref.strip()
    elif isinstance(value, dict):
        for v in value.values():
            yield from _template_refs(v)
    elif isinstance(value, list):
        for v in value:
            yield from _template_refs(v)


def _find_key(node, key):
    """Every mapping stored under ``key``, at any depth."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, dict):
                yield v
            yield from _find_key(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _find_key(v, key)
