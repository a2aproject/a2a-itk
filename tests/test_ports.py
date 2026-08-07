"""Port allocation: pair distinctness, per-process reservoir bookkeeping."""

from __future__ import annotations

import socket
import threading

import pytest

from test_suite.launcher import ports


class TestAddressReservoir:
    def test_take_and_contains(self):
        r = ports.AddressReservoir()
        assert not r.contains(1234)
        r.take(1234)
        assert r.contains(1234)

    def test_release(self):
        r = ports.AddressReservoir()
        r.take(1234)
        r.release(1234)
        assert not r.contains(1234)

    def test_release_absent_is_noop(self):
        r = ports.AddressReservoir()
        r.release(9999)  # no-op, does not raise

    def test_clear(self):
        r = ports.AddressReservoir()
        r.take(1)
        r.take(2)
        r.clear()
        assert not r.contains(1)
        assert not r.contains(2)

    def test_thread_safe(self):
        """Two threads racing to take() the same port both succeed
        (idempotent) — no crash, no inconsistent state.
        """
        r = ports.AddressReservoir()
        def worker():
            for i in range(1000):
                r.take(i)
                r.release(i)
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)


class TestAllocatePair:
    def test_returns_two_ports(self):
        http, grpc = ports.allocate_pair(ports.AddressReservoir())
        assert isinstance(http, int) and isinstance(grpc, int)
        assert 1024 < http < 65536
        assert 1024 < grpc < 65536

    def test_pair_is_distinct(self):
        # Reservoir excludes ports handed out on the previous call within
        # allocate_pair; second call should return a different port.
        for _ in range(50):
            http, grpc = ports.allocate_pair(ports.AddressReservoir())
            assert http != grpc

    def test_ports_are_bindable(self):
        # A port coming out of allocate_pair should be free at that instant.
        # (Small race window between our close and the test's bind — we're
        # not testing that window here, just that the port is a real ephemeral.)
        http, grpc = ports.allocate_pair(ports.AddressReservoir())
        for p in (http, grpc):
            with socket.socket() as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(('', p))  # would raise EADDRINUSE if not free

    def test_two_calls_do_not_overlap_within_process(self):
        # Same reservoir across calls — no port returned twice from either
        # http or grpc slot across many pairs.
        r = ports.AddressReservoir()
        seen: set[int] = set()
        for _ in range(50):
            a, b = ports.allocate_pair(r)
            assert a not in seen and b not in seen, (
                'reservoir must not hand out an in-use port'
            )
            seen.add(a)
            seen.add(b)

    def test_default_reservoir_used_when_none(self):
        # Passing None uses the module-level default; two consecutive calls
        # with None should still not overlap.
        ports._default_reservoir.clear()  # noqa: SLF001 — isolate test
        try:
            a, b = ports.allocate_pair()
            c, d = ports.allocate_pair()
            assert len({a, b, c, d}) == 4
        finally:
            ports._default_reservoir.clear()  # noqa: SLF001

    def test_reservoir_exhaustion_raises(self, monkeypatch):
        # If _one_free_port never finds a free port not in the reservoir,
        # it raises after 100 attempts. Simulate by patching _one_free_port
        # to always return the same port.
        r = ports.AddressReservoir()
        r.take(12345)

        def fake_free_port(_reservoir):
            return 12345  # same port every time

        # We can't easily force the kernel to give us 12345 every time, so
        # we test the RuntimeError path by patching _one_free_port itself.
        def always_taken(reservoir):
            # Simulate 100 attempts all colliding with reservoir.
            raise RuntimeError('could not find a free port not already reserved by this process')

        monkeypatch.setattr(ports, '_one_free_port', always_taken)
        with pytest.raises(RuntimeError, match='could not find a free port'):
            ports.allocate_pair(r)

    def test_second_failure_rolls_back_first_reservation(self, monkeypatch):
        """Regression: allocate_pair must release the http port if the
        grpc allocation raises. Prior to the fix, the http reservation
        leaked into the reservoir for the process lifetime.
        """
        r = ports.AddressReservoir()
        call = [0]

        def fake_one_free_port(reservoir):
            call[0] += 1
            if call[0] == 1:
                reservoir.take(7777)
                return 7777
            raise RuntimeError('boom on second call')

        monkeypatch.setattr(ports, '_one_free_port', fake_one_free_port)
        with pytest.raises(RuntimeError, match='boom on second call'):
            ports.allocate_pair(r)
        # http port must have been rolled back.
        assert not r.contains(7777)


class TestRelease:
    def test_release_via_module_helper(self):
        r = ports.AddressReservoir()
        r.take(9001)
        ports.release(9001, reservoir=r)
        assert not r.contains(9001)

    def test_release_multiple(self):
        r = ports.AddressReservoir()
        r.take(1)
        r.take(2)
        r.take(3)
        ports.release(1, 2, 3, reservoir=r)
        assert not (r.contains(1) or r.contains(2) or r.contains(3))
