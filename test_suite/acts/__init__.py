"""ACTS conformance suite: schema, compat rules and loader.

ACTS ([A2A#1882](https://github.com/a2aproject/A2A/pull/1882)) declares
protocol-conformance tests as YAML: a sequence of abstract, transport-agnostic
operations against one SUT, plus assertions on what comes back. This package
is the ITK-side implementation: `schema.py` models the format, `compat.py`
reconciles the shipped corpus with it, and `loader.py` turns a manifest into a
flat, ordered run plan.

It sits *beside* `test_suite.scenarios`, not inside it, because the two suites
answer different questions and share nothing at run time: a traversal walks an
N-agent Euler circuit to prove SDKs interoperate, while an ACTS run drives a
single mounted SUT to prove it matches the spec. Keeping them apart is a
standing decision (SCOPE "keep ACTS and traversal as distinct suites sharing
agents + config; don't force one model into the other").

The corpus itself lives in `scenarios/acts/`, as a byte-identical mirror of the
upstream snapshot named in its `PROVENANCE.md`. Twenty-six of its tests violate
the ACTS CDDL; `compat.py` rewrites those defects at load time rather than
editing the YAML, so the mirror stays refreshable. `compat=False` on any loader
entry point shows the corpus exactly as shipped.
"""

from test_suite.acts.compat import (
    EXPECTED_SITES,
    PUSH_CONFIG_OPERATIONS,
    Rewrite,
    normalize_document,
    site_counts,
)
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
from test_suite.acts.wire_map import (
    ERRORS,
    OPERATIONS,
    ErrorBinding,
    OperationBinding,
    binding_for_error,
    binding_for_operation,
    error_for_jsonrpc_code,
    error_for_reason,
    http_status_for_grpc,
)


__all__ = [
    'ACTS_VERSION',
    'ActsDocument',
    'ActsFileError',
    'Backoff',
    'ClientResponseBlock',
    'CollectionMatch',
    'ERRORS',
    'ERROR_TYPE_NAMES',
    'EXPECTED_SITES',
    'ErrorBinding',
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
    'OPERATIONS',
    'Operation',
    'OperationBinding',
    'Ordering',
    'PUSH_CONFIG_OPERATIONS',
    'Preconditions',
    'RawBlock',
    'Repeat',
    'Rewrite',
    'RunnerRequirement',
    'Step',
    'StepKind',
    'Suite',
    'Test',
    'TransportBinding',
    'binding_for_error',
    'binding_for_operation',
    'error_for_jsonrpc_code',
    'error_for_reason',
    'http_status_for_grpc',
    'is_acts_document',
    'load_document',
    'load_suite',
    'normalize_document',
    'parse_document',
    'render_validation_error',
    'site_counts',
]
