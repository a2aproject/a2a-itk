"""Cluster: batch spawn + parallel readiness + safe teardown.

The launcher's higher-level context manager. Owns everything the runner
needs for a single ``/run`` request:

  * Cache pins (via :func:`test_suite.launcher.cache.checkout_and_build` /
    :func:`~test_suite.launcher.cache.release`).
  * Dynamic port pair allocation (via :mod:`.ports`), safe under concurrent
    runners on the same host.
  * Log-file handle lifecycle (matching :class:`LaunchSession`).
  * Process-group ownership — every agent Cluster spawns runs in its own
    POSIX session (Cluster passes ``new_session=True`` to
    :func:`test_suite.current.spawn_from_dir`), and teardown signals the
    whole group so grandchildren (mvn -> java, npm -> tsx, go run ->
    compiled binary) don't leak.
  * Parallel readiness gating with per-target outcome reporting so
    partial-startup failure is diagnosable — "which specific peer didn't
    come up" — not a blanket "cluster failed".

Usage::

    with Cluster() as cluster:
        outcomes = cluster.start_all(plan)          # spawn in parallel
        for outcome in outcomes:
            if not outcome.ok():
                print(f'peer {outcome.spec} failed: {outcome.error}')
                continue
            handle = outcome.handle                 # has http_port, grpc_port, pid
            # ... run tests against handle.http_port ...
    # SIGTERM/SIGKILL every agent's pgroup, close log handles,
    # release cache pins, return ports to the reservoir — even on error.
"""

from __future__ import annotations

import contextlib
import errno
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from test_suite import current as _current
from test_suite.launcher import cache, config, health, ports
from test_suite.launcher.errors import (
    InfraFailure,
    LauncherError,
    PermanentError,
    Stage,
)
from test_suite.launcher.spec import Kind, TargetSpec


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentHandle:
    """A live agent the cluster owns.

    Callers read ``http_port`` / ``grpc_port`` to build client URIs. Never
    hold ``proc`` past ``__exit__`` — the cluster kills the process group
    on teardown and this Popen becomes invalid.
    """

    spec: TargetSpec
    http_port: int
    grpc_port: int
    pid: int              # equals pgid because spawn uses start_new_session
    proc: subprocess.Popen


@dataclass(frozen=True)
class StartOutcome:
    """Result of one agent's startup attempt.

    Exactly one of ``handle`` / ``error`` is set. Callers check with
    :meth:`ok` before reading ``handle``.
    """

    spec: TargetSpec
    handle: AgentHandle | None
    error: LauncherError | None
    elapsed_s: float

    def ok(self) -> bool:
        return self.handle is not None


# ---------------------------------------------------------------------------
# Cluster
# ---------------------------------------------------------------------------


class Cluster(contextlib.AbstractContextManager):
    """Own the lifecycle of a batch of agents.

    Not thread-safe for concurrent ``add()`` on the same instance — use
    :meth:`start_all` for parallel batch spawn instead.
    """

    def __init__(
        self,
        *,
        log_dir: Path | None = None,
        readiness_timeout_s: int | None = None,
        teardown_grace_s: int | None = None,
    ) -> None:
        self._log_dir = log_dir
        self._readiness_timeout = (
            readiness_timeout_s
            if readiness_timeout_s is not None
            else config.readiness_timeout()
        )
        self._teardown_grace = (
            teardown_grace_s
            if teardown_grace_s is not None
            else config.teardown_grace()
        )
        # Per-Cluster reservoir (deliberate, not the module-level default).
        # Kernel-level bind(0) contention already prevents two live Clusters
        # from binding the same port; the reservoir is a within-call-site
        # belt so `allocate_pair`'s two `_one_free_port` calls don't return
        # the same port. Keeping the reservoir per-Cluster means a leaked
        # (never __exit__'d) Cluster's reservations can't poison the next
        # Cluster's port pool.
        self._reservoir = ports.AddressReservoir()

        # Everything below is filled during add()/start_all() and drained
        # in __exit__. Serialised by _mutex to keep concurrent start_all()
        # workers from clobbering each other's bookkeeping.
        self._mutex = threading.Lock()
        self._handles: list[AgentHandle] = []
        self._pinned: list[tuple[str, str]] = []
        self._log_handles: list = []
        self._allocated_ports: list[int] = []

    # -- single-target API --------------------------------------------------

    def add(
        self,
        spec: TargetSpec,
        *,
        log_name: str | None = None,
    ) -> AgentHandle:
        """Resolve, spawn, and wait for readiness for one target.

        Raises the underlying :class:`LauncherError` on failure. For a
        version that returns per-target outcomes instead of raising, use
        :meth:`start_all`.
        """
        outcome = self._start_one(spec, log_name=log_name)
        if outcome.error is not None:
            raise outcome.error
        assert outcome.handle is not None
        return outcome.handle

    # -- batch API ----------------------------------------------------------

    def start_all(
        self,
        specs: list[TargetSpec],
        *,
        log_names: list[str | None] | None = None,
        max_workers: int | None = None,
    ) -> list[StartOutcome]:
        """Spawn every spec in parallel; wait for all readiness.

        Args:
            specs: What to start. The same spec may appear more than once —
                two scenario peers can share a (repo, sha), e.g.
                ``python_v10`` and ``python_v10_2``.
            log_names: Log basenames, positionally aligned with ``specs``.
                Positional, not keyed by spec: ``TargetSpec`` is a frozen
                dataclass, so duplicate specs compare equal and a dict would
                silently collapse them onto one log file — losing exactly
                the distinction you need when two instances of one SDK are
                talking to each other.
            max_workers: Parallelism cap; defaults to ``ITK_MAX_WORKERS``.

        Order of returned outcomes matches ``specs``. Failures are captured
        per-target so the caller can decide whether to run scenarios with a
        partial cluster or abort. The returned list is always the same
        length as ``specs`` — callers can safely ``zip(specs, outcomes)``.

        Raises:
            ValueError: ``log_names`` was given but is a different length
                than ``specs``, which would misattribute agent logs.
        """
        if not specs:
            return []
        if log_names is not None and len(log_names) != len(specs):
            raise ValueError(
                f'log_names has {len(log_names)} entries but there are '
                f'{len(specs)} specs; they must line up positionally'
            )
        if max_workers is None:
            # ITK_MAX_WORKERS (see config.max_workers) overrides the default
            # for resource-constrained CI runners; otherwise scale to
            # len(specs) but never below 4 so small clusters stay parallel.
            max_workers = config.max_workers()
        workers = max_workers if max_workers is not None else max(4, len(specs))

        outcomes: list[StartOutcome | None] = [None] * len(specs)

        def worker(i: int) -> None:
            name = log_names[i] if log_names is not None else None
            outcomes[i] = self._start_one(specs[i], log_name=name)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            for _ in executor.map(worker, range(len(specs))):
                pass

        # `_start_one` catches every exception and always returns; a None
        # slot means the worker itself was cancelled before running (only
        # possible under shutdown). Fill any such slot with a synthetic
        # failure so `zip(specs, outcomes)` at the call site stays sound.
        for i, o in enumerate(outcomes):
            if o is None:
                outcomes[i] = StartOutcome(
                    spec=specs[i], handle=None,
                    error=InfraFailure(
                        specs[i].repo, specs[i].sha, Stage.SPAWN,
                        message='start_all worker did not run to completion',
                    ),
                    elapsed_s=0.0,
                )
        return [o for o in outcomes if o is not None]  # type: ignore[misc]

    # -- internals ----------------------------------------------------------

    def _start_one(
        self, spec: TargetSpec, *, log_name: str | None,
    ) -> StartOutcome:
        """Resolve + spawn + wait-ready for one target, catching every error."""
        t0 = time.monotonic()
        try:
            # 1. Resolve (fetches + builds CHECKOUT; pins under our name)
            agent_dir = self._resolve_and_pin(spec)
        except LauncherError as e:
            return StartOutcome(spec=spec, handle=None, error=e,
                                elapsed_s=time.monotonic() - t0)

        # 2. Allocate ports
        try:
            http, grpc = ports.allocate_pair(self._reservoir)
        except RuntimeError as e:
            err = InfraFailure(
                spec.repo, spec.sha, Stage.SPAWN,
                message=f'port allocation failed: {e}',
            )
            return StartOutcome(spec=spec, handle=None, error=err,
                                elapsed_s=time.monotonic() - t0)
        with self._mutex:
            self._allocated_ports.extend((http, grpc))

        # 3. Spawn — Cluster wants pgroup teardown, so opt into new_session.
        try:
            proc = _current.spawn_from_dir(
                agent_dir, http, grpc,
                log_dir=self._log_dir, log_name=log_name,
                new_session=True,
            )
        except RuntimeError as e:
            # spawn_from_dir raises RuntimeError only for unrecoverable
            # configuration problems (missing agent dir, unknown language,
            # cargo built but produced no binary). Retrying won't change
            # the outcome — surface as PermanentError so the runner doesn't
            # burn its retry budget.
            err = PermanentError(spec.repo, spec.sha, Stage.SPAWN, str(e))
            return StartOutcome(spec=spec, handle=None, error=err,
                                elapsed_s=time.monotonic() - t0)
        except (OSError, subprocess.SubprocessError) as e:
            # Binary not on PATH, permission denied, cargo build call itself
            # failed — these can be transient (mirror in outage, disk full)
            # so let the runner retry.
            err = InfraFailure(spec.repo, spec.sha, Stage.SPAWN, cause=e)
            return StartOutcome(spec=spec, handle=None, error=err,
                                elapsed_s=time.monotonic() - t0)

        with self._mutex:
            log_handle = getattr(proc, '_log_file', None)
            if log_handle is not None:
                self._log_handles.append(log_handle)

        # 4. Wait ready — poll agent card. A crashed child during startup
        # also flunks readiness naturally (URL never responds 200).
        ready, elapsed = health.wait_ready(http, timeout_s=self._readiness_timeout)
        if not ready:
            # Kill this one; keep pins/log handles so __exit__ still cleans up.
            _kill_pgroup(proc, self._teardown_grace)
            err = InfraFailure(
                spec.repo, spec.sha, Stage.READY,
                message=(
                    f'agent did not respond 200 at '
                    f'{health.agent_card_url(http)} within '
                    f'{self._readiness_timeout}s (proc exit={proc.poll()})'
                ),
            )
            return StartOutcome(spec=spec, handle=None, error=err,
                                elapsed_s=time.monotonic() - t0)

        handle = AgentHandle(
            spec=spec,
            http_port=http,
            grpc_port=grpc,
            pid=proc.pid,
            proc=proc,
        )
        with self._mutex:
            self._handles.append(handle)
        return StartOutcome(spec=spec, handle=handle, error=None,
                            elapsed_s=elapsed)

    def _resolve_and_pin(self, spec: TargetSpec) -> Path:
        """Resolve the spec, recording any pin we take."""
        if spec.kind is Kind.CHECKOUT:
            assert spec.repo is not None and spec.sha is not None
            agent_dir = cache.checkout_and_build(spec.repo, spec.sha)
            with self._mutex:
                self._pinned.append((spec.repo, spec.sha))
            return agent_dir
        # MOUNT — no pin needed; use the resolver from resolve.py for the
        # canonical mount-missing error.
        from test_suite.launcher.resolve import resolve as _resolve
        return _resolve(spec)

    # -- teardown -----------------------------------------------------------

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: D401
        # Every step is wrapped in suppress: a single agent's teardown
        # blowing up (e.g. killpg racing with an external kill -9, wait()
        # raising OSError on a reparented process) must not skip cleanup
        # for the rest of the handles/logs/pins/ports.

        # 1. Kill every agent's process group.
        for h in self._handles:
            with contextlib.suppress(Exception):
                _kill_pgroup(h.proc, self._teardown_grace)
        self._handles.clear()

        # 2. Close every log-file handle we opened.
        for log_file in self._log_handles:
            with contextlib.suppress(Exception):
                log_file.close()
        self._log_handles.clear()

        # 3. Release cache pins.
        for repo, sha in self._pinned:
            with contextlib.suppress(Exception):
                cache.release(repo, sha)
        self._pinned.clear()

        # 4. Return ports so the reservoir doesn't grow unbounded across
        # multiple sequential Cluster() instances in one process.
        for p in self._allocated_ports:
            with contextlib.suppress(Exception):
                ports.release(p, reservoir=self._reservoir)
        self._allocated_ports.clear()


# ---------------------------------------------------------------------------
# Process-group teardown
# ---------------------------------------------------------------------------


def _kill_pgroup(proc: subprocess.Popen, grace_s: int) -> None:
    """SIGTERM the process group, wait up to grace_s, SIGKILL if still alive.

    ``spawn_from_dir`` uses ``start_new_session=True`` so ``proc.pid`` is
    also the pgid. Signalling the group catches grandchildren
    (mvn -> java, npm -> tsx, cargo -> compiled binary) that would
    otherwise leak and keep ports bound.
    """
    if proc.poll() is not None:
        return

    pgid = proc.pid  # start_new_session guarantees pid == pgid
    _try_killpg(pgid, signal.SIGTERM)
    try:
        proc.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass

    _try_killpg(pgid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.wait(timeout=grace_s)


def _try_killpg(pgid: int, sig: int) -> None:
    """Best-effort ``os.killpg`` — swallow the errors that can happen normally.

    ESRCH means the group is already gone (child exited between our
    ``poll()`` and the ``killpg``). EPERM means we can't signal the group
    (very unusual — should not happen for our own child but defended
    against just in case).
    """
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        return
    except OSError as e:
        if e.errno not in (errno.ESRCH, errno.EPERM):
            raise
