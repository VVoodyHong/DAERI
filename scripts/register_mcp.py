"""Publish the installed plugin's MCP entry after its runtime passes setup."""

import json
import os
from pathlib import Path
import runpy
import sys
import tempfile


def main(*, report=False):
    root = Path(__file__).resolve().parents[1]
    python = root / '.venv/bin/python.exe'
    destination = root / '.mcp.json'
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RuntimeError('The plugin runtime is unavailable.')
    if destination.is_symlink():
        raise RuntimeError('The MCP configuration must belong to this installation.')
    directory = runpy.run_path(str(root / 'scripts/runtime_status.py'))['connection_data_directory'](root)
    config = {'mcpServers': {'runtime': {
        'command': str(python),
        'args': ['-I', '-m', 'daeri.server'],
        'env': {'__PYVENV_LAUNCHER__': '', 'DAERI_DATA_DIR': str(directory)},
    }}}
    try:
        changed = json.loads(destination.read_text(encoding='utf-8')) != config
    except (OSError, ValueError):
        changed = True
    next_action = 'reload_host' if changed or os.environ.get('DAERI_RUNTIME_PREPARED') == '1' else 'check_host_connection'
    if not changed:
        if report:
            print(json.dumps({'mcp_config_changed': False, 'data_dir': str(directory),
                              'next_action': next_action}))
        return
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
    if report:
        print(json.dumps({'mcp_config_changed': True, 'data_dir': str(directory), 'next_action': 'reload_host'}))


if __name__ == '__main__':
    try:
        main(report=sys.argv[1:] == ['--report'])
    except Exception:
        print('DAERI MCP registration failed. Retry setup.', file=sys.stderr)
        raise SystemExit(1)
