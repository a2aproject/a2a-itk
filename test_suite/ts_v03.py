import os
import subprocess

from pathlib import Path


_ROOT_DIR = Path(__file__).parent.parent

_AGENT_DIR = _ROOT_DIR / 'agents/ts/v03'


def spawn_agent(http_port: int, grpc_port: int) -> subprocess.Popen:
    """Spawns the TypeScript v0.3 agent process.

    Same lifecycle as ``test_suite.ts_v10.spawn_agent``: JIT ``npm
    install`` on first use, then execute ``main.ts`` via bundled ``tsx``.
    The v0.3 agent listens on ``--httpPort`` (JSON-RPC + REST) and
    ``--grpcPort`` (gRPC) using ``@a2a-js/sdk@0.3.13``.

    Args:
        http_port: The port for the HTTP/JSON-RPC and REST interfaces.
        grpc_port: The port for the gRPC interface.

    Returns:
        subprocess.Popen: The spawned process object.
    """
    if not (_AGENT_DIR / 'node_modules').exists():
        subprocess.run(  # noqa: S603
            ['npm', 'install', '--no-audit', '--no-fund', '--silent'],  # noqa: S607
            cwd=_AGENT_DIR,
            check=True,
        )

    tsx = _AGENT_DIR / 'node_modules' / '.bin' / 'tsx'
    args = [
        str(tsx),
        'main.ts',
        '--httpPort',
        str(http_port),
        '--grpcPort',
        str(grpc_port),
    ]

    log_level = os.environ.get('ITK_LOG_LEVEL', 'INFO')
    if log_level.upper() == 'DEBUG':
        logs_dir = _ROOT_DIR / 'logs'
        logs_dir.mkdir(parents=True, exist_ok=True)
        stdout_file = open(logs_dir / 'agent_ts_v03.log', 'w')  # noqa: WPS515
        p = subprocess.Popen(  # noqa: S603
            args,
            cwd=_AGENT_DIR,
            stdout=stdout_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        p._log_file = stdout_file  # noqa: SLF001
        return p
    else:
        return subprocess.Popen(  # noqa: S603
            args,
            cwd=_AGENT_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
