#!/bin/bash
# Regenerates pb/instruction.ts from ../../../protos/instruction.proto.
# Same mechanics as agents/ts/v10/build_protos.sh — see that file for
# the rationale of the stage-and-cleanup dance.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_ROOT="$SCRIPT_DIR/../../.."
PROTO_SRC="$PROJECT_ROOT/protos/instruction.proto"

mkdir -p "$SCRIPT_DIR/protos"
cp "$PROTO_SRC" "$SCRIPT_DIR/protos/instruction.proto"

if [ ! -x "$SCRIPT_DIR/node_modules/.bin/buf" ]; then
  (cd "$SCRIPT_DIR" && npm install --no-audit --no-fund --silent \
    @bufbuild/buf ts-proto)
fi

(cd "$SCRIPT_DIR" && node_modules/.bin/buf generate)

rm -rf "$SCRIPT_DIR/protos"
