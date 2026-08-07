"""Dynamic port allocation for concurrent-safe cluster startup.

:func:`allocate_pair` returns two distinct free ports from the OS ephemeral
range. Selection uses ``bind(0)`` + ``close()`` — the kernel arbitrates
against other bind-0 callers on the same host, so two concurrent runners
never see the same pair even without a reservation table.

There is a small race window between our ``close()`` and the agent's own
``bind()``: another process could grab the port in that gap. In practice
this bites almost never — agents come up in seconds, the ephemeral range
is thousands of ports wide, and the launcher does not run alongside other
port-hungry services on the same host. If it ever does bite,
:class:`test_suite.launcher.cluster.Cluster` catches the resulting
``EADDRINUSE`` at readiness time and reports it as an :class:`InfraFailure`
with ``Stage.SPAWN`` — retryable by the runner.

For per-process re-allocation avoidance, :class:`AddressReservoir` tracks
ports handed out this process, so two calls to ``allocate_pair()`` in one
Python process never return overlapping ports even if the kernel happens
to hand out the same port twice.
"""

from __future__ import annotations

import socket
import threading


class AddressReservoir:
    """In-process bookkeeping so two allocate_pair() calls never overlap.

    The kernel usually doesn't repeat a port until pressure forces reuse,
    but under fast concurrent allocation in a busy container it can happen.
    This reservoir is a belt to that suspenders.
    """

    def __init__(self) -> None:
        self._used: set[int] = set()
        self._lock = threading.Lock()

    def take(self, port: int) -> None:
        with self._lock:
            self._used.add(port)

    def contains(self, port: int) -> bool:
        with self._lock:
            return port in self._used

    def release(self, port: int) -> None:
        with self._lock:
            self._used.discard(port)

    def clear(self) -> None:
        with self._lock:
            self._used.clear()


_default_reservoir = AddressReservoir()


def _one_free_port(reservoir: AddressReservoir) -> int:
    """Return one OS-picked ephemeral port not previously handed out."""
    # Bounded retry — realistic collisions with the reservoir are extremely
    # rare, so 100 attempts is huge headroom.
    for _ in range(100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('', 0))
            port = s.getsockname()[1]
        if not reservoir.contains(port):
            reservoir.take(port)
            return port
    raise RuntimeError(
        'could not find a free port not already reserved by this process '
        'after 100 attempts'
    )


def allocate_pair(reservoir: AddressReservoir | None = None) -> tuple[int, int]:
    """Allocate two distinct free ports for an agent's (http, grpc) pair.

    Args:
        reservoir: Optional custom reservoir; the module-level default is
            usually right. Injectable for tests.

    Returns:
        ``(http_port, grpc_port)``, guaranteed distinct.

    Raises:
        RuntimeError: The reservoir is so full that :func:`_one_free_port`
            couldn't find a free port in 100 attempts. If the first call
            succeeded and the second raised, the first port is released
            back to the reservoir before propagating — no bookkeeping leak.
    """
    r = reservoir if reservoir is not None else _default_reservoir
    http = _one_free_port(r)
    try:
        grpc = _one_free_port(r)
    except BaseException:
        # Roll back the http reservation so a caller who catches and
        # retries doesn't slowly starve the reservoir.
        r.release(http)
        raise
    # `_one_free_port` already excludes ports the reservoir has seen this
    # process, so http != grpc as long as the reservoir was consulted
    # between the two calls (it was, above).
    assert http != grpc, f'reservoir handed out {http} twice'
    return http, grpc


def release(*ports: int, reservoir: AddressReservoir | None = None) -> None:
    """Return ports to the reservoir so they can be reused."""
    r = reservoir if reservoir is not None else _default_reservoir
    for p in ports:
        r.release(p)
