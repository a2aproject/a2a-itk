"""Load scenario files, in either format, from JSON or YAML.

One entry point for both front ends. ``run_tests.py`` reads files off disk;
``itk_service_v2`` gets the same structures over HTTP. Both call
:func:`parse_tests` so a file behaves identically whichever way it arrives.

Accepted inputs:

  * legacy JSON — ``{"tests": [{"name":..., "sdks":[...], ...}]}``, what
    every SDK's ``scenarios.json`` is today;
  * ``traversal/v1`` YAML or JSON — either a single scenario mapping, or a
    ``{"tests": [...]}`` wrapper around several;
  * a mixture, so a repo can migrate one scenario at a time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from test_suite.scenarios.schema import (
    LegacyScenario,
    TraversalScenarioV1,
    is_traversal_v1,
)


class ScenarioFileError(ValueError):
    """A scenario file is missing, unparseable, or fails validation."""


def load_file(path: Path) -> list[TraversalScenarioV1 | LegacyScenario]:
    """Read and validate one scenario file.

    Raises:
        ScenarioFileError: Missing file, malformed JSON/YAML, or a scenario
            that fails schema validation. The message names the file and the
            offending entry — these are almost always authoring mistakes, and
            a traceback would bury that.
    """
    if not path.is_file():
        raise ScenarioFileError(f'Scenario file not found: {path}')

    text = path.read_text(encoding='utf-8')
    try:
        # YAML 1.1 is a superset of JSON, so one parser covers both and a
        # .json file with a traversal/v1 scenario in it still works.
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ScenarioFileError(f'{path}: could not parse: {e}') from e

    if data is None:
        raise ScenarioFileError(f'{path}: file is empty')

    try:
        return parse_tests(data)
    except ScenarioFileError as e:
        raise ScenarioFileError(f'{path}: {e}') from None


def load_files(paths: list[Path]) -> list[TraversalScenarioV1 | LegacyScenario]:
    """Load several files, concatenating their scenarios in argument order."""
    scenarios: list[TraversalScenarioV1 | LegacyScenario] = []
    for p in paths:
        scenarios.extend(load_file(p))
    return scenarios


def parse_tests(data: Any) -> list[TraversalScenarioV1 | LegacyScenario]:
    """Validate an already-parsed document into scenario models.

    Args:
        data: Either a ``{"tests": [...]}`` mapping, a bare list of
            scenarios, or a single ``traversal/v1`` scenario mapping.

    Raises:
        ScenarioFileError: The document shape is wrong, or an entry fails
            validation.
    """
    raw_tests = _extract_tests(data)

    scenarios: list[TraversalScenarioV1 | LegacyScenario] = []
    for i, raw in enumerate(raw_tests):
        if not isinstance(raw, dict):
            raise ScenarioFileError(f'tests[{i}] must be a mapping')
        scenarios.append(_parse_one(raw, f'tests[{i}]'))
    return scenarios


def _extract_tests(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        raise ScenarioFileError(
            f'expected a mapping or a list, got {type(data).__name__}'
        )
    # A lone traversal/v1 scenario, unwrapped — the natural way to write a
    # one-scenario YAML file.
    if is_traversal_v1(data):
        return [data]
    tests = data.get('tests')
    if not isinstance(tests, list):
        raise ScenarioFileError('expected a top-level object with a "tests" array')
    return tests


def _parse_one(raw: dict, where: str) -> TraversalScenarioV1 | LegacyScenario:
    if is_traversal_v1(raw):
        model: type[TraversalScenarioV1 | LegacyScenario] = TraversalScenarioV1
    elif 'schema' in raw:
        raise ScenarioFileError(
            f'{where}: unknown schema {raw["schema"]!r}; '
            f'expected "traversal/v1" or no schema key for the legacy format'
        )
    else:
        model = LegacyScenario

    try:
        return model.model_validate(raw)
    except ValidationError as e:
        raise ScenarioFileError(f'{where}: {_render(e)}') from None


def _render(e: ValidationError) -> str:
    """Flatten a pydantic error into one line per problem.

    Pydantic's own rendering is several lines per error with a docs URL,
    which is noise in a CLI validator reporting a whole directory.
    """
    parts = []
    for err in e.errors():
        loc = '.'.join(str(x) for x in err['loc']) or '<root>'
        parts.append(f'{loc}: {err["msg"]}')
    return '; '.join(parts)


def dump_json(scenarios: list[Any]) -> str:
    """Serialise scenarios back to legacy JSON. For debugging a resolution."""
    return json.dumps(
        {'tests': [s.model_dump(exclude_none=True) for s in scenarios]},
        indent=2,
    )
