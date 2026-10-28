#!/bin/sh
set -eu
DAERI_CONNECT=false
DAERI_PREPARED=0
if [ "${1:-}" = --connect ]; then DAERI_CONNECT=true; shift; fi

runtime_unavailable() {
    printf '%s\n' '{"status":"error","data":null,"error":{"code":"RUNTIME_UNAVAILABLE","message":"DAERI \uc138\ud305 \uc0c1\ud0dc\ub97c \ud655\uc778\ud574 \uc8fc\uc138\uc694.","next_action":"setup"}}'
    exit 1
}
DAERI_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P) || runtime_unavailable
DAERI_ENVIRONMENT="$DAERI_ROOT/.venv"
DAERI_PYTHON="$DAERI_ENVIRONMENT/bin/python.exe"
read_cli_data_directory() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --data-dir) [ "$#" -gt 1 ] || return 0; shift; DAERI_CLI_DATA=$1 ;;
            --data-dir=*) DAERI_CLI_DATA=${1#--data-dir=} ;;
            *) break ;;
        esac
        DAERI_CLI_DATA_SET=true
        shift
    done
}

for DAERI_MARKER in "$DAERI_ROOT/.daeri-setup.lock" "$DAERI_ROOT/.venv.previous" "$DAERI_ROOT"/.venv.prepare.*; do
    if [ -e "$DAERI_MARKER" ] || [ -L "$DAERI_MARKER" ]; then runtime_unavailable; fi
done
if [ -L "$DAERI_ENVIRONMENT" ] || [ -L "$DAERI_ENVIRONMENT/bin" ]; then runtime_unavailable; fi
if [ ! -e "$DAERI_ENVIRONMENT" ] && [ "$DAERI_CONNECT" = false ]; then
    printf '%s\n' '{"status":"setup_required","message":"DAERI \uc138\ud305\uc774 \ud544\uc694\ud569\ub2c8\ub2e4. setup\uc744 \uc2e4\ud589\ud574 \uc8fc\uc138\uc694."}'
    exit 0
fi
for DAERI_VARIABLE in $(env | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p'); do
    case "$DAERI_VARIABLE" in
        UV_*|PYTHON*|_PYTHON*|VIRTUAL_ENV|VIRTUAL_ENV_PROMPT|CONDA_*|__PYVENV_LAUNCHER__|_OLD_VIRTUAL_PATH|_OLD_VIRTUAL_PYTHONHOME|PYLAUNCHER_DEBUG) unset "$DAERI_VARIABLE" ;;
    esac
done
cd "$DAERI_ROOT" || runtime_unavailable
if [ "$DAERI_CONNECT" = true ]; then
    [ ! -L "$DAERI_ROOT/.mcp.json" ] || runtime_unavailable
    DAERI_CLI_DATA_SET=false
    read_cli_data_directory "$@"
    if [ "$DAERI_CLI_DATA_SET" = true ]; then
        case "$DAERI_CLI_DATA" in
            '~') DAERI_DATA_DIR=$HOME ;;
            '~/'*) DAERI_DATA_DIR="$HOME/${DAERI_CLI_DATA#\~/}" ;;
            /*) DAERI_DATA_DIR=$DAERI_CLI_DATA ;;
            *) DAERI_DATA_DIR="$DAERI_ROOT/$DAERI_CLI_DATA" ;;
        esac
        export DAERI_DATA_DIR
    fi
    if [ -z "${DAERI_DATA_DIR:-}" ] && [ ! -x "$DAERI_PYTHON" ] && [ -f "$DAERI_ROOT/.mcp.json" ]; then
        DAERI_PIN=$(/usr/bin/plutil -extract mcpServers.runtime.env.DAERI_DATA_DIR raw -o - "$DAERI_ROOT/.mcp.json" 2>/dev/null) || DAERI_PIN=
        case "$DAERI_PIN" in /*) DAERI_DATA_DIR=$DAERI_PIN; export DAERI_DATA_DIR ;; esac
    fi
    if [ ! -x "$DAERI_PYTHON" ]; then
        sh "$DAERI_ROOT/scripts/setup.sh" --prepare >&2 || runtime_unavailable
        DAERI_PREPARED=1
    fi
fi
if [ ! -d "$DAERI_ENVIRONMENT" ] || [ ! -x "$DAERI_ENVIRONMENT" ] ||
   [ ! -d "$DAERI_ENVIRONMENT/bin" ] || [ ! -x "$DAERI_ENVIRONMENT/bin" ] ||
   [ ! -f "$DAERI_PYTHON" ] || [ ! -x "$DAERI_PYTHON" ]; then
    runtime_unavailable
fi
if [ "$DAERI_CONNECT" = true ]; then
    DAERI_DATA_DIR=$("$DAERI_PYTHON" -I "$DAERI_ROOT/scripts/runtime_status.py" --data-dir "$@") || runtime_unavailable
    export DAERI_DATA_DIR
    DAERI_STATUS=0
    "$DAERI_PYTHON" -I "$DAERI_ROOT/scripts/runtime_status.py" --check "$@" || DAERI_STATUS=$?
    if [ "$DAERI_STATUS" -eq 10 ]; then
        sh "$DAERI_ROOT/scripts/setup.sh" --prepare >&2 || runtime_unavailable
        DAERI_PREPARED=1
        DAERI_STATUS=0
        "$DAERI_PYTHON" -I "$DAERI_ROOT/scripts/runtime_status.py" --check "$@" || DAERI_STATUS=$?
    fi
    case "$DAERI_STATUS" in 0|11) ;; 2) exit 2 ;; *) runtime_unavailable ;; esac
    if [ "$DAERI_STATUS" -eq 11 ]; then
        sh "$DAERI_ROOT/scripts/setup.sh" --prepare-local-embedding >&2 || runtime_unavailable
        DAERI_PREPARED=1
        "$DAERI_PYTHON" -I "$DAERI_ROOT/scripts/runtime_status.py" --check "$@" || runtime_unavailable
    fi
    DAERI_CONNECTION=$(DAERI_RUNTIME_PREPARED=$DAERI_PREPARED "$DAERI_PYTHON" -I "$DAERI_ROOT/scripts/register_mcp.py" --report) || runtime_unavailable
    printf '%s\n' "$DAERI_CONNECTION" >&2
fi
exec "$DAERI_PYTHON" -I -m daeri.cli "$@"
