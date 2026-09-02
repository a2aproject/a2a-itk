"""Load ACTS documents from disk and flatten a manifest into a run plan.

Two entry points, deliberately separate:

``load_document``
    One file, validated into an :class:`~test_suite.acts.schema.ActsDocument`.

``load_suite``
    A manifest — ``scenarios/acts/suite.acts.yaml`` — followed through its
    ``include:`` list into a flat, ordered :class:`LoadedSuite` of tests, each
    carrying where it came from.

Flattening is the loader's job rather than the runner's because the checks
that matter are cross-file: a test id duplicated between two suite files
would silently overwrite a row in the report, and no single-file validation
can see it.

Both entry points apply :mod:`test_suite.acts.compat` by default, which
rewrites the known CDDL violations in the pinned upstream corpus on the way
in. Pass ``compat=False`` to validate a document exactly as written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from test_suite.acts.compat import Rewrite, normalize_document
from test_suite.acts.schema import (
    ActsDocument,
    Level,
    Suite,
    Test,
    TransportBinding,
)


class ActsFileError(ValueError):
    """An ACTS file is missing, unparseable, or fails validation.

    Carries a message naming the file and the offending test. These are
    authoring mistakes; a traceback would bury the one line that matters.
    """


@dataclass(frozen=True)
class LoadedTest:
    """A test plus where it came from.

    Provenance is not decoration: with fourteen files flattened into one list,
    "CORE-SEND-002 is malformed" is only actionable with the file and suite
    attached.
    """

    test: Test
    suite_id: str
    suite_name: str
    source: Path

    @property
    def id(self) -> str:
        return self.test.id

    @property
    def level(self) -> Level:
        return self.test.level

    def __str__(self) -> str:
        return f'{self.id} ({self.suite_id} in {self.source.name})'


@dataclass(frozen=True)
class LoadError:
    """One test that failed validation, and why.

    Kept as data rather than raised immediately so a single load reports
    every bad test at once instead of one per run-fix-rerun cycle.
    """

    source: Path
    where: str
    message: str

    def __str__(self) -> str:
        return f'{self.source.name}: {self.where}: {self.message}'


@dataclass
class LoadedSuite:
    """Everything a manifest pulled in.

    ``tests`` is flat and ordered: manifest ``include:`` order, then suite
    order within a file, then test order within a suite. Stable ordering
    keeps report diffs readable across runs.
    """

    tests: list[LoadedTest] = field(default_factory=list)
    variables: dict[str, str] = field(default_factory=dict)
    sources: list[Path] = field(default_factory=list)
    errors: list[LoadError] = field(default_factory=list)
    #: Upstream defects rewritten on the way in; empty when ``compat=False``.
    #: Surfaced rather than swallowed so a run can report that it did not
    #: execute the corpus quite as shipped.
    rewrites: list[Rewrite] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.tests)

    def __iter__(self):
        return iter(self.tests)

    def by_id(self, test_id: str) -> LoadedTest | None:
        return next((t for t in self.tests if t.id == test_id), None)

    def for_transport(self, transport: TransportBinding) -> list[LoadedTest]:
        """Tests that apply to ``transport``.

        A test with no ``transport:`` restriction applies to all of them.
        """
        return [t for t in self.tests if t.test.applies_to(transport)]

    def by_level(self, level: Level) -> list[LoadedTest]:
        return [t for t in self.tests if t.level is level]

    def required_behaviors(self) -> frozenset[str]:
        """Every ``tck-*`` prefix any loaded test needs.

        The set an SDK's ``acts/sut-behaviors.yaml`` is checked against.
        """
        out: set[str] = set()
        for loaded in self.tests:
            out |= loaded.test.behaviors()
        return frozenset(out)

    def suite_ids(self) -> list[str]:
        """Suite ids, in load order, without repeats."""
        seen: list[str] = []
        for loaded in self.tests:
            if loaded.suite_id not in seen:
                seen.append(loaded.suite_id)
        return seen


def load_document(path: Path, compat: bool = True) -> ActsDocument:
    """Read and validate one ACTS file.

    Args:
        path: The file to read.
        compat: Rewrite the known upstream defects first. Set ``False`` to
            validate the file exactly as written.

    Raises:
        ActsFileError: Missing file, malformed YAML, or a document that fails
            schema validation.
    """
    return parse_document(_read_yaml(path), source=path, compat=compat)


def parse_document(
    data: Any, source: Path | None = None, compat: bool = True
) -> ActsDocument:
    """Validate an already-parsed mapping into an :class:`ActsDocument`.

    Separate from :func:`load_document` so a document arriving over HTTP
    takes the same path as a file on disk, compat rules included.

    Raises:
        ActsFileError: The document is not a mapping, or fails validation.
    """
    where = f'{source}: ' if source else ''
    if not isinstance(data, dict):
        raise ActsFileError(
            f'{where}expected a mapping at the top level, '
            f'got {type(data).__name__}'
        )
    if compat:
        data, _ = normalize_document(data)
    try:
        return ActsDocument.model_validate(data)
    except ValidationError as e:
        raise ActsFileError(f'{where}{render_validation_error(e)}') from None


def load_suite(
    path: Path, strict: bool = True, compat: bool = True
) -> LoadedSuite:
    """Load a manifest and everything it includes, flattened.

    Args:
        path: The manifest, normally ``scenarios/acts/suite.acts.yaml``. A
            plain suite file works too — it just includes nothing.
        strict: Raise on the first invalid test. Set ``False`` to load what is
            valid and collect the rest in ``LoadedSuite.errors`` — for
            triaging a corpus refresh, where a newly-broken test should not
            stop the other 110 from loading.
        compat: Rewrite the known upstream defects on the way in, recording
            each in ``LoadedSuite.rewrites``. Set ``False`` to see the corpus
            exactly as shipped — with which the pinned snapshot has 26 invalid
            tests, so pair it with ``strict=False``.

    Raises:
        ActsFileError: A file is missing or unparseable; an ``include:``
            escapes the manifest's directory; a suite or test id is duplicated
            across files; or, when ``strict``, any test fails validation.
    """
    root = path.parent.resolve()
    loaded = LoadedSuite()
    seen_test_ids: dict[str, LoadedTest] = {}
    seen_suite_ids: dict[str, Path] = {}

    for doc_path, raw in _resolve_includes(path, root):
        if compat:
            raw, rewrites = normalize_document(raw)
            loaded.rewrites.extend(rewrites)
        # Already normalized above, so validation runs on the document as-is.
        doc, doc_errors = _validate_document(raw, doc_path, strict=strict)
        loaded.errors.extend(doc_errors)
        loaded.sources.append(doc_path)
        if doc is None:
            continue

        # Later files win: a manifest sets defaults, an included file
        # overrides for its own tests.
        loaded.variables.update(doc.variables or {})

        for suite in doc.suites or ():
            _check_unique_suite(suite, doc_path, seen_suite_ids)
            for test in suite.tests:
                entry = LoadedTest(
                    test=test,
                    suite_id=suite.id,
                    suite_name=suite.name,
                    source=doc_path,
                )
                _check_unique_test(entry, seen_test_ids)
                seen_test_ids[test.id] = entry
                loaded.tests.append(entry)

    return loaded


def _validate_document(
    raw: Any, path: Path, strict: bool
) -> tuple[ActsDocument | None, list[LoadError]]:
    """Validate a document, degrading to per-test validation when not strict.

    A pydantic failure anywhere in ``suites`` rejects the whole document, which
    for the upstream corpus would throw away a whole file over one bad test. So
    when ``strict`` is off and the document fails, retry test by test and keep
    the ones that stand up.

    ``compat=False`` throughout: :func:`load_suite` normalizes each document
    once, up front, so re-running the rules here would only deep-copy again.
    """
    try:
        return parse_document(raw, source=path, compat=False), []
    except ActsFileError:
        if strict:
            raise
        return _salvage_document(raw, path)


def _salvage_document(raw: Any, path: Path) -> tuple[ActsDocument | None, list[LoadError]]:
    """Re-validate a failed document one test at a time.

    Only individual bad *tests* are survivable. If picking them out doesn't
    explain why the document failed, the fault is in the envelope or a suite
    header, and the original error is re-raised — dropping a whole file
    quietly is the failure mode this loader exists to prevent.
    """
    if not isinstance(raw, dict):
        raise ActsFileError(f'{path}: expected a mapping at the top level')

    errors: list[LoadError] = []
    kept_suites: list[dict[str, Any]] = []

    for s_idx, raw_suite in enumerate(raw.get('suites') or ()):
        if not isinstance(raw_suite, dict):
            raise ActsFileError(f'{path}: suites[{s_idx}] must be a mapping')

        good_tests: list[Any] = []
        for t_idx, raw_test in enumerate(raw_suite.get('tests') or ()):
            where = f'suites[{s_idx}].tests[{t_idx}]'
            if isinstance(raw_test, dict) and 'id' in raw_test:
                where = f'{where} ({raw_test["id"]})'
            try:
                Test.model_validate(raw_test)
            except ValidationError as e:
                errors.append(LoadError(path, where, render_validation_error(e)))
                continue
            good_tests.append(raw_test)

        if good_tests:
            kept_suites.append({**raw_suite, 'tests': good_tests})

    if not errors:
        # Nothing per-test was wrong, so salvaging cannot have fixed anything.
        raise ActsFileError(
            f'{path}: document is invalid and no individual test explains it; '
            f'check the acts_version/spec_version envelope and suite headers'
        )

    if not kept_suites:
        # Every test was bad. The file contributes nothing, but the errors do.
        return None, errors

    # If this still fails, the remaining fault was never per-test.
    return parse_document(
        {**raw, 'suites': kept_suites}, source=path, compat=False
    ), errors


def _resolve_includes(path: Path, root: Path) -> list[tuple[Path, Any]]:
    """Depth-first walk of ``include:``, manifest first.

    Returns each file once with its parsed contents — parsed here rather than
    re-read by the caller, since the walk has to look at ``include:`` anyway.
    Order is first-reached. Including a file twice is not an error (two suites
    may legitimately share one), and visiting each at most once makes that
    harmless and makes a cycle (``a`` includes ``b`` includes ``a``) terminate
    on its own, with each file's tests appearing once.
    """
    ordered: list[tuple[Path, Any]] = []
    visited: set[Path] = set()
    stack: list[Path] = [path.resolve()]

    while stack:
        current = stack.pop()
        if current in visited:
            continue

        raw = _read_yaml(current)
        if not isinstance(raw, dict):
            raise ActsFileError(
                f'{current}: expected a mapping at the top level, '
                f'got {type(raw).__name__}'
            )

        visited.add(current)
        ordered.append((current, raw))

        includes = raw.get('include', [])
        if not isinstance(includes, list):
            raise ActsFileError(f'{current}: `include` must be a list of filenames')

        children: list[Path] = []
        for entry in includes:
            if not isinstance(entry, str):
                raise ActsFileError(
                    f'{current}: `include` entries must be filenames, '
                    f'got {type(entry).__name__}'
                )
            child = (current.parent / entry).resolve()
            if not _is_within(child, root):
                # An include is a relative filename, not a path into the
                # filesystem; a corpus that reaches outside its own directory
                # is not portable to another checkout.
                raise ActsFileError(
                    f'{current}: include {entry!r} escapes the suite directory '
                    f'({root})'
                )
            if not child.is_file():
                raise ActsFileError(f'{current}: included file not found: {entry}')
            children.append(child)

        # Reversed, because the stack pops last-first and include order is
        # what report ordering follows.
        stack.extend(reversed(children))

    return ordered


def _is_within(candidate: Path, root: Path) -> bool:
    return candidate == root or root in candidate.parents


def _check_unique_suite(
    suite: Suite, path: Path, seen: dict[str, Path]
) -> None:
    if suite.id in seen:
        raise ActsFileError(
            f'{path}: duplicate suite id {suite.id!r}, already defined in '
            f'{seen[suite.id].name}'
        )
    seen[suite.id] = path


def _check_unique_test(entry: LoadedTest, seen: dict[str, LoadedTest]) -> None:
    if entry.id in seen:
        prior = seen[entry.id]
        raise ActsFileError(
            f'{entry.source}: duplicate test id {entry.id!r}, already defined '
            f'in {prior.source.name} (suite {prior.suite_id}); report rows are '
            f'keyed by test id, so one would overwrite the other'
        )


def _read_yaml(path: Path) -> Any:
    if not path.is_file():
        raise ActsFileError(f'ACTS file not found: {path}')
    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8'))
    except yaml.YAMLError as e:
        raise ActsFileError(f'{path}: could not parse: {e}') from e
    if data is None:
        raise ActsFileError(f'{path}: file is empty')
    return data


def render_validation_error(e: ValidationError) -> str:
    """Flatten a pydantic error to one clause per problem.

    Pydantic's own rendering is several lines and a docs URL per error, which
    buries the signal when a whole directory is being validated.
    """
    parts = []
    for err in e.errors():
        loc = '.'.join(str(x) for x in err['loc']) or '<root>'
        parts.append(f'{loc}: {err["msg"]}')
    return '; '.join(parts)


__all__ = [
    'ActsFileError',
    'LoadError',
    'LoadedSuite',
    'LoadedTest',
    'load_document',
    'load_suite',
    'parse_document',
    'render_validation_error',
]
