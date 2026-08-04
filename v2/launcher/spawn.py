"""Polyglot agent spawn — parameterised on ``agent_dir``.

Vendored copy of ``test_suite/current.py``'s spawn logic. Kept independent so
the launcher's hot path does not depend on legacy code that will be deleted
at S17.

**Drift guard:** ``v2/tests/test_spawn_parity.py`` runs both this module and
the legacy ``test_suite.current.spawn_agent`` over the same fixture dirs with
subprocess patched, and asserts identical argv+cwd. That is how the two copies
stay honest until the legacy one goes.

Build behaviour matches ``current.py``: for rust and java, if the artifact
does not already exist, this module builds it lazily. For ``Kind.CHECKOUT``
targets, :mod:`v2.launcher.builders` has already eagerly built the tree under
the cache lock, so the artifact-exists branch is taken and nothing rebuilds.
Result: ``build-idempotent, build-once``.

**Log-file ownership.** When ``log_dir`` is set, the caller is handed back a
``Popen`` with an ``_log_file`` attribute pointing at the open handle.
:class:`v2.launcher.resolve.LaunchSession` closes this on exit; callers that
use :func:`spawn_from_dir` directly are responsible for closing it themselves.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Callable
from pathlib import Path


def spawn_from_dir(
    agent_dir: Path,
    http_port: int,
    grpc_port: int,
    *,
    log_dir: Path | None = None,
    log_name: str | None = None,
) -> subprocess.Popen:
    """Detect the agent's language and spawn it.

    Args:
        agent_dir: The directory that contains the agent's entrypoint
            (``main.py``, ``main.go``, ``Cargo.toml``, ...).
        http_port: JSON-RPC / HTTP port.
        grpc_port: gRPC port.
        log_dir: If set, agent stdout+stderr are appended to
            ``<log_dir>/<log_name>.log``. If unset, they are discarded.
            (Debug-log behaviour previously keyed off ``ITK_LOG_LEVEL`` in
            ``current.py``; the launcher makes the caller's intent explicit.)
        log_name: Basename for the log file. Defaults to
            ``agent_<agent_dir_name>_<8-hex-hash-of-full-path>`` — the hash
            prevents interleaving when several peers share a ``log_dir`` and
            all happen to root at ``itk/`` (which every SDK repo does).
            Callers that own naming (e.g. the runner) should pass an explicit
            name like ``agent_python_v10`` for readability.

    Raises:
        RuntimeError: The directory does not contain a recognised agent
            entrypoint, or a required lazy build failed.
    """
    if not agent_dir.exists():
        raise RuntimeError(f'agent dir does not exist: {agent_dir}')

    popen = _popen_factory(agent_dir, log_dir, log_name)

    if (agent_dir / 'main.go').exists():
        return _spawn_go(agent_dir, http_port, grpc_port, popen)

    if (agent_dir / 'main.py').exists():
        return _spawn_python(agent_dir, http_port, grpc_port, popen)

    if (agent_dir.parent / 'package.json').exists():
        return _spawn_ts(agent_dir, http_port, grpc_port, popen)

    csproj = list(agent_dir.glob('*.csproj'))
    if csproj:
        return _spawn_dotnet(agent_dir, csproj[0], http_port, grpc_port, popen)

    if (agent_dir / 'pom.xml').exists():
        return _spawn_java(agent_dir, http_port, grpc_port, popen)

    if (agent_dir / 'Cargo.toml').exists():
        return _spawn_rust(agent_dir, http_port, grpc_port, popen)

    raise RuntimeError(
        f'could not determine agent type in {agent_dir}. '
        f'Expected main.go, main.py, ../package.json, *.csproj, pom.xml, or Cargo.toml.'
    )


# ---------------------------------------------------------------------------
# Per-language spawn implementations
# ---------------------------------------------------------------------------


def _spawn_go(
    agent_dir: Path, http_port: int, grpc_port: int, popen: _PopenFactory
) -> subprocess.Popen:
    # -mod=readonly: never mutate go.mod/go.sum; fail loudly on drift.
    args = [
        'go', 'run', '-mod=readonly', 'main.go',
        '--httpPort', str(http_port),
        '--grpcPort', str(grpc_port),
    ]
    return popen(args, agent_dir)


def _spawn_python(
    agent_dir: Path, http_port: int, grpc_port: int, popen: _PopenFactory
) -> subprocess.Popen:
    # --locked: never re-resolve; fail if uv.lock is stale.
    args = [
        'uv', 'run', '--locked', 'main.py',
        '--httpPort', str(http_port),
        '--grpcPort', str(grpc_port),
    ]
    return popen(args, agent_dir)


def _spawn_ts(
    agent_dir: Path, http_port: int, grpc_port: int, popen: _PopenFactory
) -> subprocess.Popen:
    # TS agents live one level deep under a repo whose root has package.json.
    # ``npm run itk-agent`` is the convention every SDK repo exposes.
    args = [
        'npm', 'run', 'itk-agent', '--',
        '--httpPort', str(http_port),
        '--grpcPort', str(grpc_port),
    ]
    return popen(args, agent_dir)


def _spawn_dotnet(
    agent_dir: Path,
    csproj: Path,
    http_port: int,
    grpc_port: int,
    popen: _PopenFactory,
) -> subprocess.Popen:
    args = [
        'dotnet', 'run', '--project', str(csproj), '--',
        '--httpPort', str(http_port),
        '--grpcPort', str(grpc_port),
    ]
    return popen(args, agent_dir)


def _spawn_java(
    agent_dir: Path, http_port: int, grpc_port: int, popen: _PopenFactory
) -> subprocess.Popen:
    # The java itk agent is a Maven submodule; the parent pom needs -Pitk to
    # include it. Synchronously install SDK sibling deps into the local repo,
    # then exec the mock main class from inside the module directory.
    compile_args = [
        'mvn', '-Pitk', '-pl', 'itk', '-am', 'install',
        '-DskipTests', '-Dmaven.javadoc.skip=true',
    ]
    subprocess.run(compile_args, cwd=str(agent_dir.parent), check=True)  # noqa: S603

    args = [
        'mvn', 'exec:java',
        '-Dexec.mainClass=org.a2aproject.sdk.itk.Main',
        f'-Dexec.args=--httpPort {http_port} --grpcPort {grpc_port}',
    ]
    return popen(args, agent_dir)


def _spawn_rust(
    agent_dir: Path, http_port: int, grpc_port: int, popen: _PopenFactory
) -> subprocess.Popen:
    release_dir = agent_dir / 'target' / 'release'
    binary = _find_rust_binary(release_dir)
    if binary is None:
        # Lazy build for MOUNT/LOCAL. CHECKOUT trees already contain the
        # binary because builders.py built them under the cache lock.
        subprocess.run(  # noqa: S603
            ['cargo', 'build', '--locked', '--release'],  # noqa: S607
            cwd=str(agent_dir),
            check=True,
        )
        binary = _find_rust_binary(release_dir)
        if binary is None:
            raise RuntimeError(
                f'cargo build succeeded but no itk-* binary found in {release_dir}'
            )
    args = [
        str(binary),
        '--httpPort', str(http_port),
        '--grpcPort', str(grpc_port),
    ]
    return popen(args, agent_dir)


def _find_rust_binary(release_dir: Path) -> Path | None:
    if not release_dir.exists():
        return None
    # Prefer the canonical name if the caller built with a fixed bin name;
    # otherwise pick the first itk-* binary. Matches current.py's behaviour.
    canonical = release_dir / 'itk-current-agent'
    if canonical.exists():
        return canonical
    for candidate in sorted(release_dir.glob('itk-*')):
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# subprocess.Popen factory (with optional log-file redirection)
# ---------------------------------------------------------------------------


_PopenFactory = Callable[[list[str], Path], subprocess.Popen]


def _default_log_name(agent_dir: Path) -> str:
    """Hash-suffixed default so concurrent peers don't collide on ``itk``."""
    tag = hashlib.sha1(str(agent_dir).encode('utf-8')).hexdigest()[:8]  # noqa: S324
    return f'agent_{agent_dir.name}_{tag}'


def _popen_factory(
    agent_dir: Path,
    log_dir: Path | None,
    log_name: str | None = None,
) -> _PopenFactory:
    """Return a ``popen(args, cwd)`` callable that respects the log setting."""
    if log_dir is None:
        def popen(args: list[str], cwd: Path) -> subprocess.Popen:
            return subprocess.Popen(  # noqa: S603
                args,
                cwd=str(cwd),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        return popen

    log_dir.mkdir(parents=True, exist_ok=True)
    name = log_name or _default_log_name(agent_dir)
    log_path = log_dir / f'{name}.log'
    log_handle = open(log_path, 'a', encoding='utf-8')  # noqa: SIM115

    def popen(args: list[str], cwd: Path) -> subprocess.Popen:
        p = subprocess.Popen(  # noqa: S603
            args,
            cwd=str(cwd),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # Stash so LaunchSession.__exit__ can close the handle on teardown.
        # Direct callers of spawn_from_dir are responsible for closing it
        # themselves — the docstring says so.
        p._log_file = log_handle  # noqa: SLF001
        return p

    return popen


def _resolve_log_dir(env: dict[str, str] | None = None) -> Path | None:
    """Backward-compatible log-dir resolution honouring ``ITK_LOG_LEVEL=DEBUG``.

    Kept for callers that want the legacy env-driven behaviour. New code
    should pass ``log_dir=`` explicitly.
    """
    e = env if env is not None else os.environ
    if e.get('ITK_LOG_LEVEL', 'INFO').upper() != 'DEBUG':
        return None
    return Path.cwd() / 'logs'
