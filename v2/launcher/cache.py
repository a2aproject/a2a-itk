"""Checkout + build cache — the concurrency-safe core of the launcher.

Layout under :func:`v2.launcher.config.cache_root`::

    trees/<key>/            the fetched + built agent tree
    trees/<key>/.itk-built  sentinel: fully-built and safe to reuse
    locks/<key>.lock        per-key flock; build AND evict take it
    pins/<key>/<pid>        one file per live run holding the tree

Cache key = ``slug(repo)@sha@image_digest``. SHA immutable so cross-run hits
are correct; ``image_digest`` folded in so a Dockerfile bump busts every key
(exactly what you want when Go/Rust get upgraded in the fat image).

Concurrency invariants:

  * The **same** flock guards both build and evict, so a "check-pins then
    rmtree" sequence is atomic against a concurrent build of the same key.
  * Pins are refcounted per-run (``pins/<key>/<pid>``), so multiple runs share
    one cached tree and each :func:`release` removes only its own pin.
  * ``locks/`` and ``pins/`` live **outside** ``trees/``, so removing a tree
    can never unlink them.
  * Stale pins (dead PID, or older than ``RUN_DEADLINE``) are reclaimed on
    read, so a killed runner can't permanently block eviction.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from v2.launcher import builders, config, fetch
from v2.launcher.errors import InfraFailure, LauncherError, Stage


_SENTINEL = '.itk-built'

# In-process refcount so two LaunchSessions in the same process can pin the
# same key without stomping on each other's on-disk pin file. The on-disk
# file represents "at least one live use in this process"; the counter says
# how many. Only the last release() unlinks the file. Cross-process pinning
# still goes through separate pin files (one per PID), unchanged.
_pin_refs: dict[str, int] = {}
_pin_refs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _slug(repo: str) -> str:
    """Filesystem-safe slug for ``owner/name``."""
    return repo.replace('/', '_')


def cache_key(repo: str, sha: str) -> str:
    """Cache key including the current image digest."""
    return f'{_slug(repo)}@{sha}@{config.image_digest()}'


def _root() -> Path:
    return config.cache_root()


def _tree_path(key: str) -> Path:
    return _root() / 'trees' / key


def _lock_path(key: str) -> Path:
    return _root() / 'locks' / f'{key}.lock'


def _pin_dir(key: str) -> Path:
    return _root() / 'pins' / key


def _ensure_layout() -> None:
    for sub in ('trees', 'locks', 'pins'):
        (_root() / sub).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# File locking — thin wrapper so tests can substitute
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _flock(path: Path, *, blocking: bool = True) -> Iterator[bool]:
    """Acquire an exclusive advisory lock on ``path``.

    Yields True on success, False when non-blocking and the lock is already
    held. The underlying file is created if absent.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # os.open avoids the read/write ambiguity of open() and lets us keep the
    # descriptor even after the process's own file objects are closed.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    got = False
    try:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
            got = True
        except BlockingIOError:
            if blocking:
                raise
            got = False
        yield got
    finally:
        if got:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------


def _pin(key: str, pid: int | None = None) -> None:
    """Record that ``pid`` (default: this process) is using ``key``.

    Same-process re-pins bump the in-memory refcount without re-writing the
    file. Cross-process pins (explicit ``pid`` != our own) always write a
    file, since we can't refcount for another process from here.
    """
    my_pid = os.getpid()
    p = pid if pid is not None else my_pid
    if p == my_pid:
        with _pin_refs_lock:
            _pin_refs[key] = _pin_refs.get(key, 0) + 1
            if _pin_refs[key] > 1:
                return  # pin file already exists from the first pin
    pin_dir = _pin_dir(key)
    pin_dir.mkdir(parents=True, exist_ok=True)
    (pin_dir / str(p)).write_text(str(time.time()), encoding='utf-8')


def _unpin(key: str, pid: int | None = None) -> None:
    """Drop this process's pin; leave others alone.

    Same-process unpins decrement the in-memory refcount and only unlink the
    file when the count hits zero. An unpin without a matching pin (extra
    ``release()`` call) is a silent no-op — matches the pre-refcount
    behaviour of ``FileNotFoundError`` suppression.
    """
    my_pid = os.getpid()
    p = pid if pid is not None else my_pid
    if p == my_pid:
        with _pin_refs_lock:
            current = _pin_refs.get(key, 0)
            if current == 0:
                # Never pinned in this process (or already fully unpinned) —
                # nothing to do, and don't touch the file (another process
                # might own the on-disk pin).
                return
            _pin_refs[key] = current - 1
            if _pin_refs[key] > 0:
                return
            del _pin_refs[key]
    pin_file = _pin_dir(key) / str(p)
    with contextlib.suppress(FileNotFoundError):
        pin_file.unlink()


def _pid_alive(pid: int) -> bool:
    """Portable liveness check via signal 0."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Another user's process — still alive, just not ours to signal.
        return True
    except OSError as e:
        # ESRCH is the standard case; treat everything else as alive to avoid
        # false-positive eviction.
        return e.errno != errno.ESRCH
    return True


def _has_live_pin(key: str) -> bool:
    """True iff any live run is holding this key.

    Reclaims dead/stale pins as a side effect: a pin file whose PID no longer
    exists, or whose timestamp is older than ``RUN_DEADLINE``, is unlinked.
    """
    pin_dir = _pin_dir(key)
    if not pin_dir.exists():
        return False
    live = False
    deadline = config.run_deadline()
    now = time.time()
    for entry in pin_dir.iterdir():
        try:
            pid = int(entry.name)
        except ValueError:
            # Junk file — remove and continue.
            with contextlib.suppress(OSError):
                entry.unlink()
            continue
        try:
            ts = float(entry.read_text(encoding='utf-8').strip() or '0')
        except (OSError, ValueError):
            ts = 0.0
        if _pid_alive(pid) and (now - ts) < deadline:
            live = True
        else:
            with contextlib.suppress(OSError):
                entry.unlink()
    return live


def _clear_pins(key: str) -> None:
    """Remove the entire pin dir for ``key``; called after successful rmtree."""
    pin_dir = _pin_dir(key)
    if pin_dir.exists():
        shutil.rmtree(pin_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Main entry: fetch, build, cache
# ---------------------------------------------------------------------------


def checkout_and_build(
    repo: str,
    sha: str,
    *,
    subdir: str = 'itk',
    _fetcher=fetch.fetch_commit,
    _builder=builders.build_in_place,
) -> Path:
    """Return the built agent dir for ``repo@sha`` (fetching if needed).

    Args:
        repo: ``owner/name``.
        sha: Resolved 40-hex commit SHA.
        subdir: Path inside the checked-out tree that holds the agent
            (defaults to ``itk``, which is where every SDK repo keeps it).
        _fetcher, _builder: Injection seams for tests. Callers should not set.

    The tree is pinned for the caller. Release the pin with :func:`release`
    (or use :class:`v2.launcher.resolve.LaunchSession`) when done.
    """
    _ensure_layout()
    key = cache_key(repo, sha)
    tree = _tree_path(key)
    sentinel = tree / _SENTINEL

    with _flock(_lock_path(key)):
        # Pin under the lock so a concurrent evict that just checked the pin
        # count and decided "no live pins" cannot rmtree between our check and
        # our pin write.
        _pin(key)

        if sentinel.exists():
            return tree / subdir

        # Any prior contents are a failed/partial build — always start clean.
        if tree.exists():
            shutil.rmtree(tree, ignore_errors=True)

        try:
            _fetcher(repo, sha, tree)
        except LauncherError:
            shutil.rmtree(tree, ignore_errors=True)
            _unpin(key)
            raise
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(tree, ignore_errors=True)
            _unpin(key)
            raise InfraFailure(repo, sha, Stage.FETCH, cause=e) from e

        agent_dir = tree / subdir
        if not agent_dir.exists():
            shutil.rmtree(tree, ignore_errors=True)
            _unpin(key)
            raise InfraFailure(
                repo, sha, Stage.FETCH,
                message=f'subdir {subdir!r} missing after fetch',
            )

        try:
            _builder(repo, sha, agent_dir)
        except LauncherError:
            shutil.rmtree(tree, ignore_errors=True)
            _unpin(key)
            raise
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(tree, ignore_errors=True)
            _unpin(key)
            raise InfraFailure(repo, sha, Stage.BUILD, cause=e) from e

        sentinel.touch()
        return agent_dir


def release(repo: str, sha: str, pid: int | None = None) -> None:
    """Drop this run's pin on the cached tree."""
    _unpin(cache_key(repo, sha), pid=pid)


# ---------------------------------------------------------------------------
# Eviction
# ---------------------------------------------------------------------------


def _tree_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _lru_keys() -> list[str]:
    """Cached keys ordered by oldest last-access first.

    Access time is approximated by the sentinel's mtime, which we ``touch()``
    on a successful build. Trees without a sentinel (partial) come first.
    """
    trees_dir = _root() / 'trees'
    if not trees_dir.exists():
        return []
    entries: list[tuple[float, str]] = []
    for tree in trees_dir.iterdir():
        if not tree.is_dir():
            continue
        sentinel = tree / _SENTINEL
        try:
            mtime = sentinel.stat().st_mtime if sentinel.exists() else 0.0
        except OSError:
            mtime = 0.0
        entries.append((mtime, tree.name))
    entries.sort()  # oldest first
    return [k for _, k in entries]


def evict() -> list[str]:
    """Reclaim unused cache slots if over budget or past TTL.

    Returns the list of evicted keys (useful for logging / testing). Safe to
    call from anywhere; skips keys whose per-key lock is held by a live
    builder, and keeps trees with any live pin.
    """
    _ensure_layout()
    evicted: list[str] = []
    budget = config.disk_budget_bytes()
    ttl = config.tree_ttl()
    now = time.time()
    trees_dir = _root() / 'trees'
    total = sum(_tree_size(trees_dir / k) for k in _lru_keys())

    for key in _lru_keys():
        tree = _tree_path(key)
        sentinel = tree / _SENTINEL
        try:
            age = now - sentinel.stat().st_mtime if sentinel.exists() else float('inf')
        except OSError:
            age = float('inf')

        need_evict = (total > budget) or (age > ttl)
        if not need_evict:
            continue

        with _flock(_lock_path(key), blocking=False) as got:
            if not got:
                # Someone is building/spawning under this key right now.
                continue
            if _has_live_pin(key):
                continue
            size = _tree_size(tree)
            shutil.rmtree(tree, ignore_errors=True)
            _clear_pins(key)
            total -= size
            evicted.append(key)
    return evicted


def _cli() -> int:
    """``python -m v2.launcher.cache evict`` — for scheduled CI hooks."""
    import sys
    if len(sys.argv) < 2 or sys.argv[1] != 'evict':
        print('usage: python -m v2.launcher.cache evict', file=sys.stderr)
        return 2
    evicted = evict()
    for key in evicted:
        print(key)
    return 0


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(_cli())
