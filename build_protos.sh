#!/bin/bash

# Regenerate pyproto/ from two protos:
#
#   * protos/instruction.proto — ITK's own agent-instruction contract.
#   * the A2A service definition, fetched from the specification repo at the
#     tag in A2A_SPEC_REF below. Not vendored: the spec owns it, and pinning a
#     tag says which revision these stubs were built from without forking it.
#
# The generated stubs are committed so a bare checkout can `uv run pytest`
# without a protoc toolchain or network. Re-run this after editing
# instruction.proto, or after moving A2A_SPEC_REF.
#
# Peers fetched by the launcher get their own instruction stubs generated on
# the fly by test_suite/launcher/codegen.py::prepare_python — keep the two in
# sync.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

# The A2A specification revision these stubs are generated from. Moving this
# is a deliberate act: regenerate, run the tests, and commit the result.
A2A_SPEC_REF="v1.0.0"
A2A_PROTO_URL="https://raw.githubusercontent.com/a2aproject/A2A/${A2A_SPEC_REF}/specification/a2a.proto"

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

# a2a.proto imports google/api/{annotations,client,field_behavior}.proto.
# googleapis-common-protos ships those .proto sources next to its generated
# modules, so site-packages doubles as the include path — no separate
# googleapis checkout needed.
INCLUDE_ROOT="$(uv run python -c 'import google.rpc.status_pb2 as m, pathlib; print(pathlib.Path(m.__file__).parents[2])')"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

# The file must be named a2a.proto at the include root: protoc records the
# path it was compiled under, and the generated module identifies itself by it.
curl -sSfL "${A2A_PROTO_URL}" -o "${WORK_DIR}/a2a.proto"

uv run --with grpcio-tools python -m grpc_tools.protoc \
	-I"${WORK_DIR}" \
	-I"${INCLUDE_ROOT}" \
	--python_out=pyproto \
	--grpc_python_out=pyproto \
	a2a.proto

sed -i 's/^import a2a_pb2 as a2a__pb2/from . import a2a_pb2 as a2a__pb2/' \
	pyproto/a2a_pb2_grpc.py

echo "Done. Generated files are in pyproto/ (A2A spec ${A2A_SPEC_REF})."
