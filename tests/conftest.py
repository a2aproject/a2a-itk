"""Shared fixtures.

Every test that touches the cache root sets ``ITK_CACHE_DIR`` to a per-test
``tmp_path`` so tests never see each other's state and never touch
``$HOME/.cache``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolate the launcher cache under ``tmp_path/cache``."""
    root = tmp_path / 'cache'
    monkeypatch.setenv('ITK_CACHE_DIR', str(root))
    # Force a stable image digest across a test so cache keys are predictable.
    monkeypatch.setenv('ITK_IMAGE_DIGEST', 'test-digest')
    # Shorten defaults so eviction/pin-timeout tests can trigger fast.
    monkeypatch.setenv('ITK_RETRIES', '2')
    yield root


@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neuter :func:`test_suite.launcher.fetch._sleep_backoff` so retry tests are fast."""
    from test_suite.launcher import fetch
    monkeypatch.setattr(fetch, '_sleep_backoff', lambda _attempt: None)


@pytest.fixture(autouse=True)
def _reset_home_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent tests from accidentally polluting the developer's cache."""
    # Belt-and-braces: even if a test forgets to use cache_dir, at least it
    # won't land in ~/.cache/a2a-itk.
    if 'ITK_CACHE_DIR' not in os.environ:
        monkeypatch.setenv('ITK_CACHE_DIR', '/tmp/a2a-itk-cache-unset')  # noqa: S108
