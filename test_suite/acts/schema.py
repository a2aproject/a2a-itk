"""ACTS document models, from the CDDL grammar in the spec's Appendix A.

Mirrors [A2A#1882](https://github.com/a2aproject/A2A/pull/1882) — see
``scenarios/acts/PROVENANCE.md`` for the pinned snapshot and the corrections
applied to the corpus locally.

**What this validates, and what it deliberately does not.** The document
envelope, the suite and test envelopes, and the *shape* of every step are
checked here. Assertion trees are not: ``expect.body``, ``expect_parsed``,
``params``, ``wire_payload``, ``final_event`` and friends stay as plain data.

That is not laziness. Spec §5.2 makes ``expect.body`` a nested map mirroring
the response, so ``{status: {state: FOO}}`` is two levels of path followed by
an exact match, while ``{type: array, count_gte: 1}`` is two operators on one
value — and *nothing in the syntax distinguishes them*. Only the evaluator,
walking a real response, can tell which a given map is. Modelling assertions
as pydantic types here would either reject valid documents or invent a
distinction the format does not have. Story 4.3 owns that evaluation.

So: this module rejects a document no runner could execute, and passes
everything else through intact.
"""

from __future__ import annotations

import enum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


ACTS_VERSION = '1.0'

# A step's ``id`` is what ``{{step.var}}`` substitution resolves against, so
# these are identifiers, not prose.
StepId = Annotated[str, Field(min_length=1)]


class Level(str, enum.Enum):
    """RFC 2119 conformance level, per test.

    Drives the report's ``by_level`` breakdown (spec §13.2) and, downstream,
    whether a failure blocks: a ``MUST`` failure means non-conformant, a
    ``SHOULD`` failure is a warning, a ``MAY`` failure is informational.
    """

    MUST = 'must'
    SHOULD = 'should'
    MAY = 'may'


class TransportBinding(str, enum.Enum):
    """Which wire binding a test is restricted to.

    Deliberately **not** :class:`test_suite.transports.Transport`. The two
    vocabularies disagree: ACTS says ``rest`` where the traversal engine says
    ``http_json``. Sharing one enum would silently mistranslate whichever
    suite lost the coin toss, so they stay separate and story 4.2's
    ``wire_map`` owns the correspondence.
    """

    JSONRPC = 'jsonrpc'
    GRPC = 'grpc'
    REST = 'rest'


class Operation(str, enum.Enum):
    """Abstract, transport-agnostic operation (spec §4.1).

    A step names one of these; the dispatcher binds it to a concrete method,
    path or RPC. Tests never name wire methods, which is what lets one corpus
    run against all three bindings.
    """

    SEND_MESSAGE = 'send_message'
    SEND_STREAMING_MESSAGE = 'send_streaming_message'
    GET_TASK = 'get_task'
    LIST_TASKS = 'list_tasks'
    CANCEL_TASK = 'cancel_task'
    SUBSCRIBE_TO_TASK = 'subscribe_to_task'
    GET_AGENT_CARD = 'get_agent_card'
    GET_EXTENDED_AGENT_CARD = 'get_extended_agent_card'
    CREATE_PUSH_CONFIG = 'create_push_config'
    GET_PUSH_CONFIG = 'get_push_config'
    LIST_PUSH_CONFIGS = 'list_push_configs'
    DELETE_PUSH_CONFIG = 'delete_push_config'


class ErrorType(str, enum.Enum):
    """Abstract A2A error name (spec §6.2).

    Abstract for the same reason operations are: ``TaskNotFoundError`` is
    JSON-RPC ``-32001``, HTTP 404 and gRPC ``NOT_FOUND`` depending on the
    binding, and a test should not have to care.
    """

    TASK_NOT_FOUND = 'TaskNotFoundError'
    TASK_NOT_CANCELABLE = 'TaskNotCancelableError'
    UNSUPPORTED_OPERATION = 'UnsupportedOperationError'
    CONTENT_TYPE_NOT_SUPPORTED = 'ContentTypeNotSupportedError'
    INVALID_PARAMS = 'InvalidParamsError'
    VERSION_NOT_SUPPORTED = 'VersionNotSupportedError'
    PUSH_NOTIFICATION_NOT_SUPPORTED = 'PushNotificationNotSupportedError'
    STREAMING_NOT_SUPPORTED = 'StreamingNotSupportedError'
    EXTENSION_SUPPORT_REQUIRED = 'ExtensionSupportRequiredError'
    EXTENDED_CARD_NOT_SUPPORTED = 'ExtendedCardNotSupportedError'
    JSON_PARSE = 'JSONParseError'
    METHOD_NOT_FOUND = 'MethodNotFoundError'
    INTERNAL = 'InternalError'


ERROR_TYPE_NAMES: frozenset[str] = frozenset(e.value for e in ErrorType)


class RunnerRequirement(str, enum.Enum):
    """A capability the *runner* must have, beyond speaking the protocol.

    Lets a runner skip honestly ("no webhook endpoint configured") instead of
    reporting a failure it caused itself.
    """

    WEBHOOK_ENDPOINT = 'webhook_endpoint'
    CONCURRENT_STREAMS = 'concurrent_streams'
    STREAM_DISCONNECT = 'stream_disconnect'
    AUTH_CREDENTIALS = 'auth_credentials'
    HEADER_INSPECTION = 'header_inspection'


class HttpMethod(str, enum.Enum):
    """HTTP method for a raw step."""

    GET = 'GET'
    POST = 'POST'
    PUT = 'PUT'
    DELETE = 'DELETE'


class Ordering(str, enum.Enum):
    """Ordering constraint over a stream's events (spec §7.1)."""

    MONOTONIC_STATE = 'monotonic_state'


class Backoff(str, enum.Enum):
    """Delay growth between polling attempts."""

    NONE = 'none'
    LINEAR = 'linear'
    EXPONENTIAL = 'exponential'


class EventMatch(str, enum.Enum):
    """Whether a streamed-event assertion is positional."""

    EXACT_POSITION = 'exact_position'
    ANY_POSITION = 'any_position'


# An assertion subtree. Opaque here — see the module docstring.
Assertion = Any


class _Model(BaseModel):
    """Base for every ACTS model.

    ``extra='forbid'`` throughout: an unknown key is nearly always a typo or
    a stale field name, and silently ignoring it drops the assertion the
    author meant to make. That is the "no test silently dropped" requirement
    from the Phase 2 validator brief, applied here.
    """

    model_config = ConfigDict(extra='forbid')


class Metadata(_Model):
    """Human-facing description of a document. Carries no execution meaning."""

    title: str | None = None
    description: str | None = None
    authors: list[str] | None = None
    license: str | None = None


class Preconditions(_Model):
    """What the SUT must advertise for a test to be applicable (spec §3.3).

    Evaluated against the agent card before the test runs. Unmet means
    *skip*, not fail — a server that does not claim push notifications is not
    non-conformant for lacking them.
    """

    capabilities: dict[str, Any] | None = None
    skills: list[dict[str, Any]] | None = None
    transport: list[TransportBinding] | None = None
    extensions: list[str] | None = None
    description: str | None = None


class CollectionMatch(_Model):
    """A wildcard path plus the assertions each matched element must satisfy.

    The escape hatch from `expect`'s positional nesting: `expect` can only say
    "artifact[0]", while `path: task.artifacts[*].parts[*]` says "some part,
    anywhere".
    """

    path: str
    match: dict[str, Assertion]


class NamedAssertion(_Model):
    """An assertion evaluated against a captured value rather than the
    response of the step it is attached to.

    ``source`` is what makes it cross-step — typically ``{{step.response}}``.
    Exactly one of ``match``/``any``/``all``/``none`` gives the check.
    """

    id: str | None = None
    description: str | None = None
    source: str
    path: str | None = None
    match: Assertion = None
    any: CollectionMatch | None = None
    all: CollectionMatch | None = None
    none: CollectionMatch | None = None

    @model_validator(mode='after')
    def _needs_exactly_one_check(self) -> NamedAssertion:
        given = [
            n for n in ('match', 'any', 'all', 'none')
            if getattr(self, n) is not None
        ]
        if not given:
            raise ValueError(
                'needs one of `match`, `any`, `all` or `none`; '
                'without one it asserts nothing'
            )
        if len(given) > 1:
            raise ValueError(
                f'set only one of `match`, `any`, `all`, `none`; got {given}'
            )
        return self


class InlineAssertion(_Model):
    """The body of a standalone ``assertion`` step."""

    source: str
    any: CollectionMatch | None = None
    all: CollectionMatch | None = None
    none: CollectionMatch | None = None

    @model_validator(mode='after')
    def _needs_exactly_one_check(self) -> InlineAssertion:
        given = [n for n in ('any', 'all', 'none') if getattr(self, n) is not None]
        if not given:
            raise ValueError('needs one of `any`, `all` or `none`')
        if len(given) > 1:
            raise ValueError(
                f'set only one of `any`, `all`, `none`; got {given}'
            )
        return self


class RawBlock(_Model):
    """A hand-built HTTP request, bypassing the dispatcher.

    For the tests that assert something about the wire itself — a malformed
    JSON body, an unknown method, a bad ``A2A-Version`` header — which an
    abstract operation cannot express because a well-behaved client would
    never send it.
    """

    method: HttpMethod
    path: str
    headers: dict[str, str] | None = None
    body: Any = None
    body_raw: str | None = Field(
        default=None,
        description='Literal request body, sent unparsed. For payloads that '
                    'are not valid JSON and so cannot go in `body` — the '
                    'ParseError tests need exactly that.',
    )

    @model_validator(mode='after')
    def _body_or_body_raw(self) -> RawBlock:
        if self.body is not None and self.body_raw is not None:
            raise ValueError('set either `body` or `body_raw`, not both')
        return self


class ClientResponseBlock(_Model):
    """A canned wire payload fed to the SDK's own client (spec §10).

    Inverts the usual direction: nothing is sent, and the assertion is about
    what the *client* parsed. Catches the interop bugs where two SDKs
    disagree about a payload neither server ever emits.
    """

    operation: Operation
    wire_payload: Any


class Repeat(_Model):
    """Re-dispatch the step until ``until`` holds (spec §9).

    How a test waits for a long-running task without a fixed sleep.
    """

    until: str = Field(
        min_length=1,
        description='Expression over the latest response, e.g. '
                    '"status.state in [TASK_STATE_COMPLETED]". Evaluated by '
                    'the runner (story 4.3).',
    )
    max_attempts: int | None = Field(default=None, ge=1)
    delay_ms: int | None = Field(default=None, ge=0)
    backoff: Backoff | None = None


class ExpectBlock(_Model):
    """Assertions on a non-streaming response.

    Only ``status`` and ``body``. A response field placed directly here is a
    mistake the corpus made twice (see PROVENANCE.md §B) and ``extra='forbid'``
    is what catches it.
    """

    status: Assertion = None
    body: dict[str, Assertion] | None = None


class ExpectError(_Model):
    """Assert the operation failed, and optionally how."""

    error_type: Assertion = Field(
        default=None,
        description='Abstract error name, or an assertion over it. The CDDL '
                    'requires it; five corpus tests omit it and assert only a '
                    '`message`, which reads as "any A2A error". Optional here '
                    'for exactly that case — see PROVENANCE.md §C.',
    )
    message: Assertion = None
    data: Assertion = None
    details: dict[str, Assertion] | None = None

    @model_validator(mode='after')
    def _known_error_name(self) -> ExpectError:
        """Catch a misspelled error name, without rejecting an assertion.

        A bare string here should be one of the spec's names; anything else
        would never match and the test would fail for the wrong reason. A
        mapping is an assertion (``one_of: [...]``) and is left to the
        evaluator.
        """
        if isinstance(self.error_type, str) and self.error_type not in ERROR_TYPE_NAMES:
            raise ValueError(
                f'unknown error_type {self.error_type!r}; '
                f'expected one of {sorted(ERROR_TYPE_NAMES)} '
                f'or an assertion object'
            )
        return self

    def literal_error_type(self) -> ErrorType | None:
        """The error name when it is a plain one, else ``None``.

        Lets the dispatcher map a single expected error to a wire code
        without re-deriving whether ``error_type`` is a name or an assertion.
        """
        if isinstance(self.error_type, str):
            return ErrorType(self.error_type)
        return None


class EventAssertion(_Model):
    """One expected event within a stream.

    Open-ended on purpose: the payload keys (``status_update``, ``artifact``,
    ``task``, …) are assertion subtrees like any other body, so they are
    accepted as extras rather than enumerated.
    """

    model_config = ConfigDict(extra='allow')

    description: str | None = None
    match: EventMatch | None = None
    index: int | None = Field(default=None, ge=0)


class ExpectStream(_Model):
    """Assertions over a stream of events (spec §7)."""

    min_count: int | None = Field(default=None, ge=0)
    max_count: int | None = Field(default=None, ge=0)
    timeout_ms: int | None = Field(default=None, ge=0)
    ordering: Ordering | None = None
    events: list[EventAssertion] | None = None
    final_event: dict[str, Assertion] | None = None
    each_event: dict[str, Assertion] | None = None

    @model_validator(mode='after')
    def _counts_are_consistent(self) -> ExpectStream:
        if (
            self.min_count is not None
            and self.max_count is not None
            and self.min_count > self.max_count
        ):
            raise ValueError(
                f'min_count ({self.min_count}) exceeds '
                f'max_count ({self.max_count}); no stream can satisfy both'
            )
        return self


class StepKind(str, enum.Enum):
    """Which of the four step forms a step is (spec §4)."""

    OPERATION = 'operation'
    RAW = 'raw'
    CLIENT = 'client'
    ASSERTION = 'assertion'


class Step(_Model):
    """One step of a test.

    The CDDL writes ``step`` as a union of four disjoint records. This is one
    model with a validator instead, because a pydantic union reports a failure
    as four parallel "did not match" branches — for a step with a typo, the
    real error is buried in whichever branch was closest. One model can say
    "a raw step cannot carry `params`" directly.

    Use :meth:`kind` rather than testing fields, so the runner's dispatch
    stays in one place.
    """

    id: StepId
    description: str | None = None

    # -- the four kinds; exactly one must be present ---------------------
    operation: Operation | None = None
    raw: RawBlock | None = None
    client_response: ClientResponseBlock | None = None
    assertion: InlineAssertion | None = None

    # -- operation-step payload -----------------------------------------
    params: dict[str, Any] | None = None

    # -- outcome assertions ----------------------------------------------
    expect: ExpectBlock | None = None
    expect_error: ExpectError | None = None
    expect_stream: ExpectStream | None = None
    expect_parsed: dict[str, Assertion] | None = None

    # -- extras ------------------------------------------------------------
    capture: dict[str, str] | None = Field(
        default=None,
        description='Variable name -> dot-path into this step\'s response. '
                    'Later steps read it as {{<step id>.<name>}}.',
    )
    assertions: list[NamedAssertion] | None = None
    repeat: Repeat | None = None
    delay_ms: int | None = Field(default=None, ge=0)

    def kind(self) -> StepKind:
        """Which form this step takes. Total, given validation passed."""
        if self.operation is not None:
            return StepKind.OPERATION
        if self.raw is not None:
            return StepKind.RAW
        if self.client_response is not None:
            return StepKind.CLIENT
        return StepKind.ASSERTION

    @model_validator(mode='after')
    def _exactly_one_kind(self) -> Step:
        present = [
            n for n in ('operation', 'raw', 'client_response', 'assertion')
            if getattr(self, n) is not None
        ]
        if not present:
            raise ValueError(
                'a step needs one of `operation`, `raw`, `client_response` '
                'or `assertion`'
            )
        if len(present) > 1:
            raise ValueError(
                f'a step has exactly one kind; got {present}'
            )
        return self

    @model_validator(mode='after')
    def _fields_match_the_kind(self) -> Step:
        """Reject fields the step's kind cannot act on.

        Left unchecked these read as working assertions and are simply never
        evaluated — a test that passes while testing nothing, which is worse
        than one that fails.
        """
        kind = self.kind()

        if kind is not StepKind.OPERATION and self.params is not None:
            raise ValueError(f'`params` belongs to an operation step, not a {kind.value} step')

        if kind is not StepKind.CLIENT and self.expect_parsed is not None:
            raise ValueError(
                '`expect_parsed` belongs to a client_response step; use '
                '`expect` for a response from the SUT'
            )
        if kind is StepKind.CLIENT:
            if self.expect_parsed is None:
                raise ValueError(
                    'a client_response step needs `expect_parsed` — without it '
                    'the payload is parsed and nothing is asserted'
                )
            for name in ('expect', 'expect_error', 'expect_stream', 'capture', 'repeat'):
                if getattr(self, name) is not None:
                    raise ValueError(
                        f'`{name}` belongs to a step that talks to the SUT; a '
                        f'client_response step sends nothing'
                    )

        if kind is StepKind.ASSERTION:
            for name in ('expect', 'expect_error', 'expect_stream', 'capture', 'repeat'):
                if getattr(self, name) is not None:
                    raise ValueError(
                        f'`{name}` belongs to a step that talks to the SUT; an '
                        f'assertion step only re-checks captured values'
                    )

        # `expect_error` on a raw step is explicitly allowed (spec §4.4) — the
        # runner maps the wire error back to an abstract name — so it is not
        # checked here.

        if (
            self.expect is not None
            and self.expect.body is not None
            and self.expect_error is not None
        ):
            # `expect.status` alongside `expect_error` is coherent (a JSON-RPC
            # error rides an HTTP 200), but a success *body* and a failure are
            # not both assertable about one call.
            raise ValueError(
                'set either `expect.body` or `expect_error`, not both — a call '
                'cannot both return a body and fail'
            )
        if self.expect_error is not None and self.expect_stream is not None:
            raise ValueError('set either `expect_error` or `expect_stream`, not both')

        if self.repeat is not None and kind is not StepKind.OPERATION:
            raise ValueError(f'`repeat` re-dispatches an operation; not valid on a {kind.value} step')

        return self


class Test(_Model):
    """One conformance test: a precondition set and an ordered list of steps.

    Steps within a test share a scope — later ones read earlier captures — so
    a test is the isolation unit. The runner gives each a fresh scope.
    """

    # Named for the domain, not for pytest — which would otherwise try to
    # collect it as a test class wherever it is imported.
    __test__ = False

    id: str = Field(min_length=1)
    name: str
    description: str | None = None
    spec_ref: str | None = Field(
        default=None,
        description='Where in the A2A spec the requirement lives. What makes '
                    'a conformance report traceable rather than a bare tally.',
    )
    level: Level
    tags: list[str] | None = None
    transport: list[TransportBinding] | None = Field(
        default=None,
        description='Restrict to these bindings. Set when the test asserts '
                    'something binding-specific (a JSON-RPC envelope, an HTTP '
                    'status); omit for anything transport-agnostic.',
    )
    preconditions: Preconditions | None = None
    requires_behaviors: list[str] | None = Field(
        default=None,
        description='`tck-*` prefixes the SUT must implement. An SDK that '
                    'lacks one FAILS the test rather than skipping it — the '
                    'point is that lagging support stays visible (story 4.5).',
    )
    origin: str | None = None
    runner_requirements: list[RunnerRequirement] | None = None
    steps: list[Step] = Field(min_length=1)
    assertions: list[NamedAssertion] | None = Field(
        default=None,
        description='Evaluated once, after every step. For cross-step checks '
                    'that belong to no single step.',
    )

    @model_validator(mode='after')
    def _step_ids_are_unique(self) -> Test:
        """Duplicate step ids make ``{{id.var}}`` ambiguous.

        Which of two same-named steps a capture refers to would be decided by
        iteration order — invisible in the file, and silently wrong.
        """
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(
                    f'duplicate step id {step.id!r}; capture references like '
                    f'{{{{{step.id}.x}}}} would be ambiguous'
                )
            seen.add(step.id)
        return self

    @model_validator(mode='after')
    def _raw_only_tests_name_a_transport(self) -> Test:
        """Spec §4.4: a test of only raw steps MUST set ``transport``.

        A raw step hard-codes one binding's method, path and body. Without the
        filter the test also runs against the other two, where it fails for a
        reason that has nothing to do with conformance.
        """
        if not self.steps or self.transport:
            return self
        if all(step.kind() is StepKind.RAW for step in self.steps):
            raise ValueError(
                'a test whose steps are all raw must declare `transport`; raw '
                'requests are binding-specific and would fail against the others'
            )
        return self

    def behaviors(self) -> frozenset[str]:
        """The `tck-*` prefixes this test needs from the SUT."""
        return frozenset(self.requires_behaviors or ())

    def applies_to(self, transport: TransportBinding) -> bool:
        """Should this test run against ``transport``?

        An unrestricted test runs against every binding — that is the whole
        value of abstract operations.
        """
        return not self.transport or transport in self.transport


class Suite(_Model):
    """A named group of tests. The unit the report is grouped by."""

    id: str = Field(min_length=1)
    name: str
    description: str | None = None
    tags: list[str] | None = None
    tests: list[Test] = Field(min_length=1)

    @model_validator(mode='after')
    def _test_ids_are_unique(self) -> Suite:
        seen: set[str] = set()
        for test in self.tests:
            if test.id in seen:
                raise ValueError(f'duplicate test id {test.id!r} in suite {self.id!r}')
            seen.add(test.id)
        return self


class ActsDocument(_Model):
    """One ``*.acts.yaml`` file.

    Two roles share this shape: a *manifest* (``include:``, listing other
    files — ``suite.acts.yaml``) and a *suite file* (``suites:``). A file may
    do both; it must do at least one, or it declares nothing.
    """

    acts_version: str
    spec_version: str
    spec_ref: str | None = None
    metadata: Metadata | None = None
    variables: dict[str, str] | None = Field(
        default=None,
        description='Document-scoped `{{name}}` substitutions. Values are '
                    'strings; a runner may inject its own (SUT_BASE_URL and '
                    'the like) over the top.',
    )
    include: list[str] | None = Field(
        default=None,
        description='Sibling files to pull in, resolved relative to this '
                    "file's directory.",
    )
    suites: list[Suite] | None = None

    @model_validator(mode='after')
    def _declares_something(self) -> ActsDocument:
        if not self.include and not self.suites:
            raise ValueError(
                'needs `include` or `suites`; a document with neither '
                'contributes no tests'
            )
        return self

    @model_validator(mode='after')
    def _suite_ids_are_unique(self) -> ActsDocument:
        seen: set[str] = set()
        for suite in self.suites or ():
            if suite.id in seen:
                raise ValueError(f'duplicate suite id {suite.id!r}')
            seen.add(suite.id)
        return self


def is_acts_document(raw: object) -> bool:
    """Does this parsed mapping look like an ACTS document?

    ``acts_version`` is the discriminator, mirroring how a traversal scenario
    is identified by its ``schema:`` key. Lets one directory hold both kinds
    without either loader guessing.
    """
    return isinstance(raw, dict) and 'acts_version' in raw


__all__ = [
    'ACTS_VERSION',
    'ERROR_TYPE_NAMES',
    'ActsDocument',
    'Assertion',
    'Backoff',
    'ClientResponseBlock',
    'CollectionMatch',
    'ErrorType',
    'EventAssertion',
    'EventMatch',
    'ExpectBlock',
    'ExpectError',
    'ExpectStream',
    'HttpMethod',
    'InlineAssertion',
    'Level',
    'Metadata',
    'NamedAssertion',
    'Operation',
    'Ordering',
    'Preconditions',
    'RawBlock',
    'Repeat',
    'RunnerRequirement',
    'Step',
    'StepKind',
    'Suite',
    'Test',
    'TransportBinding',
    'is_acts_document',
]
