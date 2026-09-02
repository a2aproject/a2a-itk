"""Variable substitution, path reads and capture (spec §8, §12.2).

Three small things the runner and the assertion evaluator both need, kept
together because they share one notion of "a path into a response":

* **`{{...}}` substitution** — resolving a reference against a test's scope.
* **Dot-paths** — `task.artifacts[0].parts[*].text`, used as a *value* by
  `capture`, `repeat.until` and `collection-match.path` (spec §5.2 forbids
  them as `expect` keys, where YAML nesting does the same job).
* **`capture`** — reading named values out of a response into the scope.

Nothing here talks to a SUT or evaluates an assertion; this is the pure layer
underneath both.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final


class _Missing:
    """A value that is not there at all.

    Distinct from ``None``, and the distinction is load-bearing: JSON has
    ``null``, so ``{exists: true}`` must pass for a field present and null,
    while ``{absent: true}`` must fail for it. Collapsing the two onto ``None``
    would make those two operators agree with each other, which is precisely
    what they exist to disagree about.
    """

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return '<missing>'


MISSING: Final = _Missing()


class PathError(ValueError):
    """A dot-path that cannot be parsed."""


class UnresolvedVariable(LookupError):
    """A `{{...}}` reference with nothing behind it.

    Spec §12.2 requires the step to fail with a clear message rather than the
    reference being left in place or silently emptied — a request carrying a
    literal ``{{send.taskId}}`` would fail against the SUT for a reason that
    looks like non-conformance.
    """


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Key:
    """A named field."""

    name: str

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class Index:
    """A fixed array position."""

    at: int

    def __str__(self) -> str:
        return f'[{self.at}]'


@dataclass(frozen=True, slots=True)
class _Wildcard:
    """Every element of an array (`[*]`), for collection assertions only."""

    def __str__(self) -> str:
        return '[*]'


WILDCARD: Final = _Wildcard()

Segment = Key | Index | _Wildcard

_SEGMENT = re.compile(r'\[\s*(\*|-?\d+)\s*\]|([^.\[\]]+)')


def parse_path(path: str) -> tuple[Segment, ...]:
    """Split a dot-path into segments.

    `task.artifacts[0].parts[*]` becomes key, key, index, key, wildcard. An
    empty path is the identity — it addresses the root, which is what
    `named-assertion` means by omitting `path`.
    """
    text = path.strip()
    if not text:
        return ()

    segments: list[Segment] = []
    position = 0
    for match in _SEGMENT.finditer(text):
        if match.start() != position:
            gap = text[position:match.start()]
            if gap != '.':
                raise PathError(f'cannot parse {path!r} at offset {position}: {gap!r}')
        position = match.end()
        bracketed, name = match.group(1), match.group(2)
        if bracketed is None:
            segments.append(Key(name))
        elif bracketed == '*':
            segments.append(WILDCARD)
        else:
            segments.append(Index(int(bracketed)))

    if position != len(text):
        raise PathError(f'cannot parse {path!r}: trailing {text[position:]!r}')
    return tuple(segments)


def format_path(segments: Sequence[Segment]) -> str:
    """Render segments back to a dot-path, for failure messages."""
    out = ''
    for segment in segments:
        if isinstance(segment, Key):
            out = f'{out}.{segment.name}' if out else segment.name
        else:
            out = f'{out}{segment}'
    return out


def _step(value: Any, segment: Segment) -> Any:
    """Follow one segment, or return `MISSING`.

    A miss is never an error: `{absent: true}` is a legitimate assertion, and
    a path into a response that took a different shape should read as a
    failed assertion rather than a crashed run.
    """
    if isinstance(segment, Key):
        if isinstance(value, Mapping) and segment.name in value:
            return value[segment.name]
        return MISSING
    if isinstance(segment, Index):
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            try:
                return value[segment.at]
            except IndexError:
                return MISSING
        return MISSING
    raise PathError('`[*]` is only valid in a collection assertion path')


def read_path(value: Any, path: str) -> Any:
    """Read a dot-path out of ``value``, or `MISSING`.

    Rejects wildcards: a `capture` or an `until` needs exactly one value, and
    quietly taking the first match of several would make which one you got
    depend on response ordering.
    """
    for segment in parse_path(path):
        value = _step(value, segment)
        if value is MISSING:
            return MISSING
    return value


def read_path_all(value: Any, path: str) -> Iterator[tuple[str, Any]]:
    """Every value a wildcard path reaches, each with its concrete path.

    `task.artifacts[*].parts[*]` over two artifacts of one part each yields
    two pairs, keyed `task.artifacts[0].parts[0]` and `...[1].parts[0]`. The
    concrete path is what makes a collection failure locatable.

    Missing branches are skipped rather than reported: "no element matched"
    is the `any`/`all`/`none` result, not an error.
    """
    yield from _walk(value, parse_path(path), ())


def _walk(
    value: Any,
    remaining: Sequence[Segment],
    reached: tuple[Segment, ...],
) -> Iterator[tuple[str, Any]]:
    if not remaining:
        yield format_path(reached), value
        return
    head, tail = remaining[0], remaining[1:]
    if head is WILDCARD:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for i, element in enumerate(value):
                yield from _walk(element, tail, (*reached, Index(i)))
        return
    stepped = _step(value, head)
    if stepped is not MISSING:
        yield from _walk(stepped, tail, (*reached, head))


# ---------------------------------------------------------------------------
# Substitution
# ---------------------------------------------------------------------------

# The body cannot contain a brace, so `{{a}}-{{b}}` is two references rather
# than one spanning reference — which a lazy `.+?` still matches under
# `fullmatch`, because being forced to cover the whole string beats being lazy.
_REFERENCE = re.compile(r'\{\{\s*([^{}]+?)\s*\}\}')

#: The capture name every step gets for free. Spec §5.5 has `named-assertion`
#: point `source` at a prior step's whole response, and the corpus writes that
#: as `{{get.response}}` without ever declaring it in a `capture` block.
IMPLICIT_RESPONSE: Final = 'response'


class Scope:
    """The variables one test can see.

    A test is the isolation unit (steps within it share captures, tests never
    do), so the runner builds one of these per test and throws it away after.

    Resolution follows spec §12.2, highest precedence first: step captures,
    then the document `variables` map, then `env.`, then `$uuid`. In practice
    the four barely compete — `{{env.X}}` and `{{$uuid}}` are syntactically
    distinct, and so is `{{stepId.name}}` from `{{name}}` — so the order only
    decides a document variable literally named `foo.bar` against step `foo`'s
    capture `bar`. The step capture wins.
    """

    def __init__(
        self,
        variables: Mapping[str, Any] | None = None,
        *,
        env: Mapping[str, str] | None = None,
        new_uuid: Callable[[], str] | None = None,
    ) -> None:
        #: Document-level `variables`, already overlaid with anything the
        #: runner injected. Two of the corpus's references — an insufficient
        #: auth token and another user's task id — are never defined by any
        #: document and can only arrive this way.
        self.variables: dict[str, Any] = dict(variables or {})
        self._env = os.environ if env is None else env
        self._new_uuid = new_uuid if new_uuid is not None else lambda: str(uuid.uuid4())
        self._captures: dict[str, dict[str, Any]] = {}

    # -- recording ---------------------------------------------------------

    def record(self, step_id: str, name: str, value: Any) -> None:
        """Make `{{step_id.name}}` resolve to ``value``."""
        self._captures.setdefault(step_id, {})[name] = value

    def record_response(self, step_id: str, response: Any) -> None:
        """Make `{{step_id.response}}` resolve to a step's whole response.

        Recorded for every step, whether or not it declares a `capture`. An
        explicit capture named `response` shadows this; nothing in the corpus
        does that, and if a document did, honoring what it wrote is the less
        surprising behavior.
        """
        self._captures.setdefault(step_id, {}).setdefault(IMPLICIT_RESPONSE, response)

    def captures_for(self, step_id: str) -> Mapping[str, Any]:
        """What ``step_id`` has captured so far. Read-only view, for reports."""
        return dict(self._captures.get(step_id, {}))

    def capture(
        self,
        step_id: str,
        spec: Mapping[str, str],
        response: Any,
    ) -> dict[str, Any]:
        """Apply a step's `capture` block against its response.

        Returns what was captured. A path that reads `MISSING` raises: a
        capture feeds later steps, so silently binding "nothing" turns one
        unmet expectation into a cascade of confusing failures further down.
        """
        captured: dict[str, Any] = {}
        for name, path in spec.items():
            value = read_path(response, path)
            if value is MISSING:
                raise UnresolvedVariable(
                    f'step {step_id!r} cannot capture {name!r}: no value at '
                    f'path {path!r} in the response'
                )
            self.record(step_id, name, value)
            captured[name] = value
        return captured

    # -- resolution --------------------------------------------------------

    def resolve(self, reference: str) -> Any:
        """Resolve the inside of a `{{...}}`, preserving the value's type."""
        ref = reference.strip()

        if ref == '$uuid':
            # Spec §8.1: "each occurrence produces a new value".
            return self._new_uuid()

        if '.' in ref:
            head, _, tail = ref.partition('.')
            if head in self._captures and tail in self._captures[head]:
                return self._captures[head][tail]
            if ref in self.variables:
                return self.variables[ref]
            if head == 'env':
                if tail in self._env:
                    return self._env[tail]
                raise UnresolvedVariable(f'environment variable {tail!r} is not set')
            if head in self._captures:
                known = sorted(self._captures[head])
                raise UnresolvedVariable(
                    f'step {head!r} captured nothing called {tail!r}; it has {known}'
                )
            raise UnresolvedVariable(
                f'no step {head!r} has run yet, and no variable is named {ref!r}'
            )

        if ref in self.variables:
            return self.variables[ref]
        raise UnresolvedVariable(
            f'undefined variable {ref!r}; known: {sorted(self.variables)}'
        )

    def substitute(self, node: Any) -> Any:
        """Resolve every `{{...}}` in a structure, returning a new one.

        Two cases, and the difference matters. A string that is *nothing but*
        one reference yields the referenced value with its own type, so a
        captured integer stays an integer for an exact-match assertion.
        A reference embedded in surrounding text — `Bearer {{token}}` — is
        string interpolation and yields a string.

        Mapping keys are left alone: spec §8.1 puts references "within text
        values", and in an `expect` tree the keys are response field names.
        """
        if isinstance(node, str):
            whole = _REFERENCE.fullmatch(node)
            if whole is not None:
                return self.resolve(whole.group(1))
            return _REFERENCE.sub(lambda m: str(self.resolve(m.group(1))), node)
        if isinstance(node, Mapping):
            return {key: self.substitute(value) for key, value in node.items()}
        if isinstance(node, (list, tuple)):
            return [self.substitute(value) for value in node]
        return node


def references(node: Any) -> set[str]:
    """Every `{{...}}` reference in a structure, unresolved.

    Lets a caller see what a test will need before running it — the corpus
    names two variables no document defines, and finding that out at dispatch
    time is late.
    """
    found: set[str] = set()
    if isinstance(node, str):
        found.update(match.strip() for match in _REFERENCE.findall(node))
    elif isinstance(node, Mapping):
        for value in node.values():
            found |= references(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            found |= references(value)
    return found


__all__ = [
    'IMPLICIT_RESPONSE',
    'MISSING',
    'WILDCARD',
    'Index',
    'Key',
    'PathError',
    'Scope',
    'Segment',
    'UnresolvedVariable',
    'format_path',
    'parse_path',
    'read_path',
    'read_path_all',
    'references',
]
