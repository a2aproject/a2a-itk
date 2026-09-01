"""Polyglot agent spawn — the launcher's shared implementation.

:func:`spawn_from_dir` detects an agent's language from the contents of its
directory and starts it. Both launcher entry points go through it —
:mod:`test_suite.launcher.resolve` for single spawns and
:class:`test_suite.launcher.cluster.Cluster` for batches — and both the
``MOUNT`` (the SUT bind-mounted at ``agents/repo/itk``) and ``CHECKOUT``
(a peer fetched from its SDK repo) kinds, so no drift is possible between
how the code under test and its peers are started.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from tempfile import gettempdir


# ---------------------------------------------------------------------------
# Polyglot spawn body
# ---------------------------------------------------------------------------


def spawn_from_dir(
    agent_dir: Path,
    http_port: int,
    grpc_port: int,
    *,
    log_dir: Path | None = None,
    log_name: str | None = None,
    new_session: bool = False,
) -> subprocess.Popen:
    """Detect the agent's language and spawn it.

    Args:
        agent_dir: The directory that contains the agent's entrypoint
            (``main.py``, ``main.go``, ``Cargo.toml``, ...).
        http_port: JSON-RPC / HTTP port.
        grpc_port: gRPC port.
        log_dir: If set, agent stdout+stderr are appended to
            ``<log_dir>/<log_name>.log``. If unset, they are discarded.
        log_name: Basename for the log file. Defaults to
            ``agent_<agent_dir_name>_<8-hex-hash-of-full-path>`` — the hash
            prevents interleaving when several peers share a ``log_dir`` and
            all happen to root at ``itk/`` (which every SDK repo does).
            Callers that own naming (e.g. the runner) should pass an explicit
            name like ``agent_python_v10`` for readability.
        new_session: If True, spawn the child in its own POSIX session so
            ``proc.pid == pgid``. Only :class:`~test_suite.launcher.cluster.Cluster`
            passes True — its teardown signals the whole group via
            ``os.killpg`` to catch grandchildren (mvn -> java, npm -> tsx,
            go run -> compiled binary). Defaults to False so a caller that
            only signals the direct child via ``proc.terminate()`` doesn't
            silently leak those grandchildren.

    Returns:
        The spawned ``subprocess.Popen``. If ``log_dir`` was set, the Popen
        carries a ``_log_file`` attribute — the caller (usually
        :class:`test_suite.launcher.resolve.LaunchSession` or
        :class:`test_suite.launcher.cluster.Cluster`) is responsible for
        closing it on teardown.

    Raises:
        RuntimeError: The directory does not contain a recognised agent
            entrypoint, or a required lazy build failed. Callers should
            treat this as a permanent (non-retryable) configuration error.
    """
    if not agent_dir.exists():
        raise RuntimeError(f'agent dir does not exist: {agent_dir}')

    popen = _popen_factory(agent_dir, log_dir, log_name, new_session=new_session)

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


_PopenFactory = Callable[[list[str], Path], subprocess.Popen]


def _spawn_go(
    agent_dir: Path, http_port: int, grpc_port: int, popen: _PopenFactory,
) -> subprocess.Popen:
    # -mod=readonly: never mutate go.mod/go.sum; fail loudly on drift.
    args = [  # noqa: S607
        'go', 'run', '-mod=readonly', 'main.go',
        '--httpPort', str(http_port),
        '--grpcPort', str(grpc_port),
    ]
    return popen(args, agent_dir)


def _spawn_python(
    agent_dir: Path, http_port: int, grpc_port: int, popen: _PopenFactory,
) -> subprocess.Popen:
    # --locked: never re-resolve; fail if uv.lock is stale.
    args = [  # noqa: S607
        'uv', 'run', '--locked', 'main.py',
        '--httpPort', str(http_port),
        '--grpcPort', str(grpc_port),
    ]
    return popen(args, agent_dir)


def _spawn_ts(
    agent_dir: Path, http_port: int, grpc_port: int, popen: _PopenFactory,
) -> subprocess.Popen:
    # TS agents live one level deep under a repo whose root has package.json.
    # ``npm run itk-agent`` is the convention every SDK repo exposes.
    args = [  # noqa: S607
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
    args = [  # noqa: S607
        'dotnet', 'run', '--project', str(csproj), '--',
        '--httpPort', str(http_port),
        '--grpcPort', str(grpc_port),
    ]
    return popen(args, agent_dir)


def _spawn_java(
    agent_dir: Path, http_port: int, grpc_port: int, popen: _PopenFactory,
) -> subprocess.Popen:
    # The java itk agent is a Maven submodule; the parent pom needs -Pitk to
    # include it. Synchronously install SDK sibling deps into the local repo,
    # then exec the mock main class from inside the module directory.
    local_repo = maven_repo_dir(agent_dir)
    compile_args = [  # noqa: S607
        'mvn', '-Pitk', '-pl', 'itk', '-am', 'install',
        '-DskipTests', '-Dmaven.javadoc.skip=true',
        f'-Dmaven.repo.local={local_repo}',
    ]
    subprocess.run(  # noqa: S603
        compile_args,
        cwd=str(agent_dir.parent),
        check=True,
        timeout=int(os.environ.get('ITK_BUILD_TIMEOUT', str(10 * 60))),
    )

    args = [  # noqa: S607
        'mvn', 'exec:java',
        '-Dexec.mainClass=org.a2aproject.sdk.itk.Main',
        f'-Dexec.args=--httpPort {http_port} --grpcPort {grpc_port}',
        f'-Dmaven.repo.local={local_repo}',
    ]
    return popen(args, agent_dir)


def maven_repo_dir(agent_dir: Path) -> Path:
    """Writable Maven local repo isolated per resolved ``agent_dir``.

    ``current`` and ``java_v10`` both install the same
    ``1.3.1.Final-SNAPSHOT`` coordinates. Sharing ``~/.m2`` lets the last
    ``mvn install`` win, so one JVM can exec the other's jars.
    """
    root = Path(
        os.environ.get(
            'ITK_MAVEN_CURRENT_REPO_DIR',
            str(Path(gettempdir()) / 'itk-maven-repos'),
        )
    )
    digest = hashlib.sha1(  # noqa: S324
        str(agent_dir.resolve()).encode('utf-8')
    ).hexdigest()
    repo = root / digest
    repo.mkdir(parents=True, exist_ok=True)
    return repo


def rust_target_dir(agent_dir: Path) -> Path:
    """Writable ``CARGO_TARGET_DIR`` isolated per resolved ``agent_dir``.

    Every rust ITK tree emits the same binary name
    (``itk-rust-current-agent``). A shared target dir lets parallel
    ``current`` and ``rust_v10`` builds overwrite each other, so
    ``Cluster.start_all`` can exec the last-written binary twice.
    """
    root = Path(
        os.environ.get(
            'ITK_RUST_CURRENT_TARGET_DIR',
            str(Path(gettempdir()) / 'itk-rust-targets'),
        )
    )
    digest = hashlib.sha1(  # noqa: S324
        str(agent_dir.resolve()).encode('utf-8')
    ).hexdigest()
    target = root / digest
    target.mkdir(parents=True, exist_ok=True)
    return target


def _spawn_rust(
    agent_dir: Path, http_port: int, grpc_port: int, popen: _PopenFactory,
) -> subprocess.Popen:
    # Always build for current Rust agent so local source changes are used and
    # stale itk-* binaries from previous runs cannot mask regressions.
    build_env = os.environ.copy()
    rust_target_root = rust_target_dir(agent_dir)
    build_env['CARGO_TARGET_DIR'] = str(rust_target_root)
    subprocess.run(  # noqa: S603
        ['cargo', 'build', '--locked', '--release'],  # noqa: S607
        cwd=str(agent_dir),
        env=build_env,
        check=True,
        timeout=int(os.environ.get('ITK_BUILD_TIMEOUT', str(10 * 60))),
    )
    binary = _find_rust_binary(rust_target_root / 'release')
    if binary is None:
        raise RuntimeError(
            'cargo build succeeded but no itk-* binary found in '
            f'{rust_target_root / "release"}'
        )
    args = [  # noqa: S607
        str(binary),
        '--httpPort', str(http_port),
        '--grpcPort', str(grpc_port),
    ]
    return popen(args, agent_dir)


def _is_runnable_rust_binary(path: Path) -> bool:
    return path.is_file() and path.suffix != '.d' and os.access(path, os.X_OK)


def _find_rust_binary(release_dir: Path) -> Path | None:
    if not release_dir.exists():
        return None
    current_named = release_dir / 'itk-rust-current-agent'
    if _is_runnable_rust_binary(current_named):
        return current_named
    canonical = release_dir / 'itk-current-agent'
    if _is_runnable_rust_binary(canonical):
        return canonical
    for candidate in sorted(release_dir.glob('itk-*')):
        if _is_runnable_rust_binary(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------
# subprocess.Popen factory (with optional log-file redirection)
# ---------------------------------------------------------------------------


def _default_log_name(agent_dir: Path) -> str:
    """Hash-suffixed default so concurrent peers don't collide on ``itk``."""
    tag = hashlib.sha1(str(agent_dir).encode('utf-8')).hexdigest()[:8]  # noqa: S324
    return f'agent_{agent_dir.name}_{tag}'


def _popen_factory(
    agent_dir: Path,
    log_dir: Path | None,
    log_name: str | None = None,
    *,
    new_session: bool = False,
) -> _PopenFactory:
    """Return a ``popen(args, cwd)`` callable that respects the log setting.

    ``new_session=True`` makes the child its own process-group leader so a
    caller with ``killpg``-based teardown (i.e. :class:`Cluster`) can reap
    grandchildren. See ``spawn_from_dir`` docstring for why it defaults off.
    """
    if log_dir is None:
        def popen(args: list[str], cwd: Path) -> subprocess.Popen:
            return subprocess.Popen(  # noqa: S603
                args,
                cwd=str(cwd),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=new_session,
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
            start_new_session=new_session,
        )
        # Stash so LaunchSession/Cluster.__exit__ can close the handle on
        # teardown. Direct callers of spawn_from_dir are responsible for
        # closing it themselves — see the spawn_from_dir docstring.
        p._log_file = log_handle  # noqa: SLF001
        return p

    return popen
