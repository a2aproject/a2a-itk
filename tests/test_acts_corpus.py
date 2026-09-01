"""The pinned ACTS corpus in ``scenarios/acts/`` loads, and stays as pinned.

This is story 4.1's acceptance test. Two things are being asserted:

1. the schema and loader handle the real corpus, not just fixtures; and
2. the corpus is exactly what ``PROVENANCE.md`` says it is — the upstream
   snapshot plus the recorded corrections, and nothing else.

The second matters because a conformance corpus that quietly drifts from
upstream is the failure ACTS exists to remove. A refresh should make these
fail; that is the prompt to re-read PROVENANCE.md and update it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_suite.acts import (
    ErrorType,
    Level,
    Operation,
    StepKind,
    TransportBinding,
    load_suite,
)


CORPUS = Path(__file__).resolve().parent.parent / 'scenarios' / 'acts'
MANIFEST = CORPUS / 'suite.acts.yaml'


@pytest.fixture(scope='module')
def corpus():
    """The whole corpus, loaded strictly.

    Strict on purpose: after the corrections in PROVENANCE.md the corpus is
    fully valid, and the day it stops being so is the day we want to hear
    about it.
    """
    return load_suite(MANIFEST)


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

        The runner has to inject these, so story 4.6 needs the list — and an
        unnoticed addition to it would surface as an unsubstituted `{{...}}`
        going out on the wire.
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
    """The `tck-*` set story 4.5 has to implement in each SDK's agent."""

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
        that does needs the SUT to play along, which is what 4.5 builds.

        Note the corpus also writes `requires_behaviors: []` explicitly on
        some tests, so "declares the key" (80) is not "needs a behavior" (70).
        """
        assert len([e for e in corpus if e.test.behaviors()]) == 70
        assert len([e for e in corpus if e.test.requires_behaviors is not None]) == 80


class TestProvenanceCorrectionsHeld:
    """The corrections recorded in PROVENANCE.md, asserted as properties.

    If upstream fixes one of these and we refresh, the corresponding
    assertion keeps passing — it describes the corrected state, not the act
    of correcting. If a refresh *reintroduces* a defect, it fails here.
    """

    def test_no_push_notification_config_operation_names(self, corpus):
        """PROVENANCE §A.1 — the spec's abstract-operation enum has no
        `*_push_notification_config` member."""
        used = {
            step.operation for entry in corpus for step in entry.test.steps
            if step.operation is not None
        }
        assert Operation.CREATE_PUSH_CONFIG in used
        assert all('push_notification' not in op.value for op in used)

    def test_expect_error_uses_error_type_not_code(self, corpus):
        """PROVENANCE §A.2 — `code` is not a field of `expect-error`; the
        schema forbids extras, so this holds by construction. Asserted
        anyway, because a refresh reintroducing `code` would fail the load
        with a less obvious message."""
        blocks = [
            step.expect_error for entry in corpus for step in entry.test.steps
            if step.expect_error is not None
        ]
        assert blocks
        assert all(not hasattr(b, 'code') for b in blocks)

    def test_version_not_supported_maps_to_32006(self, corpus):
        """PROVENANCE §A.5 — the spec assigns -32006, not -32009."""
        entry = corpus.by_id('VER-NEG-001')
        assert entry is not None
        assert entry.test.steps[0].expect.body['error']['code'] == -32006

    def test_failure_assertions_use_expect_error(self, corpus):
        """PROVENANCE §A.4/§A.6 — a failure is asserted with `expect_error`,
        never by smuggling an `error` key into `expect`."""
        for test_id, step_id, error_type in [
            ('CORE-MULTI-006', 'turn2', ErrorType.INVALID_PARAMS),
            ('STREAM-SUB-003', 'subscribe', ErrorType.UNSUPPORTED_OPERATION),
        ]:
            entry = corpus.by_id(test_id)
            assert entry is not None, test_id
            step = next(s for s in entry.test.steps if s.id == step_id)
            assert step.expect is None
            assert step.expect_error.literal_error_type() is error_type

    def test_response_assertions_live_under_expect_body(self, corpus):
        """PROVENANCE §B — a response field directly under `expect` is never
        evaluated. Forbidden by the schema; pinned here so the reason is
        recorded next to the corpus it was found in."""
        for test_id in ('STREAM-SUB-001', 'STREAM-SUB-003'):
            entry = corpus.by_id(test_id)
            first = entry.test.steps[0]
            assert first.expect is not None
            assert 'task' in first.expect.body

    def test_inline_file_part_uses_raw(self, corpus):
        """PROVENANCE §A.3 — the `Part` proto calls it `raw`, not `bytes`."""
        entry = corpus.by_id('CLIENT-PARSE-006')
        payload = entry.test.steps[0].client_response.wire_payload
        files = list(_find_key(payload, 'file'))
        assert files, 'expected an inline file part in the golden payload'
        assert any('raw' in f for f in files)
        assert not any('bytes' in f for f in files)


class TestKnownDivergencesStillPresent:
    """PROVENANCE §C — shapes left uncorrected on purpose.

    Pinned so that "we decided not to touch this" stays a decision on the
    record rather than something a later reader assumes was an oversight.
    """

    def test_five_error_assertions_do_not_name_an_error_type(self, corpus):
        unconstrained = [
            (entry.id, step.id)
            for entry in corpus for step in entry.test.steps
            if step.expect_error is not None and step.expect_error.error_type is None
        ]
        assert sorted(unconstrained) == [
            ('CORE-CTX-001', 'send'),
            ('CORE-ERR-009', 'get-missing'),
            ('CORE-MULTI-003', 'mismatch'),
            ('SEC-AUTH-003', 'get-missing-task'),
            ('SEC-AUTH-003', 'get-other-user-task'),
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
