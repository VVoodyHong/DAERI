"""Publish the installed plugin's MCP entry after its runtime passes setup."""

import json
import os
from pathlib import Path
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    python = root / '.venv/bin/python.exe'
    destination = root / '.mcp.json'
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RuntimeError('The plugin runtime is unavailable.')
    if destination.is_symlink():
        raise RuntimeError('The MCP configuration must belong to this installation.')
    config = {'mcpServers': {'operations': {
        'command': str(python),
        'args': ['-I', '-m', 'daeri.server'],
        'env': {'__PYVENV_LAUNCHER__': ''},
    }}}
    candidate = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=root, prefix='.mcp.prepare.', delete=False,
        ) as output:
            candidate = Path(output.name)
            json.dump(config, output, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        candidate.replace(destination)
    finally:
        if candidate is not None:
            candidate.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError):
        print('DAERI MCP registration failed. Retry setup.', file=sys.stderr)
        raise SystemExit(1)
