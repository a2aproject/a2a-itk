"""What the shared runner invokes must actually be in the image.

A helper under a ``.dockerignore``-excluded path but invoked with
``docker exec`` is missing at run time, and surfaces two steps later as a
FastAPI 422 about a missing request field. That happened once.

Pure text inspection: no Docker needed, so it runs in CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DOCKERIGNORE = ROOT / '.dockerignore'
SHARED_RUNNER = ROOT / 'scripts' / 'run_itk_shared.sh'


def excluded_paths() -> set[str]:
    """Top-level paths .dockerignore keeps out of the build context."""
    out = set()
    for raw in DOCKERIGNORE.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or line.startswith('**/'):
            continue
        out.add(line.strip('/'))
    return out


class TestDockerignore:
    def test_scripts_is_excluded(self):
        """The premise of the test below. If this ever changes, the rule it
        enforces stops being necessary — but check deliberately, don't drift."""
        assert 'scripts' in excluded_paths()

    def test_scenarios_and_test_suite_are_included(self):
        """The service imports test_suite and reads the shared scenario sets
        out of the image."""
        excluded = excluded_paths()
        assert 'test_suite' not in excluded
        assert 'scenarios' not in excluded


def _logical_lines(text: str) -> list[str]:
    """Shell source with backslash continuations joined into single lines."""
    return re.sub(r'\\\n\s*', ' ', text).splitlines()


def _exec_commands() -> list[str]:
    """Every container-side command in the shared runner."""
    return [
        line for line in _logical_lines(SHARED_RUNNER.read_text(encoding='utf-8'))
        if re.search(r'\bexec\b', line) and 'itk-service' in line
    ]


class TestSharedRunnerInvocations:
    """Anything run with `exec ... itk-service` must be in the image."""

    def _in_container_python_targets(self) -> list[str]:
        # `$CONTAINER_RT exec ... uv run python -m <module>` — the module form
        # is the supported one; a bare path is caught by the test below.
        return [
            m.group(1)
            for line in _exec_commands()
            if (m := re.search(r'python\s+-m\s+([\w.]+)', line))
        ]

    def test_at_least_one_in_container_call_exists(self):
        """Guards the regex: if the call is reworded, this test must be too,
        rather than silently passing on an empty list."""
        assert self._in_container_python_targets()

    def test_every_in_container_module_is_shipped(self):
        excluded = excluded_paths()
        for module in self._in_container_python_targets():
            top = module.split('.')[0]
            assert top not in excluded, (
                f'run_itk_shared.sh runs `python -m {module}` inside the '
                f'container, but .dockerignore excludes {top!r}, so it is not '
                f'in the image'
            )
            path = ROOT / Path(*module.split('.')).with_suffix('.py')
            assert path.is_file(), f'{module} does not exist at {path}'

    def test_no_in_container_call_uses_a_scripts_path(self):
        """The specific mistake: `docker exec ... scripts/foo.py`."""
        for line in _exec_commands():
            assert 'scripts/' not in line, (
                f'a container-side command references scripts/, which '
                f'.dockerignore keeps out of the image:\n  {line.strip()}'
            )


class TestHostSideScriptsAreStdlibOnly:
    """Host-side scripts run under a bare `python3` with no venv.

    `uv` is not installed on every SDK's runner, so a third-party import here
    would fail on whichever repo happens not to have it.
    """

    THIRD_PARTY = ('yaml', 'pydantic', 'httpx', 'fastapi', 'uvicorn')

    @pytest.mark.parametrize('name', ['itk_report.py', 'process_results.py'])
    def test_no_third_party_imports(self, name):
        text = (ROOT / 'scripts' / name).read_text(encoding='utf-8')
        imports = set(re.findall(r'^\s*(?:import|from)\s+(\w+)', text, re.M))
        offenders = imports & set(self.THIRD_PARTY)
        assert not offenders, (
            f'scripts/{name} runs on the host under a bare python3 but '
            f'imports {sorted(offenders)}'
        )
