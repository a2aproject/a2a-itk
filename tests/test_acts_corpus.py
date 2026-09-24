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

import json

from collections.abc import Mapping
from pathlib import Path

import pytest

from test_suite.acts import (
    Level,
    Operation,
    StepKind,
    TransportBinding,
    load_suite,
)
from acts_runner import RUNNER_CAPABILITIES, NoApplicableTests, _in_scope, _subset
from test_suite.acts.runner import KNOWN_CAPABILITIES
from test_suite.acts.schema import RunnerRequirement


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
        # `webhookUrl` is runner-provided now (§12.2): the URL the runner
        # listens on is not something a document can state.
        assert corpus.variables == {'baseUrl': '{{env.SUT_BASE_URL}}'}


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
            # 142 before the vacuity audit added six: the missing half of
            # CORE-ERR-009, a positive control on each SEC-EXTCARD test, and
            # two on REST-STATUS-001. Three more when the streaming tests were
            # given the long-running task their concurrency needs a subject.
            StepKind.OPERATION: 151,
            StepKind.RAW: 22,
            StepKind.CLIENT: 9,
            # The five push tests that used to register a config and assert
            # nothing about what the SUT then delivered.
            StepKind.WEBHOOK: 5,
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

    def test_the_bindings_between_them_cover_the_whole_corpus(self, corpus):
        """101 + 88 + 92 overlaps, but nothing falls through the gaps: every
        test targets at least one binding, so scoping loses none of them."""
        covered = {
            entry.id
            for binding in TransportBinding
            for entry in corpus.for_transport(binding)
        }
        assert covered == {entry.id for entry in corpus}

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
            'webhookUrl',
        ]


class TestWhatEachBindingIsScoredOn:
    """`acts_runner._in_scope` — what a report for one binding contains.

    A report covers one binding (§13.1), so a test declaring `transport:` for
    a different one is outside that report rather than skipped inside it.
    Getting this wrong is not cosmetic: it put twenty-three phantom skips in
    the gRPC report and scored a clean run 88/111.
    """

    def test_a_binding_is_scored_only_on_the_tests_that_target_it(self, corpus):
        assert [
            len(_in_scope(corpus, binding).tests) for binding in TransportBinding
        ] == [101, 88, 92]

    def test_an_out_of_scope_test_is_absent_rather_than_skipped(self, corpus):
        ids = {entry.id for entry in _in_scope(corpus, TransportBinding.JSONRPC)}
        assert 'GRPC-STATUS-001' not in ids
        assert 'JSONRPC-ERR-002' in ids

    def test_scoping_keeps_the_variables_the_run_resolves_against(self, corpus):
        """Dropping these would leave every `{{...}}` in the corpus unresolved
        — a run-wide error that reads as the SUT's fault."""
        scoped = _in_scope(corpus, TransportBinding.REST)
        assert scoped.variables == corpus.variables
        assert scoped.sources == corpus.sources

    def test_a_selection_no_binding_can_run_is_refused(self, corpus):
        """`-t GRPC-STATUS-001 --transport all` asks two bindings to grade a
        test neither offers. Erroring beats a vacuous 0/0 CONFORMANT."""
        grpc_only = _subset(
            corpus, [entry for entry in corpus if entry.id == 'GRPC-STATUS-001']
        )
        with pytest.raises(NoApplicableTests, match='jsonrpc.*GRPC-STATUS-001'):
            _in_scope(grpc_only, TransportBinding.JSONRPC)


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
        some tests, so "declares the key" (81) is not "needs a behavior" (70).
        """
        # 69/80 before REST-STATUS-001 gained a positive control, which needs
        # a real task and so a behaviour to produce one.
        assert len([e for e in corpus if e.test.behaviors()]) == 70
        assert len([e for e in corpus if e.test.requires_behaviors is not None]) == 81


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

    def test_canned_agent_cards_carry_every_required_skill_field(self, corpus):
        """A §10 payload is what the SUT's own client has to parse, so a card
        the spec forbids tests the client's tolerance, not its correctness.

        `a2a.proto` marks all four of `id`, `name`, `description` and `tags`
        REQUIRED on `AgentSkill`, and a strict client rejects a card missing
        any of them before reaching the `capabilities` under test.
        """
        required = {'id', 'name', 'description', 'tags'}
        for entry in corpus:
            for step in entry.test.steps:
                if step.client_response is None:
                    continue
                payload = step.client_response.wire_payload
                if not isinstance(payload, Mapping):
                    continue
                for skill in payload.get('skills') or ():
                    missing = required - set(skill)
                    assert not missing, (
                        f'{entry.id}/{step.id}: skill {skill.get("id")!r} '
                        f'omits {sorted(missing)}, which a2a.proto marks '
                        f'REQUIRED on AgentSkill'
                    )


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
        """The spec field for "this test needs a runner capability".

        Without it a runner that cannot supply the fixture hits the
        unresolved-variable failure path, or runs something weaker, instead of
        skipping.
        """
        declared = {e.id for e in corpus if e.test.runner_requirements}
        assert declared == {
            # inspect a response header
            'CARD-CACHE-001', 'JSONRPC-CT-001', 'REST-CT-001',
            # name a §12.2 runner-provided variable
            'SEC-AUTH-002', 'SEC-AUTH-003', 'SEC-EXTCARD-002', 'CORE-ERR-009',
            # need a receiver the SUT can actually POST to
            'PUSH-DELIV-001', 'PUSH-DELIV-002', 'PUSH-DELIV-003',
            'SEC-PUSH-001', 'SEC-PUSH-002',
            # need two streams open at once, or one broken mid-flight
            'STREAM-MULTI-001', 'STREAM-MULTI-002', 'STREAM-RESUB-001',
        }
        for entry in corpus:
            for step in entry.test.steps:
                if step.expect is not None and step.expect.headers:
                    assert entry.test.runner_requirements, entry.id

    def test_every_runner_variable_reference_declares_its_requirement(
        self, corpus
    ):
        """§12.2: naming one of these without declaring `auth_credentials`
        turns a runner's missing fixture into an unresolved-variable error
        about the harness, where the spec wants an honest skip."""
        for entry in corpus:
            source = json.dumps(entry.test.model_dump(mode='json'))
            for variable in ('insufficientAuthToken', 'otherUserTaskId'):
                if f'{{{{{variable}}}}}' in source:
                    assert RunnerRequirement.AUTH_CREDENTIALS in (
                        entry.test.runner_requirements or ()
                    ), f'{entry.id} names {variable}'

    def test_the_harness_declares_every_capability_the_corpus_asks_for(
        self, corpus
    ):
        """Otherwise a gated test skips for a reason that is about us, not it.

        This is the check that was missing. `expect.headers` was wired into
        the runner and the corpus tagged those three tests
        `header_inspection`, but no front end ever declared the capability, so
        all three skipped on every nightly for a release while reading as an
        honest "this runner cannot do that".

        A new requirement in the corpus now has to be either met in
        `RUNNER_CAPABILITIES` or listed below as one we genuinely cannot
        arrange.
        """
        needed = {
            requirement
            for entry in corpus
            for requirement in entry.test.runner_requirements or ()
        }
        # `webhook_endpoint` is granted per run rather than statically, so it
        # is absent from `RUNNER_CAPABILITIES` without being unarrangeable.
        cannot_arrange = {
            RunnerRequirement.WEBHOOK_ENDPOINT,
        }
        # A requirement in both lists is a contradiction: `auth_credentials`
        # sat in `cannot_arrange` after the harness gained it, so declaring it
        # looked satisfied here while the tests went on skipping.
        both = set(RUNNER_CAPABILITIES) & cannot_arrange
        assert not both, (
            f'{sorted(r.value for r in both)} is declared in '
            f'RUNNER_CAPABILITIES and also listed as unarrangeable'
        )
        unmet = needed - set(RUNNER_CAPABILITIES) - cannot_arrange
        assert not unmet, (
            f'the corpus needs {sorted(r.value for r in unmet)}, which the '
            f'runner neither declares in RUNNER_CAPABILITIES nor admits it '
            f'cannot arrange'
        )

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
        """Two tests assert only that *some* error came back.

        Deliberate in both: the spec mandates a failure without mandating
        which error, and each still produces a genuine contrast because the
        same operation succeeds earlier in the test.

        Two have left this list. `SEC-AUTH-003` first — A2A requires an
        inaccessible task to be reported *not found*, so naming the error is
        the whole substance and leaving it unnamed let any two error strings
        pass a MUST. Then `CORE-ERR-009`, for the same reason: it asserts the
        indistinguishability of a missing and an unauthorized resource, which
        is a claim about *which* error, not about failure.
        """
        unconstrained = [
            (entry.id, step.id)
            for entry in corpus for step in entry.test.steps
            if step.expect_error is not None and step.expect_error.error_type is None
        ]
        assert sorted(unconstrained) == [
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
