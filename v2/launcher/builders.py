"""Per-language eager builders.

The comparison harness treats a ``CHECKOUT`` peer's cache slot as "built" iff
the ``.itk-built`` sentinel exists. Builders here materialise that state under
the cache lock so :mod:`.spawn` skips the lazy-build branch and simply spawns.

Detection is shared with :mod:`.spawn` so both modules classify a directory
identically. If they drift, ``spawn`` builds lazily for a language the eager
builder skipped — silent extra latency instead of a hard failure.

Every builder is idempotent (skip if the artifact already exists), so calling
:func:`build_in_place` twice is a no-op the second time.
"""

from __future__ import annotations

import enum
import subprocess
from collections.abc import Callable
from pathlib import Path

from v2.launcher import config
from v2.launcher.errors import InfraFailure, Stage


class Language(enum.Enum):
    """Detected agent language."""

    PYTHON = 'python'
    GO = 'go'
    JAVA = 'java'
    RUST = 'rust'
    TS = 'ts'
    DOTNET = 'dotnet'


def detect_language(agent_dir: Path) -> Language:
    """Classify ``agent_dir`` by the same precedence :mod:`.spawn` uses.

    Ordering matters: an agent that happens to have both a ``main.py`` and a
    ``pom.xml`` should be classified the same way ``spawn_from_dir`` will
    ultimately spawn it.

    Raises:
        RuntimeError: No recognised entrypoint.
    """
    if (agent_dir / 'main.go').exists():
        return Language.GO
    if (agent_dir / 'main.py').exists():
        return Language.PYTHON
    if (agent_dir.parent / 'package.json').exists():
        return Language.TS
    if any(agent_dir.glob('*.csproj')):
        return Language.DOTNET
    if (agent_dir / 'pom.xml').exists():
        return Language.JAVA
    if (agent_dir / 'Cargo.toml').exists():
        return Language.RUST
    raise RuntimeError(
        f'could not detect agent language in {agent_dir}. '
        f'Expected main.go, main.py, ../package.json, *.csproj, pom.xml, or Cargo.toml.'
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

_Builder = Callable[[Path, int], None]


def build_in_place(
    repo: str,
    sha: str,
    agent_dir: Path,
    *,
    timeout: int | None = None,
) -> Language:
    """Detect the language of ``agent_dir`` and build the agent there.

    Args:
        repo: Owning repo, for error attribution.
        sha: Resolved commit, for error attribution.
        agent_dir: The unpacked source tree.
        timeout: Per-build timeout; defaults to :func:`config.build_timeout`.

    Returns:
        The detected language, so the caller can log it or emit a metric.

    Raises:
        InfraFailure: Build timed out or exited non-zero.
    """
    lang = detect_language(agent_dir)
    to = timeout if timeout is not None else config.build_timeout()
    builder = _BUILDERS[lang]
    try:
        builder(agent_dir, to)
    except subprocess.TimeoutExpired as e:
        raise InfraFailure(repo, sha, Stage.BUILD, cause=e) from e
    except subprocess.CalledProcessError as e:
        raise InfraFailure(repo, sha, Stage.BUILD, cause=e) from e
    return lang


# ---------------------------------------------------------------------------
# Per-language builders
# ---------------------------------------------------------------------------


def _build_python(agent_dir: Path, timeout: int) -> None:
    """uv sync from the committed lockfile.

    Uses the local .venv if one exists (idempotent — uv is a no-op if
    everything is already installed). If the lockfile is stale, --locked
    makes uv fail loudly rather than silently re-resolve.
    """
    subprocess.run(  # noqa: S603
        ['uv', 'sync', '--locked'],  # noqa: S607
        cwd=str(agent_dir),
        check=True,
        timeout=timeout,
        capture_output=True,
    )


def _build_go(agent_dir: Path, timeout: int) -> None:
    """Eager ``go build`` so a compiled binary lives in the cached tree."""
    bin_dir = agent_dir / 'bin'
    bin_dir.mkdir(exist_ok=True)
    binary = bin_dir / 'agent'
    if binary.exists():
        return
    subprocess.run(  # noqa: S603
        ['go', 'build', '-mod=readonly', '-o', str(binary), '.'],  # noqa: S607
        cwd=str(agent_dir),
        check=True,
        timeout=timeout,
        capture_output=True,
    )


def _build_java(agent_dir: Path, timeout: int) -> None:
    """Maven install of the itk submodule + its siblings.

    ``-Pitk`` is required because the itk module is excluded from the default
    ``<modules>`` in a2a-java's parent pom. Mirrors ``spawn``'s recipe.
    """
    parent = agent_dir.parent
    subprocess.run(  # noqa: S603
        [
            'mvn', '-Pitk', '-pl', 'itk', '-am', 'install',
            '-DskipTests', '-Dmaven.javadoc.skip=true',
        ],  # noqa: S607
        cwd=str(parent),
        check=True,
        timeout=timeout,
        capture_output=True,
    )


def _build_rust(agent_dir: Path, timeout: int) -> None:
    """``cargo build --locked --release``; idempotent via target/ inspection."""
    release_dir = agent_dir / 'target' / 'release'
    if release_dir.exists() and any(release_dir.glob('itk-*')):
        return
    subprocess.run(  # noqa: S603
        ['cargo', 'build', '--locked', '--release'],  # noqa: S607
        cwd=str(agent_dir),
        check=True,
        timeout=timeout,
        capture_output=True,
    )


def _build_ts(agent_dir: Path, timeout: int) -> None:
    """``npm ci`` at the repo root.

    TS agents live one level below a repo whose root owns ``package.json`` and
    ``package-lock.json``. ``npm ci`` uses the lockfile verbatim and errors if
    it is out of sync.
    """
    repo_root = agent_dir.parent
    if (repo_root / 'node_modules').exists():
        return
    subprocess.run(  # noqa: S603
        ['npm', 'ci'],  # noqa: S607
        cwd=str(repo_root),
        check=True,
        timeout=timeout,
        capture_output=True,
    )


def _build_dotnet(agent_dir: Path, timeout: int) -> None:  # noqa: ARG001
    """No-op: ``dotnet run`` builds implicitly on spawn.

    Kept for parity with ``spawn``'s detection so the launcher doesn't refuse
    a .NET tree. Once .NET is an active SDK line we replace this with
    ``dotnet publish`` or ``dotnet build`` here.
    """
    return


_BUILDERS: dict[Language, _Builder] = {
    Language.PYTHON: _build_python,
    Language.GO: _build_go,
    Language.JAVA: _build_java,
    Language.RUST: _build_rust,
    Language.TS: _build_ts,
    Language.DOTNET: _build_dotnet,
}
