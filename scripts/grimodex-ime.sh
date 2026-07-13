#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)

BUILD_DIR=${BUILD_DIR:-"${REPO_ROOT}/build-grimodex"}
INSTALL_PREFIX=${INSTALL_PREFIX:-/usr}
GGML_VULKAN=${GGML_VULKAN:-OFF}
SERVER=${GRIMODEX_SERVER:-/usr/bin/fcitx5-grimodex-server}
RESTART_LOG=${GRIMODEX_RESTART_LOG:-"${HOME}/.cache/fcitx5-grimodex-restart.log"}

usage() {
    cat <<'EOF'
Usage: scripts/grimodex-ime.sh <command>

Commands:
  build    Configure and build the Grimodex IME
  install  Install the existing build with CMake
  restart  Replace the old IME server and restart Fcitx5
  all      Build, install, and restart Fcitx5

Environment overrides:
  BUILD_DIR, INSTALL_PREFIX, GGML_VULKAN, GRIMODEX_SERVER, GRIMODEX_RESTART_LOG
EOF
}

run_as_root() {
    if [[ ${EUID} -eq 0 ]]; then
        "$@"
    else
        sudo "$@"
    fi
}

build() {
    cmake -S "${REPO_ROOT}" -B "${BUILD_DIR}" \
        -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="${INSTALL_PREFIX}" \
        -DGGML_VULKAN="${GGML_VULKAN}"
    cmake --build "${BUILD_DIR}"
}

install() {
    run_as_root cmake --install "${BUILD_DIR}"
}

restart() {
    command -v fcitx5 >/dev/null
    command -v fcitx5-remote >/dev/null
    [[ -x "${SERVER}" ]] || {
        printf 'server executable not found: %s\n' "${SERVER}" >&2
        return 1
    }

    mkdir -p "$(dirname -- "${RESTART_LOG}")"
    nohup "${SERVER}" --replace \
        >"${RESTART_LOG}" 2>&1 </dev/null &
    sleep 2

    fcitx5 -rd
    sleep 2
    fcitx5-remote -s grimodex
    fcitx5-remote -o
    printf 'Current input method: '
    fcitx5-remote -n
}

main() {
    local command=${1:-all}
    case "${command}" in
        build) build ;;
        install) install ;;
        restart) restart ;;
        all)
            build
            install
            restart
            ;;
        -h|--help|help) usage ;;
        *)
            printf 'unknown command: %s\n\n' "${command}" >&2
            usage >&2
            return 2
            ;;
    esac
}

main "$@"
