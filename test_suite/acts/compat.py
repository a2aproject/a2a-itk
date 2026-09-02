"""Rewrite known upstream defects in the ACTS corpus, at load time.

``scenarios/acts/`` is a **byte-identical mirror** of the corpus on
[A2A#1882](https://github.com/a2aproject/A2A/pull/1882). Twenty-six of its 111
tests violate the ACTS CDDL and cannot be validated as written. Rather than
edit the YAML — which forks the corpus and makes every future refresh a
three-way merge — the corrections live here, as a small table of mechanical
rewrites applied to the parsed mapping before it reaches
:mod:`test_suite.acts.schema`.

That split is the point. ``schema.py`` stays a faithful statement of what the
CDDL permits, so it can still tell a valid ACTS document from an invalid one.
This module states, separately and reviewably, "and here is where the shipped
corpus disagrees with its own grammar".

**Every rule is mechanical.** Each is a rename or a move that the surrounding
document already determines; none invents an assertion, a value or an error
name. A defect that needs a judgement call does not belong here — leave it
broken and raise it upstream, because a runner that silently guesses what a
malformed conformance test *meant* is worse than one that reports it.

**Rules are meant to die.** :data:`EXPECTED_SITES` pins how many places each
one fires. When upstream fixes a defect and the corpus is refreshed, the count
drops, ``tests/test_acts_corpus.py`` fails, and that is the prompt to delete
the rule rather than to update the number. A rule at zero sites is dead code
and should go.

Pass ``compat=False`` to any loader entry point to switch this off and see the
corpus exactly as upstream ships it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any


#: ``expect_error`` keys the abstract error name as ``code``; the CDDL calls
#: it ``error_type`` and reserves numeric codes for the wire. All 13 sites
#: hold an error *name* (or an assertion over names), never a JSON-RPC number,
#: so this is a pure key rename.
#: Upstream: https://github.com/a2aproject/A2A/pull/1882#discussion_r3305157201
RULE_ERROR_TYPE_KEY = 'expect-error-code-key'

#: ``operation:`` uses push-config names that are absent from the spec's own
#: ``abstract-operation`` enum (§4.1). The enum's names are the only ones a
#: dispatcher can bind, so the legacy spellings are unrunnable as written.
#: Upstream: https://github.com/a2aproject/A2A/pull/1882#discussion_r3305157185
RULE_PUSH_CONFIG_OPERATION = 'push-config-operation-name'

#: A response field placed directly under ``expect``. The CDDL's
#: ``expect-block`` admits only ``status`` and ``body``, so left alone the
#: runner would look for a top-level ``task`` on the response and always fail.
RULE_EXPECT_BODY_FIELD = 'expect-field-outside-body'

#: ``expect: {error: {...}}`` used to assert a failure. Failures are asserted
#: with ``expect_error`` (§6.2). Converted to a bare ``expect_error``, meaning
#: "some A2A error, don't constrain which" — an idiom five other corpus tests
#: already use deliberately, so nothing is invented by adopting it here.
RULE_EXPECT_ERROR_BLOCK = 'expect-error-as-expect-field'


#: Legacy spelling -> the spec's ``abstract-operation`` name.
PUSH_CONFIG_OPERATIONS: dict[str, str] = {
    'set_push_notification_config': 'create_push_config',
    'get_push_notification_config': 'get_push_config',
    'list_push_notification_configs': 'list_push_configs',
    'delete_push_notification_config': 'delete_push_config',
}


#: How many sites each rule fires on across the pinned corpus. Asserted by
#: ``tests/test_acts_corpus.py``; see the module docstring on why a *drop*
#: here is the signal to delete a rule.
EXPECTED_SITES: dict[str, int] = {
    RULE_ERROR_TYPE_KEY: 13,
    RULE_PUSH_CONFIG_OPERATION: 18,
    RULE_EXPECT_BODY_FIELD: 2,
    RULE_EXPECT_ERROR_BLOCK: 2,
}


@dataclass(frozen=True)
class Rewrite:
    """One place a rule fired.

    Returned rather than logged so the corpus tests can assert the exact set,
    and so a refresh that changes the corpus shows up as a diff in *what was
    rewritten* and not only in what happened to load.
    """

    rule: str
    where: str
    detail: str

    def __str__(self) -> str:
        return f'{self.where}: {self.detail} [{self.rule}]'


def normalize_document(raw: Any) -> tuple[Any, list[Rewrite]]:
    """Apply every rule to a parsed ACTS document.

    Returns the normalized document and the rewrites that fired. The input is
    left untouched — the loader keeps the raw mapping from its ``include:``
    walk, and a caller comparing against upstream should not have it mutated
    underneath.

    Anything that is not a well-formed document is returned unchanged;
    reporting that is the schema's job, not this module's.
    """
    if not isinstance(raw, dict):
        return raw, []

    out: list[Rewrite] = []
    doc = copy.deepcopy(raw)

    for suite in doc.get('suites') or ():
        if not isinstance(suite, dict):
            continue
        for test in suite.get('tests') or ():
            if not isinstance(test, dict):
                continue
            test_id = test.get('id', '<unnamed test>')
            for step in test.get('steps') or ():
                if isinstance(step, dict):
                    _normalize_step(step, test_id, out)

    return doc, out


def _normalize_step(step: dict[str, Any], test_id: str, out: list[Rewrite]) -> None:
    """Apply the rules to one step, in place."""
    where = f'{test_id}.{step.get("id", "<unnamed step>")}'

    _rewrite_push_config_operation(step, where, out)
    _rewrite_error_type_key(step, where, out)
    # Runs last: it can remove `expect` entirely, and reads `expect_error`,
    # which the rule above may have just rewritten.
    _rewrite_expect_block(step, where, out)


def _rewrite_push_config_operation(
    step: dict[str, Any], where: str, out: list[Rewrite]
) -> None:
    operation = step.get('operation')
    if not isinstance(operation, str):
        return
    replacement = PUSH_CONFIG_OPERATIONS.get(operation)
    if replacement is None:
        return
    step['operation'] = replacement
    out.append(
        Rewrite(
            RULE_PUSH_CONFIG_OPERATION,
            where,
            f'operation {operation!r} -> {replacement!r}',
        )
    )


def _rewrite_error_type_key(
    step: dict[str, Any], where: str, out: list[Rewrite]
) -> None:
    expect_error = step.get('expect_error')
    if not isinstance(expect_error, dict) or 'code' not in expect_error:
        return
    if 'error_type' in expect_error:
        # Both spellings at once is a genuine ambiguity, not a known defect.
        # Leave it and let the schema reject it.
        return
    expect_error['error_type'] = expect_error.pop('code')
    out.append(
        Rewrite(RULE_ERROR_TYPE_KEY, where, 'expect_error.code -> expect_error.error_type')
    )


def _rewrite_expect_block(
    step: dict[str, Any], where: str, out: list[Rewrite]
) -> None:
    """Move stray keys out of ``expect``.

    Two distinct defects share one symptom — a key under ``expect`` that the
    CDDL does not allow — and they need opposite treatments. ``error`` means
    the step asserts a *failure*, which belongs in ``expect_error``. Anything
    else is a response field that belongs under ``body``.
    """
    expect = step.get('expect')
    if not isinstance(expect, dict):
        return

    stray = [key for key in expect if key not in ('status', 'body')]
    if not stray:
        return

    if 'error' in stray:
        stray.remove('error')
        del expect['error']
        if step.get('expect_error') is None:
            step['expect_error'] = {}
        # A string like `status: error` is not an HTTP status; it is the same
        # "this failed" intent spelled a second way, and `expect_error` now
        # carries it. An integer status is a real assertion and stays.
        if not isinstance(expect.get('status'), int):
            expect.pop('status', None)
        out.append(
            Rewrite(
                RULE_EXPECT_ERROR_BLOCK,
                where,
                'expect.error -> expect_error (failure asserted with the wrong key)',
            )
        )

    if stray:
        body = expect.setdefault('body', {})
        if isinstance(body, dict):
            for key in stray:
                body[key] = expect.pop(key)
            out.append(
                Rewrite(
                    RULE_EXPECT_BODY_FIELD,
                    where,
                    f'expect.{{{", ".join(sorted(stray))}}} -> expect.body',
                )
            )

    if not expect:
        del step['expect']


def site_counts(rewrites: list[Rewrite]) -> dict[str, int]:
    """Rewrites per rule, for comparison against :data:`EXPECTED_SITES`."""
    counts = dict.fromkeys(EXPECTED_SITES, 0)
    for rewrite in rewrites:
        counts[rewrite.rule] = counts.get(rewrite.rule, 0) + 1
    return counts


__all__ = [
    'EXPECTED_SITES',
    'PUSH_CONFIG_OPERATIONS',
    'RULE_ERROR_TYPE_KEY',
    'RULE_EXPECT_BODY_FIELD',
    'RULE_EXPECT_ERROR_BLOCK',
    'RULE_PUSH_CONFIG_OPERATION',
    'Rewrite',
    'normalize_document',
    'site_counts',
]
