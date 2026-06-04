import os
import subprocess

from pathlib import Path


_ROOT_DIR = Path(__file__).parent.parent

_AGENT_DIR = _ROOT_DIR / 'agents/rust/v10'


def spawn_agent(http_port: int, grpc_port: int) -> subprocess.Popen:
    """Spawns the Rust v1.0 agent process.

    The binary is compiled on first use via ``cargo build --release``.
    Subsequent launches reuse the cached binary under
    ``agents/rust/v10/target/release/``.

    Args:
        http_port: The port for the HTTP/JSON-RPC and REST interfaces.
        grpc_port: The port for the gRPC interface.

    Returns:
        subprocess.Popen: The spawned process object.
    """
    binary = _AGENT_DIR / 'target' / 'release' / 'itk-rust-v10-agent'

    if not binary.exists():
        subprocess.run(  # noqa: S603
            ['cargo', 'build', '--release'],  # noqa: S607
            cwd=_AGENT_DIR,
            check=True,
        )

    args = [  # noqa: S607
        str(binary),
        '--httpPort',
        str(http_port),
        '--grpcPort',
        str(grpc_port),
    ]

    log_level = os.environ.get('ITK_LOG_LEVEL', 'INFO')
    if log_level.upper() == 'DEBUG':
        logs_dir = _ROOT_DIR / 'logs'
        logs_dir.mkdir(parents=True, exist_ok=True)
        with open(logs_dir / 'agent_rust_v10.log', 'w') as stdout_file:  # noqa: WPS515
            p = subprocess.Popen(  # noqa: S603
                args,
                stdout=stdout_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
        return p
    else:
        return subprocess.Popen(  # noqa: S603
            args,
            stderr=subprocess.STDOUT,
            text=True,
        )
