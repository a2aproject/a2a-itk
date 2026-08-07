"""Target specification: how the launcher describes what to build and spawn.

A ``TargetSpec`` names one of two things:

  * ``MOUNT``    — the code under test, mounted into the container at
                   ``agents/repo/itk``. This is the SUT / ``current``.
  * ``CHECKOUT`` — a peer SDK fetched from GitHub at a specific commit SHA.
                   The new capability the launcher unlocks.

The SHA on a ``CHECKOUT`` MUST be a resolved 40-hex commit. Passing a symbolic
ref (``main``, a tag) is a fail-fast configuration error. Refs are resolved to
SHAs once per run at plan time so intra-run drift of a moving ref cannot mix
versions across peers.

There is intentionally no ``LOCAL`` kind for baked baselines. During the
strangler window we run the untouched legacy pipeline in parallel with this
one (the S1 comparison harness diffs the two outputs). Introducing a
launcher-based path for baselines would defeat the whole point of the
comparison — it would test the new spawn/cache/codegen against itself.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass


_SHA_RE = re.compile(r'^[0-9a-f]{40}$')
_REPO_RE = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')


class Kind(enum.Enum):
    """Where the agent source comes from."""

    MOUNT = 'mount'
    CHECKOUT = 'checkout'


@dataclass(frozen=True)
class TargetSpec:
    """One launch target.

    Fields required per kind:
      * ``MOUNT``    — nothing (the mount path is fixed by the container contract).
      * ``CHECKOUT`` — ``repo`` and a resolved 40-hex ``sha``.

    Symbolic refs are rejected on construction — resolve them at plan time.
    """

    kind: Kind
    repo: str | None = None
    sha: str | None = None

    def __post_init__(self) -> None:
        if self.kind is Kind.MOUNT:
            self._check_absent('repo', 'sha')
            return

        if self.kind is Kind.CHECKOUT:
            self._check_present('repo', 'sha')
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
        """Filesystem-safe slug for :mod:`test_suite.launcher.cache` keys.

        Only meaningful for :attr:`Kind.CHECKOUT`.
        """
        if self.kind is not Kind.CHECKOUT:
            raise ValueError(f'cache_slug not defined for {self.kind.value} spec')
        assert self.repo is not None and self.sha is not None
        safe_repo = self.repo.replace('/', '_')
        return f'{safe_repo}@{self.sha}'
