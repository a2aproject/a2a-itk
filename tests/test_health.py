"""Health-check readiness gate.

We run a real stdlib HTTPServer in a background thread so wait_ready hits
actual sockets — no urllib mocking, no need for httpx.
"""

from __future__ import annotations

import http.server
import socketserver
import threading
from contextlib import contextmanager
from typing import Iterator

from test_suite.launcher import health


# ---------------------------------------------------------------------------
# Real HTTPServer harness
# ---------------------------------------------------------------------------


class _RespondingHandler(http.server.BaseHTTPRequestHandler):
    STATUS = 200
    BODY = b'{"name":"fake","protocolVersion":"1.0"}'
    PATH = '/.well-known/agent-card.json'

    def do_GET(self):  # noqa: N802
        if self.path != self.PATH:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(self.STATUS)
        self.end_headers()
        if self.STATUS == 200:
            self.wfile.write(self.BODY)

    def log_message(self, *_a, **_k):  # noqa: ARG002 — silence per-request logs
        pass


@contextmanager
def _serve(status: int = 200) -> Iterator[int]:
    """Yield the http_port a background server is listening on."""
    class _H(_RespondingHandler):
        pass
    _H.STATUS = status
    server = socketserver.TCPServer(('127.0.0.1', 0), _H)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAgentCardUrl:
    def test_default_host(self):
        assert health.agent_card_url(8001) == (
            'http://127.0.0.1:8001/.well-known/agent-card.json'
        )

    def test_custom_host(self):
        assert health.agent_card_url(8001, host='example.com') == (
            'http://example.com:8001/.well-known/agent-card.json'
        )


class TestWaitReady:
    def test_ready_immediately(self):
        with _serve(200) as port:
            ok, elapsed = health.wait_ready(port, timeout_s=5)
            assert ok
            assert elapsed < 5

    def test_404_never_ready(self):
        # Server responds 404 on the well-known path.
        with _serve(404) as port:
            ok, elapsed = health.wait_ready(
                port,
                timeout_s=1,
                poll_interval_s=0.1,
            )
            assert not ok
            assert 0.5 <= elapsed  # exhausted the timeout

    def test_500_never_ready(self):
        with _serve(500) as port:
            ok, _ = health.wait_ready(port, timeout_s=1, poll_interval_s=0.1)
            assert not ok

    def test_nothing_listening(self):
        # Pick a random unused port and don't serve on it.
        import socket
        with socket.socket() as s:
            s.bind(('', 0))
            port = s.getsockname()[1]
        ok, elapsed = health.wait_ready(
            port, timeout_s=1, poll_interval_s=0.1,
        )
        assert not ok
        assert elapsed >= 0.5

    def test_deadline_check_uses_injected_now(self):
        """Injecting `_now` lets tests exercise the deadline logic
        without waiting real seconds. Simulate: three polls, then deadline.
        """
        clock = [0.0]
        def tick():
            return clock[0]
        def sleep(dt):
            clock[0] += dt

        # Nothing listens on 1 (privileged), so urllib will raise on connect.
        # Elapsed will be advanced only by sleep.
        ok, elapsed = health.wait_ready(
            1,  # port 1 (privileged) — connect refused
            timeout_s=3,
            poll_interval_s=1.0,
            _now=tick,
            _sleep=sleep,
        )
        assert not ok
        # Deadline was 3s in fake time; the loop should have exhausted it.
        assert elapsed >= 3.0
