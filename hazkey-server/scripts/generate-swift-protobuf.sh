#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
output="${1:?usage: generate-swift-protobuf.sh OUTPUT_DIR}"
mkdir -p "$output"
output="$(cd "$output" && pwd -P)"

: "${PROTOC:?PROTOC is required}"
: "${PROTOC_GEN_SWIFT:?PROTOC_GEN_SWIFT is required}"

[[ "$("$PROTOC" --version)" == "libprotoc 3.21.12" ]]
[[ "$("$PROTOC_GEN_SWIFT" --version)" == "protoc-gen-swift 1.30.0" ]]

cd "$repo_root/protocol"
"$PROTOC" \
  --proto_path=. \
  "--plugin=protoc-gen-swift=$PROTOC_GEN_SWIFT" \
  "--swift_out=$output" \
  base.proto commands.proto config.proto mozc_sidecar.proto
