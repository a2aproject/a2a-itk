"""Launcher configuration — cache root, timeouts, budgets, image identity.

Every knob is env-overridable so the CI wiring (Story 1.11) and local
developers can shape behaviour without patching code. Defaults match the
values in the design doc.

The cache **root** is the one setting that intentionally has no obviously
correct default: today's ``run_itk.sh`` destroys the container and image after
every run, so nothing survives between invocations anyway. Once the shadow job
lands it will bind-mount a host directory and set ``ITK_CACHE_DIR`` to point at
it.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


# ---------------------------------------------------------------------------
# Timeouts and budgets (seconds / bytes)
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    """Read an integer env var; return default on unset/empty; raise on garbage."""
    raw = os.environ.get(name, '').strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f'invalid integer for {name}: {raw!r}') from e


def checkout_timeout() -> int:
    """Max seconds for a single ``git fetch + checkout`` attempt."""
    return _env_int('ITK_CHECKOUT_TIMEOUT', 5 * 60)


def build_timeout() -> int:
    """Max seconds for one per-language build."""
    return _env_int('ITK_BUILD_TIMEOUT', 10 * 60)


def run_deadline() -> int:
    """Max seconds a single test run may hold a cache pin.

    Pins older than this are considered stale and reclaimable by evict(),
    even if the owning PID is still alive. Guards against a wedged runner
    permanently pinning a tree.
    """
    return _env_int('ITK_RUN_DEADLINE', 30 * 60)


def retries() -> int:
    """Max retry attempts for a transient failure."""
    return _env_int('ITK_RETRIES', 3)


def disk_budget_bytes() -> int:
    """Soft cap on total ``trees/`` size before evict() starts reclaiming."""
    return _env_int('ITK_DISK_BUDGET_BYTES', 50 * 1024 * 1024 * 1024)  # 50 GB


def tree_ttl() -> int:
    """Trees older than this (seconds) are eligible for eviction."""
    return _env_int('ITK_TREE_TTL', 7 * 24 * 60 * 60)  # 7 days


# ---------------------------------------------------------------------------
# Cache root
# ---------------------------------------------------------------------------

def cache_root() -> Path:
    """Directory that holds ``trees/``, ``locks/``, ``pins/``.

    Selection order:
      1. ``$ITK_CACHE_DIR``            — explicit, always wins.
      2. ``$XDG_CACHE_HOME/a2a-itk``   — freedesktop default.
      3. ``$HOME/.cache/a2a-itk``      — POSIX fallback.
      4. ``/tmp/a2a-itk-cache``        — last resort (no HOME in some containers).

    Callers should not create the directory themselves; :mod:`.cache` does it
    lazily under a lock.
    """
    explicit = os.environ.get('ITK_CACHE_DIR', '').strip()
    if explicit:
        return Path(explicit)
    xdg = os.environ.get('XDG_CACHE_HOME', '').strip()
    if xdg:
        return Path(xdg) / 'a2a-itk'
    home = os.environ.get('HOME', '').strip()
    if home:
        return Path(home) / '.cache' / 'a2a-itk'
    return Path('/tmp/a2a-itk-cache')  # noqa: S108 — documented last-resort


# ---------------------------------------------------------------------------
# Image / toolchain identity
# ---------------------------------------------------------------------------

def image_digest() -> str:
    """Identity of the current toolchain image, folded into cache keys.

    An image bump changes the digest, which busts every cached build key —
    exactly what you want when Go/Rust/Node get upgraded in the Dockerfile.

    Sources, in order:
      1. ``$ITK_IMAGE_DIGEST`` — set by CI once buildx pins a digest.
      2. sha256 of the ``Dockerfile`` next to the repo root.
      3. the literal ``"unpinned"`` — usable, but cache hits will still work
         within one run because the digest is stable for that process.

    Never raises: launcher must be usable in a bare Python env with no
    Dockerfile present (e.g. local dev, unit tests).
    """
    explicit = os.environ.get('ITK_IMAGE_DIGEST', '').strip()
    if explicit:
        return explicit
    dockerfile = _repo_root() / 'Dockerfile'
    if dockerfile.is_file():
        try:
            data = dockerfile.read_bytes()
        except OSError:
            return 'unpinned'
        return 'sha256_' + hashlib.sha256(data).hexdigest()[:16]
    return 'unpinned'


def _repo_root() -> Path:
    """Locate a2a-itk's root without importing test_suite.

    ``v2/launcher/config.py`` -> parents[2] is the repo root.
    """
    return Path(__file__).resolve().parents[2]
