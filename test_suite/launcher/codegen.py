"""Per-language proto codegen for freshly-fetched agent trees.

The launcher provides the SDK build tool (uv/go/cargo/mvn/npm), but each
agent's runtime code imports proto stubs derived from
``a2a-itk/protos/instruction.proto``. Without this step, a freshly-fetched
CHECKOUT tree has no ``pyproto/``, no ``pb/``, no
``a2a-itk/agents/ts/v10/pb/``, and no reachable ``a2a-itk/protos`` for
``build.rs`` / ``protobuf-maven-plugin`` to read.

Per language (mirrors what each SDK's ``run_itk.sh`` does today):

* **python**: copy ``instruction.proto`` in, run ``grpc_tools.protoc``
  → ``pyproto/{instruction_pb2.py,instruction_pb2_grpc.py}``, patch the
  ``import`` to be relative (needed because ``pyproto`` is imported as
  a package).
* **go**: copy ``instruction.proto`` in, run ``protoc`` with
  ``protoc-gen-go`` + ``protoc-gen-go-grpc``, output to ``pb/``.
* **ts**: symlink ``a2a-itk/`` into the agent dir (the TS agent hard-codes
  ``import … from './a2a-itk/agents/ts/v10/pb/instruction.js'``), stage
  the proto under ``…/agents/ts/v10/protos/``, run ``buf generate`` from
  the a2a-js SDK's ``node_modules/.bin/buf``.
* **rust**: symlink ``a2a-itk/`` into the agent dir. ``build.rs`` looks at
  ``a2a-itk/protos/instruction.proto`` and runs ``prost_build`` itself
  when cargo triggers the build script.
* **java**: symlink ``a2a-itk/`` into the agent dir. ``protobuf-maven-plugin``
  reads ``${project.basedir}/a2a-itk/protos`` when ``mvn install`` runs.
* **dotnet**: no-op.

Idempotency: safe to call twice for a given agent_dir. Python/Go skip if
their output files already exist. TS deliberately regenerates every call
(mirrors ``run_itk.sh``'s policy that ``pb/instruction.ts`` can lag the
authoritative ``.proto``).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


# ---------------------------------------------------------------------------
# Default sources — the launcher's own a2a-itk clone
# ---------------------------------------------------------------------------


def default_proto_source() -> Path:
    """Path to ``a2a-itk/protos/instruction.proto`` (launcher's own copy)."""
    return _repo_root() / 'protos' / 'instruction.proto'


def default_itk_source() -> Path:
    """Path to the a2a-itk repo root (launcher's own copy)."""
    return _repo_root()


def _repo_root() -> Path:
    """Return the a2a-itk root — mirrors :func:`test_suite.launcher.config._repo_root`."""
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Per-language setup — public, dispatched from builders.build_in_place
# ---------------------------------------------------------------------------


def prepare_python(
    agent_dir: Path,
    *,
    proto_source: Path | None = None,
    timeout: int | None = None,
) -> None:
    """Copy proto in, run grpc_tools.protoc, patch generated import."""
    proto = proto_source or default_proto_source()
    pyproto = agent_dir / 'pyproto'
    if (pyproto / 'instruction_pb2.py').exists():
        return  # already generated for this tree

    proto_local = agent_dir / 'instruction.proto'
    shutil.copyfile(str(proto), str(proto_local))
    pyproto.mkdir(exist_ok=True)
    (pyproto / '__init__.py').touch()

    subprocess.run(  # noqa: S603
        [  # noqa: S607
            'uv', 'run', '--with', 'grpcio-tools',
            'python', '-m', 'grpc_tools.protoc',
            '-I.', '--python_out=pyproto', '--grpc_python_out=pyproto',
            'instruction.proto',
        ],
        cwd=str(agent_dir),
        check=True,
        timeout=timeout,
        capture_output=True,
    )

    grpc_file = pyproto / 'instruction_pb2_grpc.py'
    if grpc_file.exists():
        text = grpc_file.read_text(encoding='utf-8')
        # protoc emits `import instruction_pb2 as instruction__pb2` which
        # doesn't work when pyproto/ is imported as a package. Rewrite it
        # to a relative import — exact same fix run_itk.sh does with sed.
        text = text.replace(
            'import instruction_pb2 as instruction__pb2',
            'from . import instruction_pb2 as instruction__pb2',
        )
        grpc_file.write_text(text, encoding='utf-8')


def prepare_go(
    agent_dir: Path,
    *,
    proto_source: Path | None = None,
    timeout: int | None = None,
) -> None:
    """Copy proto in, run protoc with go plugins → ``pb/instruction.pb.go``."""
    proto = proto_source or default_proto_source()
    pb = agent_dir / 'pb'
    if (pb / 'instruction.pb.go').exists():
        return

    proto_local = agent_dir / 'instruction.proto'
    shutil.copyfile(str(proto), str(proto_local))
    pb.mkdir(exist_ok=True)

    # protoc-gen-go{,-grpc} must be on PATH. The fat image installs them
    # via `go install` in the Dockerfile; local dev needs GOBIN on PATH.
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            'protoc', '-I.',
            '--go_out=pb',
            '--go_opt=Minstruction.proto=github.com/a2aproject/a2a-go/itk/pb',
            '--go_opt=paths=source_relative',
            '--go-grpc_out=pb',
            '--go-grpc_opt=Minstruction.proto=github.com/a2aproject/a2a-go/itk/pb',
            '--go-grpc_opt=paths=source_relative',
            'instruction.proto',
        ],
        cwd=str(agent_dir),
        check=True,
        timeout=timeout,
        capture_output=True,
    )


def prepare_ts(
    agent_dir: Path,
    *,
    proto_source: Path | None = None,
    itk_source: Path | None = None,  # noqa: ARG001 — kept for dispatcher parity
    timeout: int | None = None,
) -> None:
    """Stage the proto next to the SDK's own ``buf.gen.yaml`` and run buf.

    Each SDK's ``itk/`` owns its ``buf.gen.yaml`` (``out: ./pb``, ``inputs:
    directory: protos``); we copy the authoritative ``instruction.proto``
    into ``<agent_dir>/protos/``, invoke ``buf generate`` from
    ``<agent_dir>``, and the generated ``pb/instruction.ts`` lands right
    next to ``main.ts`` / ``itk_agent.ts``. Regenerating on every call
    matches ``run_itk.sh``'s policy (committed ``pb/`` can lag the proto).

    No symlink into a2a-itk any more — S17 deletes ``agents/`` and the
    old symlink target vanishes; the SDK owns its codegen config now.
    """
    proto = proto_source or default_proto_source()
    if not (agent_dir / 'buf.gen.yaml').exists():
        raise RuntimeError(
            f'{agent_dir}/buf.gen.yaml missing — each SDK itk/ must own its '
            'buf codegen config so the launcher does not need to reach into '
            'a2a-itk/agents/ts/*/. Copy the reference config from '
            'a2a-itk/agents/ts/v10/buf.gen.yaml (or the SDK\'s baseline).'
        )
    protos_stage = agent_dir / 'protos'
    protos_stage.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(str(proto), str(protos_stage / 'instruction.proto'))
        buf = agent_dir.parent / 'node_modules' / '.bin' / 'buf'
        subprocess.run(  # noqa: S603
            [str(buf), 'generate'],
            cwd=str(agent_dir),
            check=True,
            timeout=timeout,
            capture_output=True,
        )
    finally:
        shutil.rmtree(protos_stage, ignore_errors=True)


def prepare_rust(
    agent_dir: Path,
    *,
    itk_source: Path | None = None,
    **_kw,
) -> None:
    """Symlink a2a-itk. ``build.rs`` runs prost_build during ``cargo build``."""
    itk = itk_source or default_itk_source()
    ensure_itk_link(agent_dir, itk)


def prepare_java(
    agent_dir: Path,
    *,
    itk_source: Path | None = None,
    **_kw,
) -> None:
    """Symlink a2a-itk. ``protobuf-maven-plugin`` picks it up during ``mvn``."""
    itk = itk_source or default_itk_source()
    ensure_itk_link(agent_dir, itk)


def prepare_dotnet(agent_dir: Path, **_kw) -> None:  # noqa: ARG001
    """No-op — the reference .NET path doesn't consume the proto today."""
    return


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def ensure_itk_link(agent_dir: Path, itk_source: Path) -> None:
    """Idempotently create ``<agent_dir>/a2a-itk`` → ``itk_source`` symlink.

    If ``<agent_dir>/a2a-itk`` is already a symlink pointing at ``itk_source``,
    nothing happens. If it's a symlink to a different target, we replace it.
    If it's a real directory (developer clone), we leave it alone — the
    caller's intent is presumably to use that clone.
    """
    link = agent_dir / 'a2a-itk'
    itk_source = itk_source.resolve()
    if link.is_symlink():
        try:
            if Path(str(link.readlink())).resolve() == itk_source:
                return
        except OSError:
            pass
        link.unlink()
    elif link.exists():
        # Real directory — don't touch. Caller's decision.
        return
    link.symlink_to(itk_source)
