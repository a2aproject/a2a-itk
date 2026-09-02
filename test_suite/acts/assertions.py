"""The ACTS assertion DSL (spec §5, §6.1, §6.2, §9.1).

An assertion is either a bare scalar — exact equality — or a map. A map is
where the format's one genuine difficulty lives, because §5.2 makes an
`expect` tree mirror the response by *nesting*, while §5.4 puts operators in a
map too. So `{status: {state: FOO}}` is two levels of path ending in an exact
match, and `{type: array, count_gte: 1}` is two operators on one value, and
nothing in the syntax separates them. `schema.py` deliberately leaves these
subtrees as opaque data for that reason; disambiguating them is this module's
job, and it can only be done here because it needs the response.

**The rule: a key is an operator when it is a known operator name *and* its
argument is well-formed for that operator; otherwise it is a field name.**
Everything a node yields is ANDed together, so operators and field descents
can share one map.

Both halves of that rule are load-bearing, and the corpus proves it:

* `{type: array, count_gte: 1, items: [...]}` mixes two operators with a
  third key — so "a map is operators *or* fields" would be wrong.
* `{type: {type: string}, title: {type: string}}` asserts on an RFC 9457
  problem-details body, whose members really are named `type` and `title` —
  so "a known name is always an operator" would be wrong too. The inner
  `type` takes a valid argument and is an operator; the outer one takes a
  map, which is not one of the six type names, and is a field.

One ambiguity survives and cannot be resolved: a field named `type` whose
expected value is literally one of the six type names. Nothing in A2A hits it,
and the format offers no escape, so it is documented rather than handled.

Nothing here performs I/O or knows about a transport; the runner supplies
already-unwrapped values (spec §4.2's assertion root) and turns the results
into report entries.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from test_suite.acts.schema import CollectionMatch, ExpectError, NamedAssertion
from test_suite.acts.variables import MISSING, read_path, read_path_all


class UntilError(ValueError):
    """A `repeat.until` expression that does not parse (spec §9.1)."""


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Failure:
    """One assertion that did not hold.

    Shaped to fill the report's `failure-detail` (spec §13.3) directly; the
    runner adds `step_id`, which is the only field this layer cannot know.
    """

    path: str
    operator: str
    expected: Any
    actual: Any
    message: str

    def as_detail(self) -> dict[str, str]:
        """Render to §13.3's `failure-detail`, stringified as it requires."""
        detail = {'message': self.message}
        if self.path:
            detail['assertion_path'] = self.path
        detail['expected'] = _show(self.expected)
        detail['actual'] = _show(self.actual)
        return detail


@dataclass(frozen=True, slots=True)
class AssertionResult:
    """The outcome of evaluating an assertion tree.

    `checks` counts the leaf comparisons that actually ran. It exists so a
    caller can tell "passed" from "asserted nothing" — an `expect` block that
    descends into a field the response does not have could otherwise report
    success having compared no values at all.
    """

    failures: tuple[Failure, ...] = ()
    checks: int = 0

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def first(self) -> Failure | None:
        """The failure to put in a report that has room for one."""
        return self.failures[0] if self.failures else None

    def __bool__(self) -> bool:
        return self.ok

    def __add__(self, other: AssertionResult) -> AssertionResult:
        return AssertionResult(self.failures + other.failures, self.checks + other.checks)


_PASS: Final = AssertionResult()


def _fail(
    path: str,
    operator: str,
    expected: Any,
    actual: Any,
    message: str,
) -> AssertionResult:
    return AssertionResult(
        (Failure(path, operator, expected, actual, message),), checks=1
    )


def _ok(count: int = 1) -> AssertionResult:
    return AssertionResult(checks=count)


def _show(value: Any) -> str:
    if isinstance(value, str):
        return value
    return repr(value)


def _join(path: str, part: str) -> str:
    if not path:
        return part
    return f'{path}{part}' if part.startswith('[') else f'{path}.{part}'


# ---------------------------------------------------------------------------
# Leaf operators (spec §5.1)
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    # `bool` subclasses `int` in Python but is not a JSON number.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_array(value: Any) -> bool:
    return isinstance(value, (list, tuple))


TYPE_NAMES: Final[Mapping[str, Callable[[Any], bool]]] = {
    'string': lambda v: isinstance(v, str),
    'number': _is_number,
    'boolean': lambda v: isinstance(v, bool),
    'array': _is_array,
    'object': lambda v: isinstance(v, Mapping),
    'null': lambda v: v is None,
}


@dataclass(frozen=True, slots=True)
class _Leaf:
    """A value-level operator: does this argument shape fit, and does it hold?"""

    accepts: Callable[[Any], bool]
    holds: Callable[[Any, Any], bool]
    #: Operators that are *about* presence, and so still mean something when
    #: the value is not there at all.
    tolerates_missing: bool = False
    describe: str = ''


def _contains(argument: Any, actual: Any) -> bool:
    # §5.1 files `contains` under string matching, but membership in a list is
    # the same question asked of a collection and has no other spelling.
    if isinstance(actual, str):
        return argument in actual
    if _is_array(actual):
        return argument in actual
    return False


def _count(actual: Any) -> int | None:
    return len(actual) if _is_array(actual) else None


def _count_op(compare: Callable[[int, int], bool]) -> Callable[[Any, Any], bool]:
    def holds(argument: Any, actual: Any) -> bool:
        size = _count(actual)
        return size is not None and compare(size, argument)

    return holds


def _compare(op: Callable[[Any, Any], bool]) -> Callable[[Any, Any], bool]:
    def holds(argument: Any, actual: Any) -> bool:
        return _is_number(actual) and op(actual, argument)

    return holds


def _matches(argument: Any, actual: Any) -> bool:
    # §5.1 says ECMA-262. Python's `re` is close but not identical (named
    # groups, lookbehind width, `\d` under Unicode); the corpus stays well
    # inside the shared subset, and a pattern that does not would be a corpus
    # bug worth surfacing rather than emulating around.
    if not isinstance(actual, str):
        return False
    return re.search(argument, actual) is not None


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)

LEAF_OPERATORS: Final[Mapping[str, _Leaf]] = {
    'type': _Leaf(
        accepts=lambda a: isinstance(a, str) and a in TYPE_NAMES,
        holds=lambda a, v: TYPE_NAMES[a](v),
        describe='be of type',
    ),
    'exists': _Leaf(
        accepts=_is_bool,
        holds=lambda a, v: (v is not MISSING) is a,
        tolerates_missing=True,
        describe='be present',
    ),
    'absent': _Leaf(
        accepts=_is_bool,
        holds=lambda a, v: (v is MISSING) is a,
        tolerates_missing=True,
        describe='be absent',
    ),
    'contains': _Leaf(accepts=_is_str, holds=_contains, describe='contain'),
    'matches': _Leaf(accepts=_is_str, holds=_matches, describe='match regex'),
    'starts_with': _Leaf(
        accepts=_is_str,
        holds=lambda a, v: isinstance(v, str) and v.startswith(a),
        describe='start with',
    ),
    'ends_with': _Leaf(
        accepts=_is_str,
        holds=lambda a, v: isinstance(v, str) and v.endswith(a),
        describe='end with',
    ),
    'gte': _Leaf(accepts=_is_number, holds=_compare(lambda v, a: v >= a), describe='be >='),
    'lte': _Leaf(accepts=_is_number, holds=_compare(lambda v, a: v <= a), describe='be <='),
    'gt': _Leaf(accepts=_is_number, holds=_compare(lambda v, a: v > a), describe='be >'),
    'lt': _Leaf(accepts=_is_number, holds=_compare(lambda v, a: v < a), describe='be <'),
    'count': _Leaf(
        accepts=_is_int, holds=_count_op(lambda n, a: n == a), describe='have length'
    ),
    'count_gte': _Leaf(
        accepts=_is_int, holds=_count_op(lambda n, a: n >= a), describe='have length >='
    ),
    'count_lte': _Leaf(
        accepts=_is_int, holds=_count_op(lambda n, a: n <= a), describe='have length <='
    ),
    'one_of': _Leaf(
        accepts=_is_array,
        holds=lambda a, v: any(_same(v, option) for option in a),
        describe='be one of',
    ),
}

#: Operators that take assertions rather than values, and so recurse.
COMBINATORS: Final[frozenset[str]] = frozenset({'all_of', 'any_of', 'not'})

#: Not in §5.1. The corpus uses `items` at five sites to assert on the
#: elements of an array *while also* asserting on the array itself
#: (`{type: array, count_gte: 1, items: ...}`), which §5.2's bare-list form
#: cannot express — one YAML node cannot be both a map and a list. Semantics
#: follow JSON Schema, which is plainly where it was borrowed from: a map
#: applies to every element, a list applies positionally.
EXTENSION_OPERATORS: Final[frozenset[str]] = frozenset({'items'})

OPERATORS: Final[frozenset[str]] = (
    frozenset(LEAF_OPERATORS) | COMBINATORS | EXTENSION_OPERATORS
)


def _accepts(name: str, argument: Any) -> bool:
    """Is ``argument`` well-formed for the operator ``name``?

    The half of the disambiguation rule that tells an operator from a field
    that happens to share its name.
    """
    if name in LEAF_OPERATORS:
        return LEAF_OPERATORS[name].accepts(argument)
    if name in ('all_of', 'any_of'):
        return _is_array(argument) and len(argument) > 0
    if name == 'not':
        # Any assertion is a valid argument, so this can never discriminate.
        # No A2A field is named `not`.
        return True
    if name == 'items':
        return _is_array(argument) or isinstance(argument, Mapping)
    return False


def _same(actual: Any, expected: Any) -> bool:
    """Exact equality, without Python's `True == 1`."""
    if isinstance(expected, bool) != isinstance(actual, bool):
        return False
    return actual == expected


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate(assertion: Any, actual: Any, *, path: str = '') -> AssertionResult:
    """Evaluate one assertion subtree against one value.

    ``actual`` may be :data:`~test_suite.acts.variables.MISSING`, which is how
    a field the response does not carry reaches the presence operators.
    """
    if isinstance(assertion, Mapping):
        return _evaluate_map(assertion, actual, path)
    if isinstance(assertion, (list, tuple)):
        return _evaluate_positional(assertion, actual, path)
    return _evaluate_exact(assertion, actual, path)


def _evaluate_exact(expected: Any, actual: Any, path: str) -> AssertionResult:
    if actual is MISSING:
        return _fail(path, 'exact', expected, actual, f'{path or "value"} is missing')
    if _same(actual, expected):
        return _ok()
    return _fail(
        path,
        'exact',
        expected,
        actual,
        f'{path or "value"} should equal {_show(expected)}, got {_show(actual)}',
    )


def _evaluate_positional(
    assertions: Sequence[Any], actual: Any, path: str
) -> AssertionResult:
    """A list asserts on array elements by position (spec §5.2)."""
    if not _is_array(actual):
        return _fail(
            path, 'items', assertions, actual,
            f'{path or "value"} should be an array to match by position',
        )
    if len(actual) < len(assertions):
        return _fail(
            path, 'items', assertions, actual,
            f'{path or "value"} has {len(actual)} element(s), '
            f'{len(assertions)} asserted',
        )
    result = _PASS
    for i, element_assertion in enumerate(assertions):
        result += evaluate(element_assertion, actual[i], path=_join(path, f'[{i}]'))
    return result


def _evaluate_map(node: Mapping[str, Any], actual: Any, path: str) -> AssertionResult:
    """Split a map into operators and field descents, and AND the two."""
    result = _PASS
    for key, argument in node.items():
        if key in OPERATORS and _accepts(key, argument):
            result += _apply_operator(key, argument, actual, path)
        else:
            result += evaluate(argument, _member(actual, key), path=_join(path, key))
    return result


def _member(actual: Any, key: str) -> Any:
    if isinstance(actual, Mapping) and key in actual:
        return actual[key]
    return MISSING


def _apply_operator(
    name: str, argument: Any, actual: Any, path: str
) -> AssertionResult:
    if name in LEAF_OPERATORS:
        return _apply_leaf(name, argument, actual, path)
    if name == 'all_of':
        result = _PASS
        for branch in argument:
            result += evaluate(branch, actual, path=path)
        return result
    if name == 'any_of':
        attempts = [evaluate(branch, actual, path=path) for branch in argument]
        if any(attempt.ok for attempt in attempts):
            return _ok()
        return _fail(
            path, 'any_of', argument, actual,
            f'{path or "value"} matched none of the {len(argument)} alternatives',
        )
    if name == 'not':
        if evaluate(argument, actual, path=path).ok:
            return _fail(
                path, 'not', argument, actual,
                f'{path or "value"} should not have matched {_show(argument)}',
            )
        return _ok()
    if name == 'items':
        return _apply_items(argument, actual, path)
    raise AssertionError(f'unhandled operator {name!r}')  # pragma: no cover


def _apply_leaf(
    name: str, argument: Any, actual: Any, path: str
) -> AssertionResult:
    leaf = LEAF_OPERATORS[name]
    if actual is MISSING and not leaf.tolerates_missing:
        return _fail(
            path, name, argument, actual,
            f'{path or "value"} is missing, so it cannot {leaf.describe} '
            f'{_show(argument)}',
        )
    if leaf.holds(argument, actual):
        return _ok()
    return _fail(
        path, name, argument, actual,
        f'{path or "value"} should {leaf.describe} {_show(argument)}, '
        f'got {_show(actual)}',
    )


def _apply_items(argument: Any, actual: Any, path: str) -> AssertionResult:
    if _is_array(argument):
        return _evaluate_positional(argument, actual, path)
    if not _is_array(actual):
        return _fail(
            path, 'items', argument, actual,
            f'{path or "value"} should be an array for `items`',
        )
    result = _PASS
    for i, element in enumerate(actual):
        result += evaluate(argument, element, path=_join(path, f'[{i}]'))
    return result


# ---------------------------------------------------------------------------
# Expect blocks
# ---------------------------------------------------------------------------


def evaluate_body(
    body: Mapping[str, Any], actual: Any, *, path: str = 'body'
) -> AssertionResult:
    """Evaluate an `expect.body` tree against a response (spec §6.1)."""
    return _evaluate_map(body, actual, path)


def evaluate_status(
    status: Any, actual: Any, *, path: str = 'status'
) -> AssertionResult:
    """Evaluate `expect.status`, a bare code or a numeric matcher."""
    return evaluate(status, actual, path=path)


def evaluate_error(
    expected: ExpectError, observed: Mapping[str, Any], *, path: str = 'error'
) -> AssertionResult:
    """Evaluate an `expect_error` block (spec §6.2).

    ``observed`` is the error as the runner recovered it — `error_type` being
    the abstract name it mapped the wire code back to, or absent when the SUT
    gave nothing to map. Each declared field is an ordinary assertion, so
    `error_type: {one_of: [...]}` works exactly like any other.
    """
    result = _PASS
    for name in ('error_type', 'message', 'data'):
        assertion = getattr(expected, name)
        if assertion is not None:
            observed_value = observed.get(name, MISSING)
            result += evaluate(assertion, observed_value, path=_join(path, name))
    if expected.details is not None:
        details = observed.get('details', MISSING)
        result += _evaluate_map(expected.details, details, _join(path, 'details'))
    return result


# ---------------------------------------------------------------------------
# Collection and named assertions (spec §5.5)
# ---------------------------------------------------------------------------


def evaluate_collection(
    match: CollectionMatch, mode: str, source: Any, *, path: str = ''
) -> AssertionResult:
    """Evaluate `any`/`all`/`none` over a wildcard path (spec §5.5).

    An empty match set is the interesting case, and the three quantifiers do
    not agree about it. `any` and `all` **fail**: both are claims that
    something was inspected, and reporting success having inspected nothing is
    the one outcome a conformance suite must never produce. `none` passes,
    because "no element is X" is genuinely satisfied by having no elements.
    """
    if mode not in ('any', 'all', 'none'):
        raise ValueError(f'unknown collection mode {mode!r}')

    reached = list(read_path_all(source, match.path))
    base = _join(path, match.path) if path else match.path

    if not reached:
        if mode == 'none':
            return _ok()
        return _fail(
            base, mode, match.match, MISSING,
            f'{match.path} matched no elements, so `{mode}` checked nothing',
        )

    outcomes = [
        (where, _evaluate_map(match.match, value, _join(path, where)))
        for where, value in reached
    ]
    passed = [where for where, outcome in outcomes if outcome.ok]
    # Real comparisons, not element count: an empty `match` inspects nothing
    # however many elements it is handed, and `checks` exists to say so.
    checks = sum(outcome.checks for _, outcome in outcomes)

    if mode == 'all':
        failures = tuple(f for _, o in outcomes for f in o.failures)
        if failures:
            return AssertionResult(failures, checks)
        return _ok(checks)
    if mode == 'any':
        if passed:
            return _ok(checks)
        return _fail(
            base, 'any', match.match, MISSING,
            f'none of the {len(outcomes)} element(s) at {match.path} matched',
        )
    if passed:
        return _fail(
            base, 'none', match.match, passed,
            f'{len(passed)} of {len(outcomes)} element(s) at {match.path} '
            f'matched but should not have ({", ".join(passed)})',
        )
    return _ok(checks)


def evaluate_named(
    assertion: NamedAssertion, source: Any, *, path: str = ''
) -> AssertionResult:
    """Evaluate a `named-assertion` against an already-resolved source.

    Resolving `source` — usually `{{step.response}}` — belongs to the runner's
    scope, so it arrives here as a value. `path` optionally narrows into it
    before the check applies.
    """
    root = path or (assertion.id or '')
    value = source
    if assertion.path:
        value = read_path(source, assertion.path)
        root = _join(root, assertion.path)

    if assertion.match is not None:
        return evaluate(assertion.match, value, path=root)
    for mode in ('any', 'all', 'none'):
        collection = getattr(assertion, mode)
        if collection is not None:
            return evaluate_collection(collection, mode, value, path=root)
    raise ValueError(  # pragma: no cover - schema forbids reaching this
        f'named assertion {assertion.id!r} has no check'
    )


# ---------------------------------------------------------------------------
# `repeat.until` expressions (spec §9.1)
# ---------------------------------------------------------------------------

_UNTIL = re.compile(
    r'^\s*(?P<path>[^\s=!]+)\s*'
    r'(?:(?P<op>==|!=)\s*(?P<value>.+?)|in\s*\[(?P<list>.*)\])\s*$'
)


def _literal(token: str) -> Any:
    """Read an `until` operand.

    Bare words are strings — the corpus compares against `TASK_STATE_WORKING`
    and friends, unquoted — with the JSON spellings and numbers recognized
    first so `true`, `null` and `3` do not become the strings "true", "null"
    and "3".
    """
    text = token.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in '\'"':
        return text[1:-1]
    if text == 'true':
        return True
    if text == 'false':
        return False
    if text in ('null', 'none'):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def evaluate_until(expression: str, response: Any) -> bool:
    """Is a `repeat.until` condition satisfied by this response?

    Three forms, per §9.1: `path == value`, `path != value`,
    `path in [a, b]`. A path that reads nothing is not an error — the field
    is simply not there yet, which is the ordinary state of a task being
    polled — so the condition is just false and the runner tries again.
    """
    match = _UNTIL.match(expression)
    if match is None:
        raise UntilError(
            f'cannot parse until expression {expression!r}; expected '
            f'`path == value`, `path != value` or `path in [a, b]`'
        )

    actual = read_path(response, match.group('path'))

    if match.group('list') is not None:
        options = [
            _literal(token)
            for token in match.group('list').split(',')
            if token.strip()
        ]
        if not options:
            raise UntilError(f'until expression {expression!r} has an empty list')
        return any(_same(actual, option) for option in options)

    expected = _literal(match.group('value'))
    equal = _same(actual, expected)
    return equal if match.group('op') == '==' else not equal


__all__ = [
    'COMBINATORS',
    'EXTENSION_OPERATORS',
    'LEAF_OPERATORS',
    'OPERATORS',
    'TYPE_NAMES',
    'AssertionResult',
    'Failure',
    'UntilError',
    'evaluate',
    'evaluate_body',
    'evaluate_collection',
    'evaluate_error',
    'evaluate_named',
    'evaluate_status',
    'evaluate_until',
]
