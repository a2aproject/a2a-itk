"""Public launcher entry points: :func:`resolve`, :func:`spawn`, :class:`LaunchSession`.

Dispatches a :class:`~v2.launcher.spec.TargetSpec` to one of three concrete
locations:

  * ``MOUNT``    — the fixed container-mount path ``agents/repo/itk``.
  * ``LOCAL``    — a baked baseline at ``agents/<sdk>/<line>``.
  * ``CHECKOUT`` — fetch + build under the on-disk cache; :mod:`.cache`
                   owns the concurrency guarantees.

:class:`LaunchSession` is the context manager the runner uses to acquire pins
on a batch of specs and release them on exit — the design's ``release()``
needs an owner, and this is it. Story 1.9 will extend it to own process
groups and dynamic port ranges.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

from v2.launcher import cache
from v2.launcher import spawn as _spawn_mod
from v2.launcher.spec import Kind, TargetSpec


def _repo_root() -> Path:
    """a2a-itk root; matches :func:`v2.launcher.config._repo_root`."""
    return Path(__file__).resolve().parents[2]


def resolve(spec: TargetSpec) -> Path:
    """Return the built agent directory for ``spec``.

    For :attr:`Kind.CHECKOUT`, this fetches and builds if necessary and pins
    the cache slot for the current process. The caller is responsible for
    releasing the pin (use :class:`LaunchSession`).

    Raises:
        RuntimeError: MOUNT target has not been mounted into the container.
        v2.launcher.errors.InfraFailure: CHECKOUT fetch/build failed after
            retries.
        v2.launcher.errors.PermanentError: CHECKOUT SHA does not exist on
            the remote.
    """
    if spec.kind is Kind.MOUNT:
        d = _repo_root() / 'agents' / 'repo' / 'itk'
        if not d.exists():
            raise RuntimeError(
                'current agent has not been mounted and is not available to test'
            )
        return d

    if spec.kind is Kind.LOCAL:
        assert spec.sdk is not None and spec.line is not None
        return _repo_root() / 'agents' / spec.sdk / spec.line

    if spec.kind is Kind.CHECKOUT:
        assert spec.repo is not None and spec.sha is not None
        return cache.checkout_and_build(spec.repo, spec.sha)

    raise ValueError(f'unknown kind: {spec.kind!r}')  # pragma: no cover


def spawn(
    spec: TargetSpec,
    http_port: int,
    grpc_port: int,
    *,
    log_dir: Path | None = None,
) -> subprocess.Popen:
    """Resolve ``spec`` then spawn its agent."""
    agent_dir = resolve(spec)
    return _spawn_mod.spawn_from_dir(agent_dir, http_port, grpc_port, log_dir=log_dir)


class LaunchSession(contextlib.AbstractContextManager):
    """Own the cache pins AND log-file handles for a batch of specs.

    Usage::

        with LaunchSession() as sess:
            for spec in plan:
                sess.spawn(spec, http, grpc, log_dir=Path('logs'))
            # ... run tests ...
        # pins released, log handles closed — even on error.

    Only CHECKOUT specs need release; MOUNT and LOCAL are cheap no-ops.
    Story 1.9 will grow this into a full cluster lifecycle owner (process
    groups, ports, teardown-on-timeout).
    """

    def __init__(self) -> None:
        self._pinned: list[tuple[str, str]] = []          # (repo, sha)
        self._spawned: list[subprocess.Popen] = []        # for log-handle cleanup

    def resolve(self, spec: TargetSpec) -> Path:
        """Resolve ``spec`` and remember any pin we took."""
        p = resolve(spec)
        if spec.kind is Kind.CHECKOUT:
            assert spec.repo is not None and spec.sha is not None
            self._pinned.append((spec.repo, spec.sha))
        return p

    def spawn(
        self,
        spec: TargetSpec,
        http_port: int,
        grpc_port: int,
        *,
        log_dir: Path | None = None,
        log_name: str | None = None,
    ) -> subprocess.Popen:
        agent_dir = self.resolve(spec)
        proc = _spawn_mod.spawn_from_dir(
            agent_dir, http_port, grpc_port,
            log_dir=log_dir, log_name=log_name,
        )
        self._spawned.append(proc)
        return proc

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: D401
        # Close log handles first — closing our copy is safe even while the
        # child is running because the child holds its own dup'd fd.
        for proc in self._spawned:
            log_file = getattr(proc, '_log_file', None)
            if log_file is not None:
                with contextlib.suppress(Exception):
                    log_file.close()
        self._spawned.clear()

        for repo, sha in self._pinned:
            with contextlib.suppress(Exception):
                cache.release(repo, sha)
        self._pinned.clear()
