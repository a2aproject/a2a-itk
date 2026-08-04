"""Fetch a specific commit from a GitHub repo into an on-disk tree.

Two entry points:

  * :func:`resolve_ref` — one-shot ``git ls-remote`` to translate a symbolic
    ref (``main``, ``v1.2.3``) to a 40-hex SHA. Called at plan time by the
    role-binding runner (Phase 2), not by :func:`fetch_commit` itself.

  * :func:`fetch_commit` — fetch exactly one commit into ``dst``, with
    bounded retry on transient errors. A 404-class failure (SHA not found)
    is surfaced as :class:`~v2.launcher.errors.PermanentError` — retrying
    will never resolve it.

Both do their I/O through :func:`_run_git`, which every test replaces with a
fake so nothing here touches the real network.
"""

from __future__ import annotations

import random
import subprocess
import time
from pathlib import Path

from v2.launcher import config
from v2.launcher.errors import InfraFailure, PermanentError, Stage


_GITHUB_HTTPS = 'https://github.com/{repo}.git'


def repo_url(repo: str) -> str:
    """Return the HTTPS URL for a ``owner/name`` repo."""
    return _GITHUB_HTTPS.format(repo=repo)


def _run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Run a git subcommand. Isolated for test injection."""
    return subprocess.run(  # noqa: S603
        ['git', *args],  # noqa: S607
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _classify_git_failure(stderr: str) -> type[InfraFailure | PermanentError]:
    """Decide retry vs fail-fast from git stderr.

    Permanent: repository access denied, ref does not exist, bad SHA format,
    HTTP 404. Anything else — including timeout — is treated as transient.
    """
    lowered = stderr.lower()
    # Markers are intentionally specific — a bare 'not found' would false-
    # positive on transient stderr like "unable to access '.../.git': not
    # found" during a proxy outage and cost the retry budget.
    permanent_markers = (
        'repository not found',
        "couldn't find remote ref",
        'did not send all necessary objects',
        'unadvertised object',
        'reference is not a tree',
        'unknown revision',
        'bad object',
        'authentication failed',
        'permission denied',
        '404 not found',              # HTTP 404 from the remote
        'not our ref',                # server refused a fetch-by-SHA
        'upload-pack: not our ref',   # same, verbose form
    )
    for marker in permanent_markers:
        if marker in lowered:
            return PermanentError
    return InfraFailure


def resolve_ref(repo: str, ref: str, *, timeout: int | None = None) -> str:
    """Resolve a symbolic ref to a 40-hex commit SHA via ``git ls-remote``.

    Args:
        repo: ``owner/name``.
        ref: A branch, tag, or partial SHA. ``git ls-remote`` will only match
            branches and tags — a bare SHA does not resolve this way.
        timeout: Per-attempt timeout; defaults to :func:`config.checkout_timeout`.

    Raises:
        PermanentError: The ref does not exist on the remote.
        InfraFailure: Network exhausted after retries.
    """
    to = timeout if timeout is not None else config.checkout_timeout()
    last_exc: BaseException | None = None
    for attempt in range(config.retries()):
        try:
            cp = _run_git(
                ['ls-remote', repo_url(repo), ref],
                timeout=to,
            )
        except subprocess.TimeoutExpired as e:
            last_exc = e
            _sleep_backoff(attempt)
            continue

        if cp.returncode == 0:
            # Output shape: "<40hex>\trefs/heads/main\n" (possibly multiple lines).
            for raw in cp.stdout.splitlines():
                line = raw.strip()
                if not line:
                    continue
                sha = line.split('\t', 1)[0].strip()
                if len(sha) == 40 and all(c in '0123456789abcdef' for c in sha):
                    return sha
            raise PermanentError(
                repo,
                ref,
                Stage.FETCH,
                f'ls-remote returned no matching ref for {ref!r}',
            )

        err_cls = _classify_git_failure(cp.stderr)
        if err_cls is PermanentError:
            raise PermanentError(repo, ref, Stage.FETCH, cp.stderr.strip() or 'ls-remote failed')
        last_exc = RuntimeError(cp.stderr.strip() or f'ls-remote exit {cp.returncode}')
        _sleep_backoff(attempt)

    raise InfraFailure(repo, ref, Stage.FETCH, cause=last_exc)


def fetch_commit(repo: str, sha: str, dst: Path, *, timeout: int | None = None) -> None:
    """Materialise ``repo@sha`` at ``dst``.

    Strategy: ``git init`` + ``git fetch --depth 1 <url> <sha>`` +
    ``git checkout FETCH_HEAD``. Cheaper than a full clone; requires the
    remote server to allow uploadpack of arbitrary SHAs (GitHub does by
    default).

    ``dst`` must not exist or must be empty. The caller (cache.py) guarantees
    this by ``rmtree``-ing any partial prior attempt under the per-key lock.

    Raises:
        PermanentError: The SHA does not exist on the remote.
        InfraFailure: Network exhausted after retries.
    """
    to = timeout if timeout is not None else config.checkout_timeout()
    dst.mkdir(parents=True, exist_ok=True)
    last_exc: BaseException | None = None
    for attempt in range(config.retries()):
        try:
            init = _run_git(['init', '--quiet'], cwd=dst, timeout=to)
            if init.returncode != 0:
                # Transient — race with rmtree or a locked .git/ dir. Capture
                # the stderr so exhausted retries surface something useful.
                last_exc = RuntimeError(
                    init.stderr.strip() or f'git init exit {init.returncode}'
                )
                _sleep_backoff(attempt)
                continue

            fetch = _run_git(
                ['fetch', '--depth', '1', repo_url(repo), sha],
                cwd=dst,
                timeout=to,
            )
            if fetch.returncode != 0:
                err_cls = _classify_git_failure(fetch.stderr)
                if err_cls is PermanentError:
                    raise PermanentError(
                        repo, sha, Stage.FETCH,
                        fetch.stderr.strip() or 'fetch failed',
                    )
                last_exc = RuntimeError(
                    fetch.stderr.strip() or f'fetch exit {fetch.returncode}'
                )
                _sleep_backoff(attempt)
                continue

            checkout = _run_git(
                ['checkout', '--quiet', 'FETCH_HEAD'],
                cwd=dst,
                timeout=to,
            )
            if checkout.returncode != 0:
                # Checkout failure after a successful fetch is almost always
                # a corrupted local dst — treat as transient so the caller's
                # cleanup + retry can recover.
                last_exc = RuntimeError(
                    checkout.stderr.strip() or f'checkout exit {checkout.returncode}'
                )
                _sleep_backoff(attempt)
                continue
            return
        except subprocess.TimeoutExpired as e:
            last_exc = e
            _sleep_backoff(attempt)

    raise InfraFailure(repo, sha, Stage.FETCH, cause=last_exc)


def _sleep_backoff(attempt: int) -> None:
    """Exponential backoff with jitter.

    Attempt 0 -> ~1s, attempt 1 -> ~2s, attempt 2 -> ~4s. Jitter avoids
    thundering-herd retries against a flaky remote.
    """
    base = 2 ** attempt
    time.sleep(base + random.uniform(0, base * 0.5))  # noqa: S311
