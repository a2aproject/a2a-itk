"""Matrix: scenario-level agent identifiers → (repo, ref).

Reads ``matrix.yaml`` from the repo root. Scenarios reference peers by
identifier — ``python_v10``, ``go_v03``, ``python_v10_2`` (``_N`` suffix
= "second instance of the same source") — and :meth:`Matrix.resolve`
translates each into a :class:`MatrixEntry` (repo + ref).

Special cases handled outside this module:

  * ``current`` — the SUT, resolved via
    :attr:`test_suite.launcher.spec.Kind.MOUNT`. Matrix rejects it.
  * ``_N`` instance suffix — the launcher's :class:`Cluster` allocates
    distinct ports per spawn, so two instances share one matrix entry.

The ref-to-SHA step (:func:`test_suite.launcher.fetch.resolve_ref`) is
kept out of matrix so tests here are pure and network-free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from test_suite.launcher.spec import Kind, TargetSpec


# `<sdk>_v<num>` with an optional `_<instance>` suffix. sdk is lowercase
# ASCII to match the file naming convention across the repo.
_AGENT_ID_RE = re.compile(r'^([a-z][a-z0-9]*)_(v[0-9]+)(?:_[0-9]+)?$')


class MatrixError(ValueError):
    """Invalid matrix.yaml, or an agent id that doesn't map to any entry."""


@dataclass(frozen=True)
class MatrixEntry:
    """One (sdk, line) → (repo, ref) row."""

    sdk: str    # 'python', 'go', 'ts', 'java', 'rust'
    line: str   # 'v10', 'v03'
    repo: str   # 'a2aproject/a2a-python'
    ref: str    # 'main', 'v0.3.24+itk', or a full SHA


class Matrix:
    """SDK version matrix loaded from YAML.

    Immutable after construction. Load once at service startup; call
    :meth:`resolve` per scenario peer.
    """

    def __init__(self, entries: dict[tuple[str, str], MatrixEntry]) -> None:
        self._entries = dict(entries)

    # -- construction --------------------------------------------------------

    @classmethod
    def from_path(cls, path: Path) -> 'Matrix':
        """Load from a YAML file. Missing file -> MatrixError."""
        if not path.is_file():
            raise MatrixError(f'matrix file not found: {path}')
        text = path.read_text(encoding='utf-8')
        data = yaml.safe_load(text) or {}
        return cls.from_dict(data)

    @classmethod
    def from_default(cls) -> 'Matrix':
        """Load from the repo-root ``matrix.yaml``."""
        return cls.from_path(_default_matrix_path())

    @classmethod
    def from_dict(cls, data: dict) -> 'Matrix':
        """Validate a parsed YAML dict and build a Matrix."""
        if not isinstance(data, dict):
            raise MatrixError(f'matrix must be a mapping; got {type(data).__name__}')
        sdks = data.get('sdks')
        if sdks is None:
            raise MatrixError('matrix missing top-level `sdks:` key')
        if not isinstance(sdks, dict):
            raise MatrixError(f'`sdks:` must be a mapping; got {type(sdks).__name__}')

        entries: dict[tuple[str, str], MatrixEntry] = {}
        for sdk, lines in sdks.items():
            if not isinstance(lines, dict):
                raise MatrixError(f'sdks.{sdk}: must be a mapping of lines')
            for line, cfg in lines.items():
                if not isinstance(cfg, dict):
                    raise MatrixError(f'sdks.{sdk}.{line}: must be a mapping with repo+ref')
                repo = cfg.get('repo')
                ref = cfg.get('ref')
                if not repo or not ref:
                    raise MatrixError(
                        f'sdks.{sdk}.{line}: needs both `repo` and `ref` '
                        f'(got repo={repo!r}, ref={ref!r})'
                    )
                if not isinstance(repo, str) or not isinstance(ref, str):
                    raise MatrixError(
                        f'sdks.{sdk}.{line}: repo and ref must be strings '
                        f'(got repo={type(repo).__name__}, ref={type(ref).__name__})'
                    )
                entries[(sdk, line)] = MatrixEntry(sdk=sdk, line=line, repo=repo, ref=ref)
        return cls(entries)

    # -- lookup --------------------------------------------------------------

    def resolve(self, agent_id: str) -> MatrixEntry:
        """Translate a scenario-level identifier to a matrix entry.

        Args:
            agent_id: Something like ``python_v10`` or ``python_v10_2``.
                The instance suffix (``_2``) is dropped; both resolve to
                the same underlying (sdk, line).

        Raises:
            MatrixError: agent_id is malformed, or the (sdk, line) pair
                is not present in matrix.yaml. Also rejects the special
                ``current`` id — MOUNT targets don't go through matrix.
        """
        if agent_id == 'current':
            raise MatrixError(
                "'current' is the SUT (Kind.MOUNT) and does not map through matrix; "
                "callers should special-case it before calling resolve()"
            )
        m = _AGENT_ID_RE.match(agent_id)
        if not m:
            raise MatrixError(
                f'invalid agent id {agent_id!r}; expected <sdk>_v<num>[_<instance>]'
            )
        sdk, line = m.group(1), m.group(2)
        try:
            return self._entries[(sdk, line)]
        except KeyError:
            known = ', '.join(f'{s}_{ln}' for s, ln in sorted(self._entries))
            raise MatrixError(
                f'unknown agent {agent_id!r} (sdk={sdk!r}, line={line!r}); '
                f'matrix.yaml has: {known}'
            ) from None

    def make_spec(self, agent_id: str, sha: str) -> TargetSpec:
        """Convenience: matrix entry + a resolved SHA → a CHECKOUT TargetSpec.

        The caller is responsible for resolving the entry's ``ref`` to a
        SHA via :func:`test_suite.launcher.fetch.resolve_ref` — matrix
        stays network-free.
        """
        if agent_id == 'current':
            return TargetSpec(kind=Kind.MOUNT)
        entry = self.resolve(agent_id)
        return TargetSpec(kind=Kind.CHECKOUT, repo=entry.repo, sha=sha)

    def keys(self) -> list[tuple[str, str]]:
        """All (sdk, line) pairs, sorted. Useful for CI diagnostics."""
        return sorted(self._entries.keys())

    def __contains__(self, agent_id: str) -> bool:
        try:
            self.resolve(agent_id)
        except MatrixError:
            return False
        return True

    def __len__(self) -> int:
        return len(self._entries)


def _default_matrix_path() -> Path:
    """Repo-root ``matrix.yaml`` — sibling of ``Dockerfile``."""
    return Path(__file__).resolve().parents[2] / 'matrix.yaml'
