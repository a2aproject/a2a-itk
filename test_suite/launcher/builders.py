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

from test_suite.launcher import codegen, config
from test_suite.launcher.errors import InfraFailure, Stage


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
    skip_codegen: bool = False,
    proto_source: Path | None = None,
    itk_source: Path | None = None,
) -> Language:
    """Detect the language of ``agent_dir`` and build the agent there.

    Runs :mod:`test_suite.launcher.codegen` before or after the SDK build tool as
    each language needs (see below). Set ``skip_codegen=True`` to bypass —
    useful in tests that isolate the build step, and for callers whose
    upstream bash script already prepared the proto artifacts.

    Ordering per language:

      * ``go`` — codegen BEFORE build (``main.go`` imports ``./pb``).
      * ``rust`` — codegen BEFORE build (``build.rs`` reads a2a-itk/protos).
      * ``java`` — codegen BEFORE build (``protobuf-maven-plugin`` reads
        ``a2a-itk/protos`` during ``mvn install``).
      * ``python`` — codegen AFTER build (needs the uv-managed
        ``grpcio-tools``).
      * ``ts`` — codegen AFTER build (needs ``node_modules/.bin/buf``).
      * ``dotnet`` — no-op both sides.

    Args:
        repo, sha: For error attribution.
        agent_dir: The unpacked source tree.
        timeout: Per-step timeout; defaults to :func:`config.build_timeout`.
        skip_codegen: If True, only run the SDK build tool.
        proto_source: Override for ``instruction.proto`` location.
        itk_source: Override for the a2a-itk root (for symlink targets).

    Returns:
        The detected language.

    Raises:
        InfraFailure: Build or codegen timed out or exited non-zero.
    """
    lang = detect_language(agent_dir)
    to = timeout if timeout is not None else config.build_timeout()
    builder = _BUILDERS[lang]

    codegen_first = lang in _CODEGEN_BEFORE_BUILD
    do_codegen = (not skip_codegen) and (lang in _CODEGEN_PREPARERS)

    try:
        if do_codegen and codegen_first:
            _run_codegen(lang, agent_dir, to, proto_source, itk_source)
        builder(agent_dir, to)
        if do_codegen and not codegen_first:
            _run_codegen(lang, agent_dir, to, proto_source, itk_source)
    except subprocess.TimeoutExpired as e:
        raise InfraFailure(repo, sha, Stage.BUILD, cause=e) from e
    except subprocess.CalledProcessError as e:
        raise InfraFailure(repo, sha, Stage.BUILD, cause=e) from e
    return lang


def _run_codegen(
    lang: Language,
    agent_dir: Path,
    timeout: int,
    proto_source: Path | None,
    itk_source: Path | None,
) -> None:
    """Dispatch to the right :mod:`codegen` preparer for ``lang``."""
    prep = _CODEGEN_PREPARERS[lang]
    prep(
        agent_dir,
        proto_source=proto_source,
        itk_source=itk_source,
        timeout=timeout,
    )


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


# Wrapper functions that ignore unused kwargs — lets us dispatch every
# preparer with the same call signature from _run_codegen.
def _prep_python(agent_dir, *, proto_source=None, itk_source=None, timeout=None):  # noqa: ARG001
    codegen.prepare_python(agent_dir, proto_source=proto_source, timeout=timeout)


def _prep_go(agent_dir, *, proto_source=None, itk_source=None, timeout=None):  # noqa: ARG001
    codegen.prepare_go(agent_dir, proto_source=proto_source, timeout=timeout)


def _prep_ts(agent_dir, *, proto_source=None, itk_source=None, timeout=None):
    codegen.prepare_ts(
        agent_dir,
        proto_source=proto_source, itk_source=itk_source, timeout=timeout,
    )


def _prep_rust(agent_dir, *, proto_source=None, itk_source=None, timeout=None):  # noqa: ARG001
    codegen.prepare_rust(agent_dir, itk_source=itk_source)


def _prep_java(agent_dir, *, proto_source=None, itk_source=None, timeout=None):  # noqa: ARG001
    codegen.prepare_java(agent_dir, itk_source=itk_source)


_CODEGEN_PREPARERS = {
    Language.PYTHON: _prep_python,
    Language.GO: _prep_go,
    Language.TS: _prep_ts,
    Language.RUST: _prep_rust,
    Language.JAVA: _prep_java,
    # Language.DOTNET intentionally absent — no codegen needed
}


# Languages whose build tool reads the proto/a2a-itk paths itself
# (or whose source imports the generated stubs at compile time) need
# codegen to happen BEFORE the SDK build tool runs. The rest run codegen
# after their build tool sets up the environment codegen depends on.
_CODEGEN_BEFORE_BUILD = frozenset({Language.GO, Language.RUST, Language.JAVA})
