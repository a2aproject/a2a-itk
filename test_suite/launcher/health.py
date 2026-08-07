"""Readiness gate: poll ``/.well-known/agent-card.json`` until 200.

Kept dependency-free (stdlib ``urllib.request`` only) so the launcher can
stay freestanding — no httpx / grpcio / a2a-sdk import chain just to
check whether an agent is up. Same 35s default as
``testlib._check_agent_ready`` so behaviour matches the legacy pipeline.
"""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request


_AGENT_CARD_PATH = '/.well-known/agent-card.json'


def agent_card_url(http_port: int, host: str = '127.0.0.1') -> str:
    """Well-known URL every ITK-compliant agent must serve."""
    return f'http://{host}:{http_port}{_AGENT_CARD_PATH}'


def wait_ready(
    http_port: int,
    *,
    timeout_s: int,
    host: str = '127.0.0.1',
    poll_interval_s: float = 1.0,
    _now=time.monotonic,
    _sleep=time.sleep,
) -> tuple[bool, float]:
    """Poll the agent-card URL until 200 or ``timeout_s`` elapses.

    Args:
        http_port: HTTP port the agent listens on.
        timeout_s: Max seconds to wait.
        host: Host to hit; defaults to loopback.
        poll_interval_s: Sleep between failed attempts.
        _now, _sleep: Injected for tests; leave defaults in production.

    Returns:
        ``(ready, elapsed_seconds)``. ``ready`` is True iff the URL returned
        200 within the timeout.
    """
    url = agent_card_url(http_port, host=host)
    deadline = _now() + timeout_s
    while True:
        elapsed = _now() - (deadline - timeout_s)
        try:
            with urllib.request.urlopen(url, timeout=1) as r:  # noqa: S310
                if r.status == 200:
                    return True, elapsed
        except (urllib.error.URLError, ConnectionError, socket.timeout, OSError):
            pass
        # Check deadline AFTER the attempt so an agent that becomes ready
        # exactly at the deadline still counts as ready.
        if _now() >= deadline:
            return False, elapsed
        _sleep(poll_interval_s)
