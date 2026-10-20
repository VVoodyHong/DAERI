#!/bin/sh
set -eu

DAERI_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
DAERI_PYTHON="$DAERI_ROOT/.venv/bin/python"

if [ ! -x "$DAERI_PYTHON" ]; then
    printf 'DAERI environment is not prepared. Run: sh "%s/scripts/setup.sh" --prepare\n' "$DAERI_ROOT" >&2
    exit 1
fi

cd "$DAERI_ROOT"
exec "$DAERI_PYTHON" -m daeri.server "$@"
