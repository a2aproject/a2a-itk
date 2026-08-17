#!/bin/bash

# Regenerate pyproto/ from the authoritative protos/instruction.proto.
#
# The generated stubs are committed so a bare checkout can `uv run pytest`
# without a protoc toolchain. Re-run this after editing instruction.proto.
#
# Peers fetched by the launcher get their own stubs generated on the fly by
# test_suite/launcher/codegen.py::prepare_python — keep the two in sync.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

mkdir -p pyproto
touch pyproto/__init__.py

uv run --with grpcio-tools python -m grpc_tools.protoc \
	-Iprotos \
	--python_out=pyproto \
	--grpc_python_out=pyproto \
	protos/instruction.proto

# protoc emits `import instruction_pb2 as instruction__pb2`, which doesn't
# resolve when pyproto/ is imported as a package.
sed -i 's/^import instruction_pb2 as instruction__pb2/from . import instruction_pb2 as instruction__pb2/' \
	pyproto/instruction_pb2_grpc.py

echo "Done. Generated files are in pyproto/"
