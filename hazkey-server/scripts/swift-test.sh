#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
package_dir="$(cd -- "$script_dir/.." && pwd)"
swift_executable="${SWIFT_EXECUTABLE:-swift}"
scratch_path="${SWIFT_SCRATCH_PATH:-$package_dir/.build}"

"$swift_executable" package resolve \
  --package-path "$package_dir" \
  --scratch-path "$scratch_path"

cmake \
  -DSWIFT_SCRATCH_PATH="$scratch_path" \
  -P "$package_dir/prepare_azookey_dependency.cmake"

exec "$swift_executable" test \
  --package-path "$package_dir" \
  --scratch-path "$scratch_path" \
  "$@"
