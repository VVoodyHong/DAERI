#!/bin/sh
set -eu

runtime_unavailable() {
    printf '%s\n' '{"status":"error","data":null,"error":{"code":"RUNTIME_UNAVAILABLE","message":"DAERI \uc138\ud305 \uc0c1\ud0dc\ub97c \ud655\uc778\ud574 \uc8fc\uc138\uc694.","next_action":"setup"}}'
    exit 1
}
DAERI_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P) || runtime_unavailable
DAERI_ENVIRONMENT="$DAERI_ROOT/.venv"
DAERI_PYTHON="$DAERI_ENVIRONMENT/bin/python.exe"

for DAERI_MARKER in "$DAERI_ROOT/.daeri-setup.lock" "$DAERI_ROOT/.venv.previous" "$DAERI_ROOT"/.venv.prepare.*; do
    if [ -e "$DAERI_MARKER" ] || [ -L "$DAERI_MARKER" ]; then runtime_unavailable; fi
done
if [ -L "$DAERI_ENVIRONMENT" ] || [ -L "$DAERI_ENVIRONMENT/bin" ]; then runtime_unavailable; fi
if [ ! -e "$DAERI_ENVIRONMENT" ]; then
    printf '%s\n' '{"status":"setup_required","message":"DAERI \uc138\ud305\uc774 \ud544\uc694\ud569\ub2c8\ub2e4. setup\uc744 \uc2e4\ud589\ud574 \uc8fc\uc138\uc694."}'
    exit 0
fi
if [ ! -d "$DAERI_ENVIRONMENT" ] || [ ! -x "$DAERI_ENVIRONMENT" ] ||
   [ ! -d "$DAERI_ENVIRONMENT/bin" ] || [ ! -x "$DAERI_ENVIRONMENT/bin" ] ||
   [ ! -f "$DAERI_PYTHON" ] || [ ! -x "$DAERI_PYTHON" ]; then
    runtime_unavailable
fi
for DAERI_VARIABLE in $(env | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p'); do
    case "$DAERI_VARIABLE" in
        UV_*|PYTHON*|_PYTHON*|VIRTUAL_ENV|VIRTUAL_ENV_PROMPT|CONDA_*|__PYVENV_LAUNCHER__|_OLD_VIRTUAL_PATH|_OLD_VIRTUAL_PYTHONHOME|PYLAUNCHER_DEBUG) unset "$DAERI_VARIABLE" ;;
    esac
done
cd "$DAERI_ROOT" || runtime_unavailable
exec "$DAERI_PYTHON" -I -m daeri.cli "$@"
