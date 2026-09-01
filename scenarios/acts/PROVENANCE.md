# ACTS corpus provenance

The `*.acts.yaml` files in this directory are a **mirror** of
`tests/acts/*.acts.yaml` from
[a2aproject/A2A#1882](https://github.com/a2aproject/A2A/pull/1882), pinned to:

    a2aproject/A2A @ dbcabfba4f01cc162c965f0fde5f44fc1f1e70e5
    (branch: conformance-spec, head of pull/1882 at copy time)

Nothing here is edited. Not to fix a typo, not to fix a defect, not to make a
test load. `tests/test_acts_corpus.py` pins a SHA-256 of every file, so an edit
fails the build rather than passing unnoticed.

## Refreshing

1. Re-copy `tests/acts/*.acts.yaml` from a newer snapshot of PR #1882 (or from
   the merged version once it lands). Copy, do not merge.
2. Update the SHA above and regenerate `UPSTREAM_DIGESTS` in
   `tests/test_acts_corpus.py` — the command is in the comment above it.
3. Run the tests. They pin the shape of the corpus and the exact number of
   sites each compat rule fires on, so anything that moved shows up as a
   named failure rather than as drift.

A compat rule whose site count drops to zero means upstream fixed that defect.
**Delete the rule** — do not update the number to zero. Likewise, a defect
listed in §B that has been fixed should be struck from this file, not left
as a stale warning.

## Known defects

### A. Load-blocking, handled by `test_suite/acts/compat.py`

Twenty-six of the 111 tests violate the ACTS CDDL and cannot be validated as
written. `compat.py` rewrites them on the way in — mechanically, without
inventing any value — so the corpus stays verbatim on disk while remaining
runnable. Load with `compat=False` to see the raw state.

| Rule | Defect | Sites | Upstream thread |
|------|--------|-------|-----------------|
| `expect-error-code-key` | `expect_error` keys the abstract error name as `code`; the CDDL calls it `error_type`. All 13 sites hold an error *name*, never a numeric code. | 13 | [r3305157201](https://github.com/a2aproject/A2A/pull/1882#discussion_r3305157201) |
| `push-config-operation-name` | `operation:` uses `set_push_notification_config` and friends, absent from the spec's own `abstract-operation` enum. | 18 | [r3305157185](https://github.com/a2aproject/A2A/pull/1882#discussion_r3305157185) |
| `expect-field-outside-body` | A response field (`task`) placed directly under `expect`, which admits only `status` and `body`. | 2 | — |
| `expect-error-as-expect-field` | `expect: {error: {…}}` used to assert a failure, which belongs in `expect_error`. | 2 | [r3305157220](https://github.com/a2aproject/A2A/pull/1882#discussion_r3305157220), [r3305157235](https://github.com/a2aproject/A2A/pull/1882#discussion_r3305157235) |

Without compat: 85 tests load and 26 fail, taking with them **all** push-config
coverage (`PUSH-*`, 10 tests), all capability-error coverage (`CORE-CAP-*`), and
the only gRPC error-status test.

### B. Not load-blocking, deliberately left broken

These parse fine and will simply produce wrong results at run time. They are
upstream's to fix; compensating for them in our code would be us deciding what
a conformance test *meant*, which is the failure mode this file exists to
prevent.

| Test | Defect |
|------|--------|
| `REST-PD-001` | Asserts an RFC 7807 problem-details body (`type`/`title`/`status`). A2A §11.6 mandates the `google.rpc.Status` shape (`error.code`, `error.status`, `error.message`, `error.details[]`) instead. Fails against every conformant implementation, including the reference SDK. |
| `CLIENT-PARSE-006` | Inline file part uses `file.bytes`; the spec and the `Part` proto both call the base64 field `file.raw`. |
| `GRPC-STREAM-002` | Two steps assert `expect.status: 200` on a `transport: [grpc]` test. gRPC has no HTTP status, so both are silent no-ops. |
| `CARD-CACHE-001`, `REST-CT-001`, `JSONRPC-CT-001` | Named for a header check (`Cache-Control`/`ETag`, `application/a2a+json`, `application/json`) but assert something unrelated, because the CDDL's `expect-block` has no `headers` key. They pass today without testing their stated requirement. `JSONRPC-CT-001` says so in its own description. |
| 23 tests tagged `runner-special` | The real verification lives in prose in `description`. Meanwhile `runner_requirements`, the spec field designed for exactly this, is used zero times across all 111 tests. |

### C. Shapes that are legal, or arguably so, and left alone

Not defects to route around — the schema accommodates them deliberately.

- **`expect_error` with no `error_type`** (5 sites: `SEC-AUTH-003` ×2,
  `CORE-ERR-009`, `CORE-MULTI-003`, `CORE-CTX-001`). The CDDL makes `error_type`
  required, but these carry only `message: {type: string}`, which reads clearly
  as "some A2A error, don't constrain which". `ExpectError.error_type` is
  optional for exactly this case — and the `expect-error-as-expect-field` compat
  rule produces the same shape.
- **`error_type` holding an assertion** rather than a bare name (`CORE-ERR-002`:
  `one_of: [TaskNotFoundError, TaskNotCancelableError]`). Useful, and accepted.
- **`get_agent_card` with `params: {extended: true}`** instead of the spec's
  separate `get_extended_agent_card` operation. Both are legal operations; which
  the corpus uses is a dispatch question, not a defect.
- **`expect_stream` on a raw step** (`JSONRPC-SSE-001`). The CDDL's `raw-step`
  production omits it, but raw SSE framing is a legitimate thing to test, so the
  grammar looks incomplete rather than the test wrong. The schema permits it.
