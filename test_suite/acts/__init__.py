"""ACTS conformance suite: schema, loader, wire mapping and assertions.

ACTS ([A2A#1882](https://github.com/a2aproject/A2A/pull/1882)) declares
protocol-conformance tests as YAML: a sequence of abstract, transport-agnostic
operations against one SUT, plus assertions on what comes back. This package
is the ITK-side implementation: `schema.py` models the format, `compat.py`
reconciles the shipped corpus with it, `loader.py` turns a manifest into a
flat, ordered run plan, `wire_map.py` and `dispatcher/` bind an abstract
operation to a transport, `variables.py` and `assertions.py` decide what a
response means, and `runner.py` sequences the whole thing into results.

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

from test_suite.acts.assertions import (
    OPERATORS,
    AssertionResult,
    Failure,
    UntilError,
    evaluate,
    evaluate_body,
    evaluate_collection,
    evaluate_error,
    evaluate_named,
    evaluate_status,
    evaluate_until,
)
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
from test_suite.acts.runner import (
    FailureDetail,
    Outcome,
    RunError,
    Runner,
    StepResult,
    TestResult,
    is_conformant,
    summarize,
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
from test_suite.acts.variables import (
    MISSING,
    PathError,
    Scope,
    UnresolvedVariable,
    read_path,
    read_path_all,
    references,
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
    'AssertionResult',
    'Backoff',
    'binding_for_error',
    'binding_for_operation',
    'ClientResponseBlock',
    'CollectionMatch',
    'error_for_jsonrpc_code',
    'error_for_reason',
    'ERROR_TYPE_NAMES',
    'ErrorBinding',
    'ERRORS',
    'ErrorType',
    'evaluate',
    'evaluate_body',
    'evaluate_collection',
    'evaluate_error',
    'evaluate_named',
    'evaluate_status',
    'evaluate_until',
    'EventAssertion',
    'EventMatch',
    'ExpectBlock',
    'EXPECTED_SITES',
    'ExpectError',
    'ExpectStream',
    'Failure',
    'FailureDetail',
    'http_status_for_grpc',
    'HttpMethod',
    'InlineAssertion',
    'is_acts_document',
    'is_conformant',
    'Level',
    'load_document',
    'load_suite',
    'LoadedSuite',
    'LoadedTest',
    'LoadError',
    'Metadata',
    'MISSING',
    'NamedAssertion',
    'normalize_document',
    'Operation',
    'OperationBinding',
    'OPERATIONS',
    'OPERATORS',
    'Ordering',
    'Outcome',
    'parse_document',
    'PathError',
    'Preconditions',
    'PUSH_CONFIG_OPERATIONS',
    'RawBlock',
    'read_path',
    'read_path_all',
    'references',
    'render_validation_error',
    'Repeat',
    'Rewrite',
    'RunError',
    'Runner',
    'RunnerRequirement',
    'Scope',
    'site_counts',
    'Step',
    'StepKind',
    'StepResult',
    'Suite',
    'summarize',
    'Test',
    'TestResult',
    'TransportBinding',
    'UnresolvedVariable',
    'UntilError',
]
