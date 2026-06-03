import os
import subprocess

from pathlib import Path


_ROOT_DIR = Path(__file__).parent.parent


def spawn_agent(http_port: int, grpc_port: int) -> subprocess.Popen:
    """Spawns the Java v1.0 agent process.

    Builds the Quarkus application if the runner JAR does not exist,
    then launches it with the specified ports as system properties.

    Args:
        http_port: The port for the HTTP/JSON-RPC interface.
        grpc_port: The port for the gRPC interface.

    Returns:
        subprocess.Popen: The spawned process object.
    """
    cwd = _ROOT_DIR / 'agents/java/v10'
    runner_jar = cwd / 'target' / 'quarkus-app' / 'quarkus-run.jar'

    if not runner_jar.exists():
        subprocess.run(  # noqa: S603, S607
            ['mvn', 'package', '-DskipTests'],
            cwd=cwd,
            check=True,
        )

    args = [  # noqa: S607
        'java',
        f'-Dquarkus.http.port={http_port}',
        f'-Dquarkus.grpc.server.port={grpc_port}',
        '-jar',
        str(runner_jar),
    ]

    log_level = os.environ.get('ITK_LOG_LEVEL', 'INFO')
    if log_level.upper() == 'DEBUG':
        logs_dir = _ROOT_DIR / 'logs'
        if not logs_dir.exists():
            raise RuntimeError(
                f"Logs directory '{logs_dir}' does not exist. Please create it or mount it."
            )
        stdout_file = open(logs_dir / 'agent_java_v10.log', 'w')  # noqa: SIM115

        p = subprocess.Popen(  # noqa: S603
            args,
            cwd=cwd,
            stdout=stdout_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        p._log_file = stdout_file  # noqa: SLF001
        return p
    else:
        return subprocess.Popen(  # noqa: S603
            args,
            cwd=cwd,
            stderr=subprocess.STDOUT,
            text=True,
        )
