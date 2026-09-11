"""The pinned ACTS corpus in ``scenarios/acts/`` loads, and stays as pinned.

Two things are being asserted: that the schema and loader handle the real
corpus rather than just fixtures, and that its measurable shape — counts,
levels, behaviours, per-binding applicability — does not drift unnoticed.

The corpus is loaded exactly as shipped. Nothing is rewritten on the way in,
so a load failure here is a defect to fix upstream.

A refresh should make these fail. That is the prompt to re-read
``PROVENANCE.md`` and re-derive the numbers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_suite.acts import (
    Level,
    Operation,
    StepKind,
    TransportBinding,
    load_suite,
)
from test_suite.acts.runner import KNOWN_CAPABILITIES


CORPUS = Path(__file__).resolve().parent.parent / 'scenarios' / 'acts'
MANIFEST = CORPUS / 'suite.acts.yaml'


@pytest.fixture(scope='module')
def corpus():
    """The whole corpus, exactly as shipped, loaded strictly.

    Strict on purpose: the corpus is fully valid against the schema, and the
    day it stops being so is the day we want to hear about it.
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
            'baseUrl': '{{env.SUT_BASE_URL}}',
            'webhookUrl': 'https://example.com/webhooks/a2a-tests',
        }


class TestCorpusShape:
    def test_every_test_has_at_least_one_step(self, corpus):
        assert all(entry.test.steps for entry in corpus)

    def test_no_test_gates_on_a_capability_a2a_does_not_define(self, corpus):
        """The assertion that would have caught the `authentication` defect.

        Five MUST/SHOULD tests gated on `capabilities.authentication` for the
        life of the corpus. Nothing failed — they simply skipped, on every
        binding against every SDK, and the reports read as though the auth
        requirements were covered.
        """
        offenders = [
            (entry.id, name)
            for entry in corpus
            for name in (
                (entry.test.preconditions.capabilities if entry.test.preconditions else None)
                or {}
            )
            if name not in KNOWN_CAPABILITIES
        ]
        assert offenders == []

    def test_every_step_has_a_resolvable_kind(self, corpus):
        counts = {k: 0 for k in StepKind}
        for entry in corpus:
            for step in entry.test.steps:
                counts[step.kind()] += 1
        assert counts == {
            StepKind.OPERATION: 142,
            StepKind.RAW: 22,
            StepKind.CLIENT: 9,
            StepKind.ASSERTION: 0,
        }

    def test_transport_restricted_tests_are_a_minority(self, corpus):
        """Most tests are transport-agnostic; that is what makes one corpus
        runnable against all three bindings."""
        restricted = [e for e in corpus if e.test.transport]
        assert len(restricted) == 26
        assert len(corpus.for_transport(TransportBinding.JSONRPC)) == 101
        assert len(corpus.for_transport(TransportBinding.GRPC)) == 88
        assert len(corpus.for_transport(TransportBinding.REST)) == 92

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
        some tests, so "declares the key" (80) is not "needs a behavior" (69).
        """
        assert len([e for e in corpus if e.test.behaviors()]) == 69
        assert len([e for e in corpus if e.test.requires_behaviors is not None]) == 80


class TestCorpusNeedsNoRewriting:
    """The corpus satisfies the schema as shipped.

    It did not always: twenty-six tests once violated the CDDL and were
    rewritten at load time. Those defects are fixed upstream, the rewriting
    is gone, and these assertions are what stop it coming back.
    """

    def test_strict_load_of_the_shipped_corpus_succeeds(self):
        assert len(load_suite(MANIFEST).tests) == 111

    def test_push_operations_use_the_abstract_enum_names(self, corpus):
        """The enum has no `*_push_notification_config` member, so a surviving
        one would be undispatchable."""
        used = {
            step.operation for entry in corpus for step in entry.test.steps
            if step.operation is not None
        }
        assert Operation.CREATE_PUSH_CONFIG in used
        assert all('push_notification' not in op.value for op in used)

    def test_failures_are_asserted_with_expect_error(self, corpus):
        """`expect: {error: ...}` is not a way to assert a failure."""
        for entry in corpus:
            for step in entry.test.steps:
                if step.expect is not None and step.expect.body:
                    assert 'error' not in step.expect.body or step.raw is not None, (
                        f'{entry.id}/{step.id}'
                    )

    def test_response_assertions_live_under_expect_body(self, corpus):
        for test_id in ('STREAM-SUB-001', 'STREAM-SUB-003'):
            first = corpus.by_id(test_id).test.steps[0]
            assert first.expect is not None
            assert 'task' in first.expect.body


class TestUpstreamFixesArePinned:
    """Defects that used to be recorded here as open, now closed upstream.

    Each of these was once a known-wrong shape this suite worked around or
    reported. They are pinned in their corrected form so a corpus refresh that
    regressed one would fail loudly rather than quietly reintroduce it.
    """

    def test_version_negotiation_uses_the_normative_jsonrpc_code(self, corpus):
        """`VER-NEG-001` asserts -32009, matching A2A §5.4.

        [r3305157228](https://github.com/a2aproject/A2A/pull/1882#discussion_r3305157228)
        asks for -32006, which is `InvalidAgentResponseError`. Pinned so
        nobody "corrects" this into being wrong.
        """
        entry = corpus.by_id('VER-NEG-001')
        assert entry.test.steps[0].expect.body['error']['code'] == -32009

    def test_inline_file_part_uses_the_flat_part_shape(self, corpus):
        """A2A 1.0's `Part` is flat: `raw`/`filename`/`mediaType`, with no
        nested `file` or `fileUrl` member and no `bytes` field."""
        entry = corpus.by_id('CLIENT-PARSE-006')
        payload = entry.test.steps[0].client_response.wire_payload
        assert not list(_find_key(payload, 'file'))
        assert not list(_find_key(payload, 'fileUrl'))
        parts = [
            part
            for artifact in payload['result']['artifacts']
            for part in artifact['parts']
        ]
        assert any(set(p) >= {'raw', 'filename', 'mediaType'} for p in parts)
        assert any(set(p) >= {'url', 'filename', 'mediaType'} for p in parts)

    def test_rest_errors_assert_the_google_rpc_status_shape(self, corpus):
        """A2A §11.6 mandates `google.rpc.Status`, not RFC 7807."""
        step = corpus.by_id('REST-PD-001').test.steps[0]
        assert set(step.expect.body) == {'error'}
        assert set(step.expect.body['error']) == {'code', 'message', 'details'}

    def test_runner_requirements_is_used_where_headers_are_asserted(self, corpus):
        """The spec field for "this test needs a runner capability", finally
        carrying the three tests that inspect response headers."""
        declared = {e.id for e in corpus if e.test.runner_requirements}
        assert declared == {'CARD-CACHE-001', 'JSONRPC-CT-001', 'REST-CT-001'}
        for entry in corpus:
            for step in entry.test.steps:
                if step.expect is not None and step.expect.headers:
                    assert entry.test.runner_requirements, entry.id

    def test_prose_only_tests_are_down_to_twenty(self, corpus):
        """`runner-special` marks a test whose real check is in its
        description. Three grew real assertions when `expect.headers` and a
        bimodal `any_of` arrived; the rest still need format work upstream."""
        special = [e for e in corpus if 'runner-special' in (e.test.tags or [])]
        assert len(special) == 20

    def test_extended_card_is_its_own_operation(self, corpus):
        """A2A §5.3 gives it a method of its own; it is not a flag on
        `get_agent_card`."""
        for test_id in ('CARD-EXT-001', 'SEC-EXTCARD-003'):
            step = corpus.by_id(test_id).test.steps[0]
            assert step.operation is Operation.GET_EXTENDED_AGENT_CARD
            assert step.params == {}


class TestKnownDivergencesStillPresent:
    """Shapes that are legal, or arguably so.

    Pinned so that "we decided not to touch this" stays a decision on the
    record rather than something a later reader assumes was an oversight.
    """

    def test_error_assertions_that_do_not_name_an_error_type(self, corpus):
        """Three tests assert only that *some* error came back.

        Deliberate in each case: the spec mandates a failure without mandating
        which error. `SEC-AUTH-003` used to be here and is not any longer —
        A2A requires an inaccessible task to be reported *not found*, so
        naming the error is the whole substance of the test and leaving it
        unnamed let any two error strings pass a MUST.
        """
        unconstrained = [
            (entry.id, step.id)
            for entry in corpus for step in entry.test.steps
            if step.expect_error is not None and step.expect_error.error_type is None
        ]
        assert sorted(unconstrained) == [
            ('CORE-ERR-009', 'get-missing'),
            ('CORE-MULTI-003', 'mismatch'),
            ('CORE-MULTI-006', 'turn2'),
        ]

    def test_every_named_error_type_is_a_literal(self, corpus):
        """No test needs an assertion object for `error_type` any more."""
        for entry in corpus:
            for step in entry.test.steps:
                if step.expect_error is None or step.expect_error.error_type is None:
                    continue
                assert step.expect_error.literal_error_type() is not None, (
                    f'{entry.id}/{step.id}'
                )


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
