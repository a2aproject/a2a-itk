"""Target specification: how the launcher describes what to build and spawn.

A ``TargetSpec`` names one of three things:

  * ``MOUNT``   — the code under test, mounted into the container at
                   ``agents/repo/itk``. This is the SUT / ``current``.
  * ``LOCAL``   — a baked baseline at ``agents/<sdk>/<line>``. Kept as a
                   degenerate reference for the strangler window; disappears
                   once ``agents/`` is deleted at the end of the migration.
  * ``CHECKOUT`` — a peer SDK fetched from GitHub at a specific commit SHA.
                   This is the new capability the launcher unlocks.

The SHA on a ``CHECKOUT`` MUST be a resolved 40-hex commit. Passing a symbolic
ref (``main``, a tag) is a fail-fast configuration error. Refs are resolved to
SHAs once per run at plan time so intra-run drift of a moving ref cannot mix
versions across peers.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass


_SHA_RE = re.compile(r'^[0-9a-f]{40}$')
_SDK_RE = re.compile(r'^[a-z][a-z0-9_-]*$')
_LINE_RE = re.compile(r'^v[0-9]+$')
_REPO_RE = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')


class Kind(enum.Enum):
    """Where the agent source comes from."""

    MOUNT = 'mount'
    LOCAL = 'local'
    CHECKOUT = 'checkout'


@dataclass(frozen=True)
class TargetSpec:
    """One launch target.

    Fields required per kind:
      * ``MOUNT``    — nothing (the mount path is fixed by the container contract).
      * ``LOCAL``    — ``sdk`` and ``line``.
      * ``CHECKOUT`` — ``sdk``, ``repo`` and a resolved 40-hex ``sha``.

    Symbolic refs are rejected on construction — resolve them at plan time.
    """

    kind: Kind
    sdk: str | None = None
    line: str | None = None
    repo: str | None = None
    sha: str | None = None

    def __post_init__(self) -> None:
        if self.kind is Kind.MOUNT:
            self._check_absent('sdk', 'line', 'repo', 'sha')
            return

        if self.kind is Kind.LOCAL:
            self._check_present('sdk', 'line')
            self._check_absent('repo', 'sha')
            self._check_sdk()
            self._check_line()
            return

        if self.kind is Kind.CHECKOUT:
            self._check_present('sdk', 'repo', 'sha')
            self._check_absent('line')  # optional in future, unused today
            self._check_sdk()
            self._check_repo()
            self._check_sha()
            return

        raise ValueError(f'unknown kind: {self.kind!r}')  # pragma: no cover

    # -- validators ----------------------------------------------------------

    def _check_present(self, *fields: str) -> None:
        for f in fields:
            if getattr(self, f) is None:
                raise ValueError(f'{self.kind.value} spec requires {f!r}')

    def _check_absent(self, *fields: str) -> None:
        for f in fields:
            if getattr(self, f) is not None:
                raise ValueError(f'{self.kind.value} spec must not set {f!r}')

    def _check_sdk(self) -> None:
        assert self.sdk is not None
        if not _SDK_RE.match(self.sdk):
            raise ValueError(
                f'invalid sdk {self.sdk!r} (must match {_SDK_RE.pattern})'
            )

    def _check_line(self) -> None:
        assert self.line is not None
        if not _LINE_RE.match(self.line):
            raise ValueError(
                f'invalid line {self.line!r} (must match {_LINE_RE.pattern})'
            )

    def _check_repo(self) -> None:
        assert self.repo is not None
        if not _REPO_RE.match(self.repo):
            raise ValueError(
                f'invalid repo {self.repo!r} (expected "owner/name")'
            )

    def _check_sha(self) -> None:
        assert self.sha is not None
        if not _SHA_RE.match(self.sha):
            raise ValueError(
                f'invalid sha {self.sha!r}: expected a resolved 40-hex commit; '
                f'symbolic refs (main, tags, short SHAs) must be resolved via '
                f'git ls-remote at plan time'
            )

    # -- convenience ---------------------------------------------------------

    def cache_slug(self) -> str:
        """Filesystem-safe slug for :mod:`v2.launcher.cache` keys.

        Only meaningful for :attr:`Kind.CHECKOUT`. Callers must check the kind.
        """
        if self.kind is not Kind.CHECKOUT:
            raise ValueError(f'cache_slug not defined for {self.kind.value} spec')
        assert self.repo is not None and self.sha is not None
        safe_repo = self.repo.replace('/', '_')
        return f'{safe_repo}@{self.sha}'
