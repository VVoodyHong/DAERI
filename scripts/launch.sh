#!/bin/sh
set -eu

for DAERI_VARIABLE in $(env | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p'); do
    case "$DAERI_VARIABLE" in
        UV_*|PYTHON*|_PYTHON*|VIRTUAL_ENV|VIRTUAL_ENV_PROMPT|CONDA_*|__PYVENV_LAUNCHER__|_OLD_VIRTUAL_PATH|_OLD_VIRTUAL_PYTHONHOME|PYLAUNCHER_DEBUG) unset "$DAERI_VARIABLE" ;;
    esac
done
DAERI_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
DAERI_PYTHON="$DAERI_ROOT/.venv/bin/python.exe"

if [ ! -x "$DAERI_PYTHON" ]; then
    printf 'DAERI environment is not prepared. Run: sh "%s/scripts/setup.sh" --prepare\n' "$DAERI_ROOT" >&2
    exit 1
fi

cd "$DAERI_ROOT"
exec "$DAERI_PYTHON" -I -m daeri.server "$@"
