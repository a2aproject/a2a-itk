# ACTS corpus provenance

The `*.acts.yaml` files in this directory are a copy of `tests/acts/*.acts.yaml`
from [a2aproject/A2A#1882](https://github.com/a2aproject/A2A/pull/1882), pinned to:

    a2aproject/A2A @ dbcabfba4f01cc162c965f0fde5f44fc1f1e70e5
    (branch: conformance-spec, head of pull/1882 at copy time)

with the local corrections listed below. Everything else is verbatim.

The spec itself (`docs/acts-specification.md`) is **not** copied — it stays in the
upstream A2A repo, and the CDDL grammar in its Appendix A is the authority for
`test_suite/acts/schema.py`. Upstream's `test-viewer.html` is also omitted; the
runner does not consume it.

## Refreshing

Re-copy from a newer snapshot of PR #1882 (or from the merged version once it
lands), update the SHA above, then re-apply whichever corrections below are still
outstanding upstream and drop the ones that were accepted. `tests/test_acts_corpus.py`
pins the resulting shape, so a refresh that changes the corpus fails loudly.

Do not hand-edit these files for any other reason. A local change that is not a
correction of an upstream defect is drift, and drift in the conformance corpus is
the exact failure mode ACTS exists to remove.

## Local corrections

### A. Unresolved `gemini-code-assist` review findings on PR #1882

All six were open and not outdated when applied (verified via the PR's review
threads). Each is applied **everywhere the defect occurs**, not only at the line
the bot annotated — the same mistakes repeat across the corpus and a partial fix
would leave it internally inconsistent.

| # | Defect | Fix | Sites | Upstream thread |
|---|--------|-----|-------|-----------------|
| 1 | `operation:` used push-notification-config names absent from the spec's `abstract-operation` enum | `set_push_notification_config`→`create_push_config`, `get_push_notification_config`→`get_push_config`, `list_push_notification_configs`→`list_push_configs`, `delete_push_notification_config`→`delete_push_config` | 18 | [r3305157185](https://github.com/a2aproject/A2A/pull/1882#discussion_r3305157185) |
| 2 | `expect_error:` keyed the error name as `code:`; the CDDL calls it `error_type:` | rename key to `error_type` | 13 | [r3305157201](https://github.com/a2aproject/A2A/pull/1882#discussion_r3305157201) |
| 3 | `file.bytes` for base64 content; spec and the `Part` proto both use `file.raw` | rename key to `raw` | 1 | [r3305157211](https://github.com/a2aproject/A2A/pull/1882#discussion_r3305157211) |
| 4 | `CORE-MULTI-006` asserted a failure with `expect: {status: error, error: {exists: true}}` — `error` is not a valid HTTP status, and neither key belongs directly under `expect` | replaced with `expect_error: {error_type: InvalidParamsError}` | 1 | [r3305157220](https://github.com/a2aproject/A2A/pull/1882#discussion_r3305157220) |
| 5 | `VER-NEG-001` expected JSON-RPC `-32009` for `VersionNotSupportedError`; the spec assigns `-32006` | `-32009` → `-32006` | 1 | [r3305157228](https://github.com/a2aproject/A2A/pull/1882#discussion_r3305157228) |
| 6 | `STREAM-SUB-003` asserted a failure with `expect: {error: {exists: true}}` | replaced with `expect_error: {error_type: UnsupportedOperationError}` | 1 | [r3305157235](https://github.com/a2aproject/A2A/pull/1882#discussion_r3305157235) |

A seventh finding,
[r3305157232](https://github.com/a2aproject/A2A/pull/1882#discussion_r3305157232),
is against `docs/acts-specification.md` §gRPC-to-HTTP status mapping and so
changes nothing here. It argues four mappings should follow the canonical gRPC
transcoding table rather than a more specific HTTP code:
`TaskNotCancelableError` 409→400, `UnsupportedOperationError` 405→501,
`VersionNotSupportedError` 406→501, `ContentTypeNotSupportedError` 415→400.
**It is unresolved, and it decides four rows of story 4.2's `wire_map.py`** —
settle it there rather than guessing.

### B. Same-class defects found locally

Not flagged by the bot, but the identical mistake as #4 and #6: a response-body
key placed directly under `expect:`, where the CDDL's `expect-block` admits only
`status` and `body`. Left alone, the runner would look for a top-level `task`
field on the response and always fail.

| Test | Step | Fix |
|------|------|-----|
| `STREAM-SUB-001` | `start` | `expect: {task: …}` → `expect: {body: {task: …}}` |
| `STREAM-SUB-003` | `setup` | `expect: {task: …}` → `expect: {body: {task: …}}` |

### C. Known-divergent shapes left as-is

Deliberately **not** corrected, because the intent is unambiguous and changing
them would be us editing test semantics rather than fixing a defect. The schema
accommodates them; see `test_suite/acts/schema.py`.

- **`expect_error` with no `error_type`** (5 sites: `SEC-AUTH-003` ×2,
  `CORE-ERR-009`, `CORE-MULTI-003`, `CORE-CTX-001`). The CDDL makes `error_type`
  required, but these carry only `message: {type: string}`, which reads clearly as
  "some A2A error, don't constrain which". The schema therefore treats
  `error_type` as optional with exactly that meaning.
- **`error_type` holding an assertion object** rather than a bare enum name
  (`CORE-ERR-002`: `one_of: [TaskNotFoundError, TaskNotCancelableError]`). Useful,
  and the schema accepts it.
- **`get_agent_card` with `params: {extended: true}`** instead of the spec's
  separate `get_extended_agent_card` operation. Both are legal spec operations;
  which one the corpus uses is a dispatch question for story 4.2, not a defect.
