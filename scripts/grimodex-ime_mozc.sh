#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)

BUILD_DIR=${BUILD_DIR:-"${REPO_ROOT}/build-grimodex-mozc"}
INSTALL_PREFIX_IS_EXPLICIT=${INSTALL_PREFIX+x}
INSTALL_PREFIX=${INSTALL_PREFIX:-/usr}
GGML_VULKAN=${GGML_VULKAN:-ON}
SERVER=${GRIMODEX_SERVER:-}
MOZC_ARTIFACT_DIR=${MOZC_ARTIFACT_DIR:-${HAZKEY_SERVER_MOZC_ARTIFACT_DIR:-}}
MOZC_HELPER=${FCITX5_GRIMODEX_MOZC_HELPER:-}
MOZC_DATA=${FCITX5_GRIMODEX_MOZC_DATA:-}
MOZC_BACKEND=${GRIMODEX_MOZC_BACKEND:-mozc}
PYTHON3=${PYTHON3:-python3}
MOZC_VERIFIER=${GRIMODEX_MOZC_VERIFIER:-"${REPO_ROOT}/packaging/scripts/verify_mozc_artifact_bundle.py"}
MOZC_RUNTIME_ROOT=${GRIMODEX_MOZC_RUNTIME_ROOT:-"${XDG_RUNTIME_DIR:-${HOME}/.cache}/fcitx5-grimodex/mozc-runtime"}
RESTART_LOG=${GRIMODEX_RESTART_LOG:-"${HOME}/.cache/fcitx5-grimodex-mozc-restart.log"}

usage() {
    cat <<'EOF'
Usage: scripts/grimodex-ime_mozc.sh <command>

Commands:
  build    Configure and build the Grimodex IME with the Mozc sidecar
  install  Install the existing Mozc-enabled build with CMake
  restart  Replace the old IME server and restart Fcitx5 in Mozc mode
  all      Build, install, and restart Fcitx5 in Mozc mode

Mozc artifact input:
  Set MOZC_ARTIFACT_DIR (or HAZKEY_SERVER_MOZC_ARTIFACT_DIR) to a verified
  fixed sidecar bundle. An existing build directory may reuse its cached path.

Environment overrides:
  BUILD_DIR, INSTALL_PREFIX, GGML_VULKAN, GRIMODEX_SERVER, GRIMODEX_RESTART_LOG,
  FCITX5_GRIMODEX_MOZC_HELPER, FCITX5_GRIMODEX_MOZC_DATA, PYTHON3,
  GRIMODEX_MOZC_VERIFIER, GRIMODEX_MOZC_RUNTIME_ROOT,
  GRIMODEX_MOZC_BACKEND (mozc or mozc-hybrid; default: mozc)

The normal runner remains separate. This Mozc runner selects either the pure
Mozc backend or the experimental Mozc-first speculative hybrid.
EOF
}

validate_mozc_backend() {
    case ${MOZC_BACKEND} in
        mozc|mozc-hybrid) ;;
        *)
            printf 'Unsupported GRIMODEX_MOZC_BACKEND: %s\n' \
                "${MOZC_BACKEND}" >&2
            return 2
            ;;
    esac
}

run_as_root() {
    if [[ ${EUID} -eq 0 ]]; then
        "$@"
    else
        sudo "$@"
    fi
}

cmake_cache_value() {
    local name=$1
    local cache_file="${BUILD_DIR}/CMakeCache.txt"
    local cache_line

    [[ -f "${cache_file}" ]] || return 1
    cache_line=$(grep -m 1 -E "^${name}:[^=]*=" "${cache_file}") || return 1
    printf '%s\n' "${cache_line#*=}"
}

configured_mozc_artifact_dir() {
    local artifact_dir

    artifact_dir=$(cmake_cache_value HAZKEY_SERVER_MOZC_ARTIFACT_DIR) || return 1
    [[ -n "${artifact_dir}" ]] || return 1
    printf '%s\n' "${artifact_dir}"
}

resolve_mozc_artifact_dir() {
    if [[ -n "${MOZC_ARTIFACT_DIR}" ]]; then
        printf '%s\n' "${MOZC_ARTIFACT_DIR}"
        return 0
    fi
    if configured_mozc_artifact_dir; then
        return 0
    fi

    printf '%s\n' \
        'Mozc artifact bundle is required. Set MOZC_ARTIFACT_DIR before build.' \
        >&2
    return 2
}

validate_mozc_bundle() {
    local artifact_dir=$1

    [[ -f "${artifact_dir}/manifest.json" ]] || {
        printf 'Mozc bundle manifest not found: %s/manifest.json\n' \
            "${artifact_dir}" >&2
        return 1
    }
    [[ -x "${artifact_dir}/fcitx5-grimodex-mozc-helper" ]] || {
        printf 'Mozc bundle helper is not executable: %s/fcitx5-grimodex-mozc-helper\n' \
            "${artifact_dir}" >&2
        return 1
    }
    [[ -r "${artifact_dir}/mozc.data" ]] || {
        printf 'Mozc bundle data is not readable: %s/mozc.data\n' \
            "${artifact_dir}" >&2
        return 1
    }
}

canonicalize_mozc_artifact_dir() {
    local artifact_dir=$1

    [[ -d "${artifact_dir}" ]] || {
        printf 'Mozc artifact directory not found: %s\n' "${artifact_dir}" >&2
        return 1
    }
    cd -- "${artifact_dir}"
    pwd -P
}

resolve_install_path() {
    local prefix=$1
    local directory=$2

    if [[ ${directory} == /* ]]; then
        printf '%s\n' "${directory%/}"
    elif [[ ${prefix} == / ]]; then
        case ${directory} in
            usr|usr/*) printf '/%s\n' "${directory%/}" ;;
            *) printf '/usr/%s\n' "${directory%/}" ;;
        esac
    else
        printf '%s/%s\n' "${prefix%/}" "${directory%/}"
    fi
}

resolve_mozc_runtime_paths() {
    local prefix=${INSTALL_PREFIX}
    local bindir=bin
    local libdir=lib
    local datarootdir=share
    local datadir
    local cached_value
    local full_bindir=
    local full_libdir=
    local full_datadir=

    if [[ -z "${INSTALL_PREFIX_IS_EXPLICIT}" ]] \
        && cached_value=$(cmake_cache_value CMAKE_INSTALL_PREFIX) \
        && [[ -n "${cached_value}" ]]
    then
        prefix=${cached_value}
    fi
    if cached_value=$(cmake_cache_value CMAKE_INSTALL_BINDIR) \
        && [[ -n "${cached_value}" ]]
    then
        bindir=${cached_value}
    fi
    if cached_value=$(cmake_cache_value CMAKE_INSTALL_LIBDIR) \
        && [[ -n "${cached_value}" ]]
    then
        libdir=${cached_value}
    fi
    if cached_value=$(cmake_cache_value CMAKE_INSTALL_DATAROOTDIR) \
        && [[ -n "${cached_value}" ]]
    then
        datarootdir=${cached_value}
    fi
    if cached_value=$(cmake_cache_value CMAKE_INSTALL_DATADIR) \
        && [[ -n "${cached_value}" ]]
    then
        datadir=${cached_value}
    else
        datadir=${datarootdir}
    fi
    if [[ -z "${INSTALL_PREFIX_IS_EXPLICIT}" ]]; then
        full_bindir=$(cmake_cache_value GRIMODEX_INSTALL_FULL_BINDIR) || true
        full_libdir=$(cmake_cache_value GRIMODEX_INSTALL_FULL_LIBDIR) || true
        full_datadir=$(cmake_cache_value GRIMODEX_INSTALL_FULL_DATADIR) || true
    fi
    if [[ -z "${full_bindir}" ]]; then
        full_bindir=$(resolve_install_path "${prefix}" "${bindir}")
    fi
    if [[ -z "${full_libdir}" ]]; then
        full_libdir=$(resolve_install_path "${prefix}" "${libdir}")
    fi
    if [[ -z "${full_datadir}" ]]; then
        full_datadir=$(resolve_install_path "${prefix}" "${datadir}")
    fi

    if [[ -z "${SERVER}" ]]; then
        SERVER="${full_bindir}/fcitx5-grimodex-server"
    fi
    if [[ -z "${MOZC_HELPER}" ]]; then
        MOZC_HELPER="${full_libdir}/fcitx5-grimodex/fcitx5-grimodex-mozc-helper"
    fi
    if [[ -z "${MOZC_DATA}" ]]; then
        MOZC_DATA="${full_datadir}/fcitx5-grimodex/mozc/mozc.data"
    fi
}

build() {
    local artifact_dir
    artifact_dir=$(resolve_mozc_artifact_dir)
    artifact_dir=$(canonicalize_mozc_artifact_dir "${artifact_dir}")
    validate_mozc_bundle "${artifact_dir}"

    cmake -S "${REPO_ROOT}" -B "${BUILD_DIR}" \
        -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="${INSTALL_PREFIX}" \
        -DGGML_VULKAN="${GGML_VULKAN}" \
        -DHAZKEY_SERVER_MOZC_ARTIFACT_DIR="${artifact_dir}"
    cmake --build "${BUILD_DIR}"
}

install() {
    configured_mozc_artifact_dir >/dev/null || {
        printf 'build directory is not configured with Mozc artifacts: %s\n' \
            "${BUILD_DIR}" >&2
        return 1
    }
    run_as_root cmake --install "${BUILD_DIR}"
}

reject_conflicting_env_file() {
    local config_home=${XDG_CONFIG_HOME:-"${HOME}/.config"}
    local env_file="${config_home}/fcitx5-grimodex/env"

    [[ -f "${env_file}" ]] || return 0
    if grep -Eq \
        '^[[:space:]]*(export[[:space:]]+)?FCITX5_GRIMODEX_(CONVERTER|MOZC_HELPER|MOZC_DATA)[[:space:]]*=' \
        "${env_file}"
    then
        printf 'Mozc runtime variables in %s can override this script.\n' \
            "${env_file}" >&2
        printf '%s\n' \
            'Remove those assignments and pass helper/data overrides to this script instead.' \
            >&2
        return 1
    fi
}

validate_mozc_runtime() {
    [[ -x "${SERVER}" ]] || {
        printf 'server executable not found: %s\n' "${SERVER}" >&2
        return 1
    }
    [[ -x "${MOZC_HELPER}" ]] || {
        printf 'Mozc helper executable not found: %s\n' "${MOZC_HELPER}" >&2
        printf '%s\n' 'Run scripts/grimodex-ime_mozc.sh install first.' >&2
        return 1
    }
    [[ -r "${MOZC_DATA}" ]] || {
        printf 'Mozc data file not found: %s\n' "${MOZC_DATA}" >&2
        printf '%s\n' 'Run scripts/grimodex-ime_mozc.sh install first.' >&2
        return 1
    }
}

prepare_mozc_runtime() {
    local generation

    command -v "${PYTHON3}" >/dev/null || {
        printf 'Python interpreter not found: %s\n' "${PYTHON3}" >&2
        return 1
    }
    [[ -f "${MOZC_VERIFIER}" ]] || {
        printf 'Mozc runtime verifier not found: %s\n' "${MOZC_VERIFIER}" >&2
        return 1
    }
    if ! generation=$("${PYTHON3}" "${MOZC_VERIFIER}" \
        --prepare-installed-runtime \
        --helper "${MOZC_HELPER}" \
        --data "${MOZC_DATA}" \
        --runtime-root "${MOZC_RUNTIME_ROOT}")
    then
        printf '%s\n' \
            'Installed Mozc helper/data failed runtime snapshot preparation.' \
            >&2
        return 1
    fi
    [[ -d "${generation}" ]] || {
        printf 'Mozc runtime generation not found: %s\n' "${generation}" >&2
        return 1
    }
    MOZC_HELPER="${generation}/fcitx5-grimodex-mozc-helper"
    MOZC_DATA="${generation}/mozc.data"
    validate_mozc_runtime
}

restart() {
    local server_pid
    local server_status

    validate_mozc_backend
    command -v fcitx5 >/dev/null
    command -v fcitx5-remote >/dev/null
    reject_conflicting_env_file
    resolve_mozc_runtime_paths
    validate_mozc_runtime
    prepare_mozc_runtime

    # Export the exact opt-in selector before starting both the server and
    # Fcitx. The Fcitx addon may respawn the server later and must retain the
    # same backend and sidecar paths.
    export FCITX5_GRIMODEX_CONVERTER="${MOZC_BACKEND}"
    export FCITX5_GRIMODEX_MOZC_HELPER="${MOZC_HELPER}"
    export FCITX5_GRIMODEX_MOZC_DATA="${MOZC_DATA}"

    mkdir -p "$(dirname -- "${RESTART_LOG}")"
    nohup "${SERVER}" --replace \
        >"${RESTART_LOG}" 2>&1 </dev/null &
    server_pid=$!
    sleep 2
    if ! kill -0 "${server_pid}" 2>/dev/null; then
        if wait "${server_pid}"; then
            server_status=0
        else
            server_status=$?
        fi
        printf 'Mozc server exited during startup (status %s).\n' \
            "${server_status}" >&2
        if [[ -s "${RESTART_LOG}" ]]; then
            tail -n 20 "${RESTART_LOG}" >&2
        fi
        return 1
    fi

    fcitx5 -rd
    sleep 2
    fcitx5-remote -s grimodex
    fcitx5-remote -o
    printf 'Requested converter backend: %s\n' "${MOZC_BACKEND}"
    printf 'Mozc helper: %s\n' "${MOZC_HELPER}"
    printf 'Mozc data: %s\n' "${MOZC_DATA}"
    printf 'Restart log: %s\n' "${RESTART_LOG}"
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
            validate_mozc_backend
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
