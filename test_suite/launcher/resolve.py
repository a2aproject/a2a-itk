"""Public launcher entry points: :func:`resolve`, :func:`spawn`, :class:`LaunchSession`.

Dispatches a :class:`~test_suite.launcher.spec.TargetSpec` to one of two concrete
locations:

  * ``MOUNT``    — :func:`test_suite.launcher.config.mount_dir`, i.e. the
                   container mount at ``agents/repo/itk`` unless
                   ``$ITK_MOUNT_DIR`` overrides it.
  * ``CHECKOUT`` — fetch + build under the on-disk cache; :mod:`.cache`
                   owns the concurrency guarantees.

:class:`LaunchSession` is the simple single-spawn context manager (pins +
log-handle cleanup). For batch orchestration of a whole cluster with dynamic
port allocation, parallel readiness gating, and process-group teardown, use
:class:`test_suite.launcher.cluster.Cluster` instead.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

from test_suite import current as _current
from test_suite.launcher import cache, config
from test_suite.launcher.spec import Kind, TargetSpec


def resolve(spec: TargetSpec) -> Path:
    """Return the built agent directory for ``spec``.

    For :attr:`Kind.CHECKOUT`, this fetches and builds if necessary and pins
    the cache slot for the current process. The caller is responsible for
    releasing the pin (use :class:`LaunchSession` or
    :class:`~test_suite.launcher.cluster.Cluster`).

    Raises:
        RuntimeError: MOUNT target is not present at
            :func:`test_suite.launcher.config.mount_dir`.
        test_suite.launcher.errors.InfraFailure: CHECKOUT fetch/build failed
            after retries.
        test_suite.launcher.errors.PermanentError: CHECKOUT SHA does not exist
            on the remote.
    """
    if spec.kind is Kind.MOUNT:
        d = config.mount_dir()
        if not d.exists():
            raise RuntimeError(
                'current agent has not been mounted and is not available to test'
            )
        return d

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
    log_name: str | None = None,
) -> subprocess.Popen:
    """Resolve ``spec`` then spawn its agent.

    Uses :func:`test_suite.current.spawn_from_dir`, the same polyglot
    implementation :class:`~test_suite.launcher.cluster.Cluster` calls, so
    the SUT and its peers can't drift in how they're started.
    """
    agent_dir = resolve(spec)
    return _current.spawn_from_dir(
        agent_dir, http_port, grpc_port,
        log_dir=log_dir, log_name=log_name,
    )


class LaunchSession(contextlib.AbstractContextManager):
    """Own the cache pins AND log-file handles for a single-spawn scenario.

    Usage::

        with LaunchSession() as sess:
            proc = sess.spawn(spec, http, grpc, log_dir=Path('logs'))
            # ... use proc ...
        # pin released, log handle closed — even on error.

    For multi-agent runs, prefer :class:`test_suite.launcher.cluster.Cluster`
    which additionally owns dynamic port allocation, parallel readiness
    gating, and process-group teardown.
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
        proc = _current.spawn_from_dir(
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
