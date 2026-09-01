"""ACTS conformance suite: schema and loader.

ACTS ([A2A#1882](https://github.com/a2aproject/A2A/pull/1882)) declares
protocol-conformance tests as YAML: a sequence of abstract, transport-agnostic
operations against one SUT, plus assertions on what comes back. This package
is the ITK-side implementation — story 4.1 lands the schema and the loader,
with the transport adapters, runner, `tck-*` behavior contract and report
writer following in 4.2-4.7.

It sits *beside* `test_suite.scenarios`, not inside it, because the two suites
answer different questions and share nothing at run time: a traversal walks an
N-agent Euler circuit to prove SDKs interoperate, while an ACTS run drives a
single mounted SUT to prove it matches the spec. Keeping them apart is a
standing decision (SCOPE "keep ACTS and traversal as distinct suites sharing
agents + config; don't force one model into the other").

The corpus itself lives in `scenarios/acts/`; see its `PROVENANCE.md` for the
pinned upstream snapshot and the corrections applied to it.
"""

from test_suite.acts.loader import (
    ActsFileError,
    LoadedSuite,
    LoadedTest,
    LoadError,
    load_document,
    load_suite,
    parse_document,
    render_validation_error,
)
from test_suite.acts.schema import (
    ACTS_VERSION,
    ERROR_TYPE_NAMES,
    ActsDocument,
    Backoff,
    ClientResponseBlock,
    CollectionMatch,
    ErrorType,
    EventAssertion,
    EventMatch,
    ExpectBlock,
    ExpectError,
    ExpectStream,
    HttpMethod,
    InlineAssertion,
    Level,
    Metadata,
    NamedAssertion,
    Operation,
    Ordering,
    Preconditions,
    RawBlock,
    Repeat,
    RunnerRequirement,
    Step,
    StepKind,
    Suite,
    Test,
    TransportBinding,
    is_acts_document,
)


__all__ = [
    'ACTS_VERSION',
    'ERROR_TYPE_NAMES',
    'ActsDocument',
    'ActsFileError',
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
    'LoadError',
    'LoadedSuite',
    'LoadedTest',
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
    'load_document',
    'load_suite',
    'parse_document',
    'render_validation_error',
]
