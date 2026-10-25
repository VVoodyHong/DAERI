#!/bin/sh
set -eu
umask 077

for DAERI_VARIABLE in $(env | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p'); do
    case "$DAERI_VARIABLE" in
        UV_*|PYTHON*|_PYTHON*|VIRTUAL_ENV|VIRTUAL_ENV_PROMPT|CONDA_*|__PYVENV_LAUNCHER__|_OLD_VIRTUAL_PATH|_OLD_VIRTUAL_PYTHONHOME|PYLAUNCHER_DEBUG) unset "$DAERI_VARIABLE" ;;
    esac
done
DAERI_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
. "$DAERI_ROOT/scripts/dependencies.env"

usage() {
    printf 'Usage: setup.sh [--check|--prepare|--prepare-local-embedding]\nDefault: local check only. --prepare downloads private uv, managed Python, and locked base dependencies. --prepare-local-embedding also includes the local embedding runtime. Preparation uses DAERI-private runtime and cache locations.\n'
}
if [ "$#" -gt 1 ]; then usage >&2; exit 2; fi
case "${1:---check}" in
    --check) ;;
    --prepare) ;;
    --prepare-local-embedding) ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac
DAERI_MODE="${1:---check}"

DAERI_CANDIDATE=
DAERI_DOWNLOAD=
cleanup() {
    [ -z "$DAERI_CANDIDATE" ] || rm -rf -- "$DAERI_CANDIDATE"
    [ -z "$DAERI_DOWNLOAD" ] || rm -rf -- "$DAERI_DOWNLOAD"
    rmdir "$DAERI_ROOT/.daeri-setup.lock" 2>/dev/null || :
}
uv_supported() {
    DAERI_UV_VERSION=$("$1" --version 2>/dev/null) || return 1
    case "$DAERI_UV_VERSION" in
        "uv $UV_INSTALL_VERSION"|"uv $UV_INSTALL_VERSION "*) return 0 ;;
        *) return 1 ;;
    esac
}
prepare_uv() {
    DAERI_TOOL_DIR="$DAERI_ROOT/.runtime"
    DAERI_UV="$DAERI_TOOL_DIR/uv.exe"
    if ! uv_supported "$DAERI_UV"; then
        case "$(uname -s):$(uname -m)" in
            Darwin:arm64|Darwin:aarch64) DAERI_ARCH=aarch64-apple-darwin; DAERI_HASH=$UV_DARWIN_ARM64_SHA256 ;;
            Darwin:x86_64) DAERI_ARCH=x86_64-apple-darwin; DAERI_HASH=$UV_DARWIN_X64_SHA256 ;;
            *) printf 'DAERI preparation is incomplete: unsupported OS or CPU.\n' >&2; exit 1 ;;
        esac
        for DAERI_TOOL in curl shasum tar; do
            command -v "$DAERI_TOOL" >/dev/null 2>&1 || { printf 'DAERI preparation is incomplete: required OS tool %s is unavailable.\n' "$DAERI_TOOL" >&2; exit 1; }
        done
        DAERI_DOWNLOAD=$(mktemp -d "${TMPDIR:-/tmp}/daeri-uv.XXXXXX")
        DAERI_ARCHIVE="$DAERI_DOWNLOAD/uv.tar.gz"
        printf 'Preparing private uv %s from the pinned official release.\n' "$UV_INSTALL_VERSION" >&2
        curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error --connect-timeout 15 --max-time 120 --output "$DAERI_ARCHIVE" "$UV_RELEASE_BASE/$UV_INSTALL_VERSION/uv-$DAERI_ARCH.tar.gz"
        DAERI_ACTUAL_HASH=$(shasum -a 256 "$DAERI_ARCHIVE" | awk '{print $1}')
        if [ "$DAERI_ACTUAL_HASH" != "$DAERI_HASH" ]; then
            printf 'DAERI preparation is incomplete: uv archive checksum mismatch.\n' >&2; exit 1
        fi
        tar -xzf "$DAERI_ARCHIVE" -C "$DAERI_DOWNLOAD" "uv-$DAERI_ARCH/uv"
        mkdir -p "$DAERI_TOOL_DIR"
        cp "$DAERI_DOWNLOAD/uv-$DAERI_ARCH/uv" "$DAERI_TOOL_DIR/uv.new"
        chmod 700 "$DAERI_TOOL_DIR/uv.new"
        uv_supported "$DAERI_TOOL_DIR/uv.new" || { printf 'DAERI preparation is incomplete: downloaded uv cannot run.\n' >&2; exit 1; }
        mv "$DAERI_TOOL_DIR/uv.new" "$DAERI_UV"
    fi
}

if [ "$DAERI_MODE" = --prepare ] || [ "$DAERI_MODE" = --prepare-local-embedding ]; then
    DAERI_DATA="${DAERI_DATA_DIR:-$HOME/Library/Application Support/DAERI}"
    case "$DAERI_DATA" in
        '~') DAERI_DATA="$HOME" ;;
        '~/'*) DAERI_DATA="$HOME/${DAERI_DATA#\~/}" ;;
    esac
    case "$DAERI_DATA" in
        /*) ;;
        *) printf 'DAERI_DATA_DIR must be an absolute path; current-user ~/ paths are supported.\n' >&2; exit 2 ;;
    esac
    if ! mkdir "$DAERI_ROOT/.daeri-setup.lock" 2>/dev/null; then
        printf 'DAERI preparation is already running or was interrupted. Check that no setup process is active before removing .daeri-setup.lock and retrying.\n' >&2
        exit 1
    fi
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' HUP TERM
    if [ -e "$DAERI_ROOT/.venv.previous" ] && [ ! -e "$DAERI_ROOT/.venv" ]; then
        mv "$DAERI_ROOT/.venv.previous" "$DAERI_ROOT/.venv"
    fi
    if [ -e "$DAERI_ROOT/.venv.previous" ]; then
        printf 'DAERI preparation is incomplete: inspect .venv.previous before retrying.\n' >&2
        exit 1
    fi
    DAERI_RUNTIME="$DAERI_DATA/runtime"
    mkdir -p "$DAERI_RUNTIME/python" "$DAERI_RUNTIME/cache"
    DAERI_RUNTIME=$(CDPATH= cd -- "$DAERI_RUNTIME" && pwd -P)
    UV_PYTHON_INSTALL_DIR="$DAERI_RUNTIME/python"
    UV_CACHE_DIR="$DAERI_RUNTIME/cache"
    UV_PYTHON_INSTALL_BIN=0
    UV_PYTHON_INSTALL_REGISTRY=0
    export UV_PYTHON_INSTALL_DIR UV_CACHE_DIR UV_PYTHON_INSTALL_BIN UV_PYTHON_INSTALL_REGISTRY
    prepare_uv
    DAERI_CANDIDATE=$(mktemp -d "$DAERI_ROOT/.venv.prepare.XXXXXX")
    cd "$DAERI_ROOT"
    "$DAERI_UV" venv --managed-python --no-config --relocatable --project "$DAERI_ROOT" "$DAERI_CANDIDATE"
    set -- sync --managed-python --no-config --frozen --no-dev --no-editable --project "$DAERI_ROOT"
    if [ "$DAERI_MODE" = --prepare-local-embedding ]; then set -- "$@" --extra local-embedding; fi
    UV_PROJECT_ENVIRONMENT="$DAERI_CANDIDATE" "$DAERI_UV" "$@"
    ln -s python "$DAERI_CANDIDATE/bin/python.exe"
    "$DAERI_CANDIDATE/bin/python.exe" -I -c 'import pathlib,sys; assert pathlib.Path(sys.prefix).resolve() == pathlib.Path(sys.argv[1]).resolve()' "$DAERI_CANDIDATE"
    if DAERI_CHECK_RESULT=$("$DAERI_CANDIDATE/bin/python.exe" -I -m daeri.cli check); then
        :
    else
        DAERI_CHECK_EXIT=$?
        printf '%s\n' "$DAERI_CHECK_RESULT" >&2
        exit "$DAERI_CHECK_EXIT"
    fi
    if [ -e "$DAERI_ROOT/.venv.previous" ]; then
        printf 'DAERI preparation is incomplete: a previous environment backup already exists; inspect it before retrying.\n' >&2
        exit 1
    fi
    if [ -e "$DAERI_ROOT/.venv" ]; then mv "$DAERI_ROOT/.venv" "$DAERI_ROOT/.venv.previous"; fi
    if ! mv "$DAERI_CANDIDATE" "$DAERI_ROOT/.venv"; then
        [ ! -e "$DAERI_ROOT/.venv.previous" ] || mv "$DAERI_ROOT/.venv.previous" "$DAERI_ROOT/.venv"
        exit 1
    fi
    if ! "$DAERI_ROOT/.venv/bin/python.exe" -I "$DAERI_ROOT/scripts/register_mcp.py"; then
        mv "$DAERI_ROOT/.venv" "$DAERI_CANDIDATE"
        [ ! -e "$DAERI_ROOT/.venv.previous" ] || mv "$DAERI_ROOT/.venv.previous" "$DAERI_ROOT/.venv"
        exit 1
    fi
    DAERI_CANDIDATE=
    # A checked new environment is published; remove only the old program environment.
    rm -rf -- "$DAERI_ROOT/.venv.previous"
    printf '%s\n' "$DAERI_CHECK_RESULT"
    exit 0
fi

DAERI_PYTHON="$DAERI_ROOT/.venv/bin/python.exe"
if [ ! -x "$DAERI_PYTHON" ]; then
    printf 'DAERI environment is not prepared. Run: sh "%s/scripts/setup.sh" --prepare\n' "$DAERI_ROOT" >&2
    exit 1
fi
cd "$DAERI_ROOT"
exec "$DAERI_PYTHON" -I -m daeri.cli check
