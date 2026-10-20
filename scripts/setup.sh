#!/bin/sh
set -eu
umask 077

DAERI_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
. "$DAERI_ROOT/scripts/dependencies.env"

usage() {
    printf 'Usage: setup.sh [--check|--prepare]\nDefault: local check only. --prepare may install user-local uv, update user shell PATH, and download Python and locked dependencies.\n'
}
if [ "$#" -gt 1 ]; then usage >&2; exit 2; fi
case "${1:---check}" in
    --check) ;;
    --prepare) ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac

DAERI_CANDIDATE=
DAERI_DOWNLOAD=
cleanup() {
    [ -z "$DAERI_CANDIDATE" ] || rm -rf -- "$DAERI_CANDIDATE"
    [ -z "$DAERI_DOWNLOAD" ] || rm -rf -- "$DAERI_DOWNLOAD"
    rmdir "$DAERI_ROOT/.daeri-setup.lock" 2>/dev/null || :
}
uv_supported() {
    DAERI_UV_VERSION=$("$1" --version 2>/dev/null) || return 1
    printf '%s\n' "$DAERI_UV_VERSION" | awk -v minimum="$UV_MIN_VERSION" '
      /^uv [0-9]+\.[0-9]+\.[0-9]+([[:space:]]|$)/ {
        split($2, v, "."); split(minimum, m, ".");
        if (v[1] != 0) exit 1;
        for (i=1; i<=3; i++) { if (v[i]+0 > m[i]+0) exit 0; if (v[i]+0 < m[i]+0) exit 1 }
        exit 0
      }
      !/^uv [0-9]+\.[0-9]+\.[0-9]+([[:space:]]|$)/ { exit 1 }'
}
prepare_uv() {
    DAERI_UV=$(command -v uv || :)
    if [ -n "$DAERI_UV" ] && uv_supported "$DAERI_UV"; then return; fi
    DAERI_TOOL_DIR="$HOME/.local/share/daeri/tools/uv/$UV_INSTALL_VERSION"
    DAERI_UV="$DAERI_TOOL_DIR/uv"
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
        printf 'Preparing uv %s from the pinned official release in the user tools directory.\n' "$UV_INSTALL_VERSION" >&2
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
    # Only the explicitly requested preparation changes user shell startup files.
    DAERI_PATH_LINE='export PATH="$HOME/.local/share/daeri/tools/uv/'"$UV_INSTALL_VERSION"':$PATH"'
    for DAERI_SHELL_PROFILE in "$HOME/.zprofile" "$HOME/.profile" "$HOME/.bash_profile"; do
        if [ -e "$DAERI_SHELL_PROFILE" ] && [ ! -f "$DAERI_SHELL_PROFILE" ]; then
            printf 'DAERI preparation is incomplete: user shell profile is not a regular file.\n' >&2; exit 1
        fi
        if ! [ -f "$DAERI_SHELL_PROFILE" ] || ! grep -Fqx "$DAERI_PATH_LINE" "$DAERI_SHELL_PROFILE"; then
            printf '\n# DAERI user-local runtime tools\n%s\n' "$DAERI_PATH_LINE" >> "$DAERI_SHELL_PROFILE"
        fi
    done
    PATH="$DAERI_TOOL_DIR:$PATH"
    export PATH
    printf 'User-local uv is ready. Restart the host so it receives the updated PATH; then reconnect the DAERI MCP server.\n' >&2
}

if [ "${1:---check}" = --prepare ]; then
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
    prepare_uv
    DAERI_CANDIDATE=$(mktemp -d "$DAERI_ROOT/.venv.prepare.XXXXXX")
    cd "$DAERI_ROOT"
    "$DAERI_UV" venv --relocatable --project "$DAERI_ROOT" "$DAERI_CANDIDATE"
    UV_PROJECT_ENVIRONMENT="$DAERI_CANDIDATE" "$DAERI_UV" sync --frozen --no-dev --no-editable --project "$DAERI_ROOT"
    "$DAERI_CANDIDATE/bin/python" -m daeri.cli check
    if [ -e "$DAERI_ROOT/.venv.previous" ]; then
        printf 'DAERI preparation is incomplete: a previous environment backup already exists; inspect it before retrying.\n' >&2
        exit 1
    fi
    if [ -e "$DAERI_ROOT/.venv" ]; then mv "$DAERI_ROOT/.venv" "$DAERI_ROOT/.venv.previous"; fi
    if ! mv "$DAERI_CANDIDATE" "$DAERI_ROOT/.venv"; then
        [ ! -e "$DAERI_ROOT/.venv.previous" ] || mv "$DAERI_ROOT/.venv.previous" "$DAERI_ROOT/.venv"
        exit 1
    fi
    DAERI_CANDIDATE=
    # A checked new environment is published; remove only the old program environment.
    rm -rf -- "$DAERI_ROOT/.venv.previous"
    exit 0
fi

DAERI_CHECK_UV=$(command -v uv || :)
if [ -z "$DAERI_CHECK_UV" ] || ! uv_supported "$DAERI_CHECK_UV"; then
    printf 'DAERI MCP launch tool uv is missing or unsupported in this host PATH. Run setup.sh --prepare, then restart the host and check its PATH.\n' >&2
    exit 1
fi
DAERI_PYTHON="$DAERI_ROOT/.venv/bin/python"
if [ ! -x "$DAERI_PYTHON" ]; then
    printf 'DAERI environment is not prepared. Run: sh "%s/scripts/setup.sh" --prepare\n' "$DAERI_ROOT" >&2
    exit 1
fi
cd "$DAERI_ROOT"
exec "$DAERI_PYTHON" -m daeri.cli check
