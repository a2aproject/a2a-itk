"""Launcher error taxonomy.

Two axes matter to the comparison harness:

  * **Stage** — where in the pipeline the failure happened. Attributes the red
    to a specific (peer, ref, stage) so a moving-target regression is
    diagnosable rather than a blanket "the run failed".

  * **Transient vs permanent** — decides whether the runner retries. A network
    hiccup during ``git fetch`` is transient; a 404 on the SHA is permanent
    (retrying will never resolve it). This maps directly to the harness's
    ``INFRA_FAILURE`` vs ``REAL_FAILURE`` classification.
"""

from __future__ import annotations

import enum


class Stage(enum.Enum):
    """Where in the launcher pipeline a failure occurred."""

    FETCH = 'fetch'         # git clone / fetch / checkout
    BUILD = 'build'         # per-language build step
    SPAWN = 'spawn'         # subprocess.Popen raised (binary missing, bad exec)
    READY = 'ready'         # health check didn't return 200 before deadline


class LauncherError(Exception):
    """Base class for launcher errors."""


def _target_label(repo: str | None, sha: str | None) -> str:
    """Human-friendly identifier used in error messages.

    CHECKOUT targets have both ``repo`` and ``sha``; MOUNT targets have
    neither. A half-populated state shouldn't happen in practice (the
    spec validators enforce all-or-nothing) but the label degrades
    gracefully rather than rendering ``"None@abc..."``.
    """
    if repo is None and sha is None:
        return 'mount'
    if repo is None:
        return f'?@{sha[:12]}'  # type: ignore[index]
    if sha is None:
        return f'{repo}@?'
    return f'{repo}@{sha[:12]}'


class InfraFailure(LauncherError):
    """Transient / retryable failure — network, timeout, dependency proxy.

    The runner retries these up to ``RETRIES`` times with exponential backoff.
    After retry exhaustion the classifier reports ``INFRA_FAILURE``.

    Attributes:
        repo: The repo being processed (``None`` for MOUNT targets).
        sha: The resolved commit SHA (``None`` for MOUNT targets).
        stage: Where the failure happened (:class:`Stage`).
        cause: Underlying exception, if any.
    """

    def __init__(
        self,
        repo: str | None,
        sha: str | None,
        stage: Stage,
        cause: BaseException | None = None,
        message: str | None = None,
    ) -> None:
        self.repo = repo
        self.sha = sha
        self.stage = stage
        self.cause = cause
        msg = message or f'{stage.value} failed for {_target_label(repo, sha)}'
        if cause is not None:
            msg = f'{msg}: {cause}'
        super().__init__(msg)


class PermanentError(LauncherError):
    """Non-retryable failure — 404 on SHA, malformed spec, unknown language.

    The runner does not retry. Surfaces as ``REAL_FAILURE`` in the classifier
    because retrying will never change the outcome.
    """

    def __init__(
        self,
        repo: str | None,
        sha: str | None,
        stage: Stage,
        message: str,
    ) -> None:
        self.repo = repo
        self.sha = sha
        self.stage = stage
        super().__init__(
            f'{stage.value} permanently failed for {_target_label(repo, sha)}: {message}'
        )
