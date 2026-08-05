"""Concurrency invariants: refcounted pins, race-free eviction, stale reclamation.

Uses threads (multiple concurrent readers/writers in one process) and PID-file
manipulation (dead/stale pin reclamation). No real subprocesses are required —
the point is to exercise the lock+pin protocol under contention, not to prove
that fcntl works.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from test_suite.launcher import cache


_REPO = 'a2aproject/a2a-python'
_SHA = 'a' * 40


def _fetch_ok(repo, sha, dst):  # noqa: ARG001
    (dst / 'itk').mkdir(parents=True, exist_ok=True)
    (dst / 'itk' / 'main.py').write_text('# fake', encoding='utf-8')


def _build_ok(repo, sha, agent_dir):  # noqa: ARG001
    (agent_dir / '.built').touch()


class TestRefcountedPins:
    def test_two_pins_from_different_pids_share_tree(self, cache_dir):
        # First run pins normally.
        cache.checkout_and_build(_REPO, _SHA, _fetcher=_fetch_ok, _builder=_build_ok)
        key = cache.cache_key(_REPO, _SHA)
        pin_dir = cache_dir / 'pins' / key
        assert (pin_dir / str(os.getpid())).exists()

        # Simulate a second live run by writing another pin under a spoofed PID.
        # We use os.getpid() twice (same PID, different files) is not possible —
        # so use PID=1 (init, always alive on Linux) to represent "another run".
        (pin_dir / '1').write_text(str(time.time()), encoding='utf-8')

        # Our release removes only our pin, not init's.
        cache.release(_REPO, _SHA)
        assert not (pin_dir / str(os.getpid())).exists()
        assert (pin_dir / '1').exists()

    def test_evict_keeps_tree_while_any_live_pin_remains(self, cache_dir, monkeypatch):
        cache.checkout_and_build(_REPO, _SHA, _fetcher=_fetch_ok, _builder=_build_ok)
        cache.release(_REPO, _SHA)  # drop our own pin

        # Simulate another live run (PID=1 is always alive on Linux).
        key = cache.cache_key(_REPO, _SHA)
        pin_dir = cache_dir / 'pins' / key
        pin_dir.mkdir(parents=True, exist_ok=True)
        (pin_dir / '1').write_text(str(time.time()), encoding='utf-8')

        # Force budget pressure so evict would otherwise reclaim.
        monkeypatch.setenv('ITK_DISK_BUDGET_BYTES', '1')
        evicted = cache.evict()
        assert key not in evicted, 'tree with a live pin must survive eviction'
        assert (cache_dir / 'trees' / key).exists()


class TestStalePinReclaim:
    def test_dead_pid_pin_is_reclaimed(self, cache_dir):
        cache.checkout_and_build(_REPO, _SHA, _fetcher=_fetch_ok, _builder=_build_ok)
        cache.release(_REPO, _SHA)

        # Write a pin for a PID that is essentially never alive.
        key = cache.cache_key(_REPO, _SHA)
        pin_dir = cache_dir / 'pins' / key
        pin_dir.mkdir(parents=True, exist_ok=True)
        dead_pid = 2**22  # far above any realistic PID; ProcessLookupError expected
        (pin_dir / str(dead_pid)).write_text(str(time.time()), encoding='utf-8')

        assert cache._has_live_pin(key) is False  # noqa: SLF001
        assert not (pin_dir / str(dead_pid)).exists(), 'reclaim should unlink dead-pid pin'

    def test_stale_pin_past_deadline_is_reclaimed(self, cache_dir, monkeypatch):
        monkeypatch.setenv('ITK_RUN_DEADLINE', '1')  # 1s deadline
        cache.checkout_and_build(_REPO, _SHA, _fetcher=_fetch_ok, _builder=_build_ok)
        cache.release(_REPO, _SHA)

        key = cache.cache_key(_REPO, _SHA)
        pin_dir = cache_dir / 'pins' / key
        pin_dir.mkdir(parents=True, exist_ok=True)
        # Live PID (us) but ancient timestamp -> considered stale.
        stale = pin_dir / str(os.getpid())
        stale.write_text('0.0', encoding='utf-8')

        assert cache._has_live_pin(key) is False  # noqa: SLF001
        assert not stale.exists()

    def test_junk_pin_file_is_removed(self, cache_dir):
        cache.checkout_and_build(_REPO, _SHA, _fetcher=_fetch_ok, _builder=_build_ok)
        cache.release(_REPO, _SHA)

        key = cache.cache_key(_REPO, _SHA)
        pin_dir = cache_dir / 'pins' / key
        junk = pin_dir / 'not-a-pid'
        junk.write_text('lolwut', encoding='utf-8')
        assert cache._has_live_pin(key) is False  # noqa: SLF001
        assert not junk.exists()


class TestEvictSkipsHeldLock:
    def test_evict_skips_key_whose_lock_is_held(self, cache_dir, monkeypatch):
        """A builder is running under the per-key lock; evict must not touch."""
        cache.checkout_and_build(_REPO, _SHA, _fetcher=_fetch_ok, _builder=_build_ok)
        cache.release(_REPO, _SHA)

        key = cache.cache_key(_REPO, _SHA)
        lock_path = cache._lock_path(key)  # noqa: SLF001

        # Force budget pressure.
        monkeypatch.setenv('ITK_DISK_BUDGET_BYTES', '1')

        # A background thread holds the per-key lock. Evict must skip.
        holding = threading.Event()
        release = threading.Event()

        def hold_lock():
            with cache._flock(lock_path):  # noqa: SLF001
                holding.set()
                release.wait(timeout=5)

        t = threading.Thread(target=hold_lock)
        t.start()
        try:
            assert holding.wait(timeout=2)
            evicted = cache.evict()
            assert key not in evicted, 'evict must skip a key with a held lock'
            assert (cache_dir / 'trees' / key).exists()
        finally:
            release.set()
            t.join(timeout=5)


class TestSameProcessRefcount:
    """Two LaunchSessions in one process must both hold the pin cleanly."""

    def test_double_pin_single_unpin_keeps_pin_alive(self, cache_dir):
        cache.checkout_and_build(_REPO, _SHA, _fetcher=_fetch_ok, _builder=_build_ok)
        # A second checkout_and_build for the same key: cache hits, pins again.
        cache.checkout_and_build(_REPO, _SHA, _fetcher=_fetch_ok, _builder=_build_ok)
        key = cache.cache_key(_REPO, _SHA)
        pin_file = cache_dir / 'pins' / key / str(os.getpid())
        assert pin_file.exists()

        # One release: pin file must remain because the second pin is live.
        cache.release(_REPO, _SHA)
        assert pin_file.exists(), 'first release must not unlink while another pin is live'

        # Second release: pin file goes.
        cache.release(_REPO, _SHA)
        assert not pin_file.exists()

    def test_extra_release_without_pin_is_noop(self, cache_dir):  # noqa: ARG002
        # Regression: calling release() more times than pin() must not
        # unlink a pin file that belongs to a live sibling.
        cache.checkout_and_build(_REPO, _SHA, _fetcher=_fetch_ok, _builder=_build_ok)
        cache.release(_REPO, _SHA)  # refcount 1 -> 0, unlinks
        # Spurious extra release — must be a no-op, must not touch a
        # newly-created same-key pin from a hypothetical sibling.
        cache.release(_REPO, _SHA)
        cache.release(_REPO, _SHA)

    def test_refcount_isolates_from_other_keys(self, cache_dir):
        other_sha = 'b' * 40
        cache.checkout_and_build(_REPO, _SHA, _fetcher=_fetch_ok, _builder=_build_ok)
        cache.checkout_and_build(_REPO, other_sha, _fetcher=_fetch_ok, _builder=_build_ok)
        # Releasing one key must not affect the other's refcount.
        cache.release(_REPO, _SHA)
        key_other = cache.cache_key(_REPO, other_sha)
        pin_file_other = cache_dir / 'pins' / key_other / str(os.getpid())
        assert pin_file_other.exists()
        cache.release(_REPO, other_sha)
        assert not pin_file_other.exists()


class TestConcurrentCheckout:
    def test_two_threads_share_tree_via_lock(self, cache_dir):
        """Two threads racing to build the same key: exactly one fetch/build."""
        calls = {'fetch': 0, 'build': 0}
        gate = threading.Event()

        def fetch(repo, sha, dst):
            gate.wait(timeout=5)  # hold both threads until we say go
            calls['fetch'] += 1
            _fetch_ok(repo, sha, dst)

        def build(repo, sha, agent_dir):
            calls['build'] += 1
            _build_ok(repo, sha, agent_dir)

        results: list[Path] = []
        errors: list[BaseException] = []

        def worker():
            try:
                d = cache.checkout_and_build(
                    _REPO, _SHA, _fetcher=fetch, _builder=build,
                )
                results.append(d)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)
            finally:
                cache.release(_REPO, _SHA)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        gate.set()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f'workers raised: {errors!r}'
        assert len(results) == 2
        assert results[0] == results[1], 'both must resolve to the same dir'
        # Second thread must see the sentinel and skip. Exactly one fetch/build.
        assert calls['fetch'] == 1
        assert calls['build'] == 1
