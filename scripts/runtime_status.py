"""Inspect one installation before its shell wrapper prepares or connects it."""

import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tomllib


def connection_data_directory(root, explicit=None):
    from daeri.cli import data_directory

    if explicit is not None or os.environ.get('DAERI_DATA_DIR'):
        return data_directory(explicit)
    configuration = root / '.mcp.json'
    if configuration.is_symlink():
        raise OSError('The MCP configuration must belong to this installation.')
    try:
        configured = json.loads(configuration.read_text(encoding='utf-8'))['mcpServers']['runtime']['env']['DAERI_DATA_DIR']
        if isinstance(configured, str) and Path(configured).is_absolute():
            return data_directory(configured)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return data_directory().resolve()


def main():
    root = Path(__file__).resolve().parents[1]
    mode, *arguments = sys.argv[1:]
    if mode == '--check':
        try:
            expected = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
            if importlib.metadata.version('daeri-agent') != expected:
                return 10
            from daeri.cli import check_environment
            if not check_environment()['ready']:
                return 10
        except Exception:
            return 10
    from daeri.cli import _parser
    args = _parser().parse_args(arguments)
    directory = connection_data_directory(root, args.data_dir)
    if mode == '--data-dir':
        print(directory)
        return 0
    if args.command == 'profile' and args.profile_command == 'show':
        from daeri.core.errors import StoreError
        from daeri.infrastructure.storage import Store
        try:
            store = Store(directory)
            try:
                profile = store.get_profile(args.name_or_id)
            finally:
                store.close()
        except StoreError:
            return 0  # The requested CLI command reports its normal profile/storage error.
        if profile['embedding']['provider'] == 'local':
            try:
                import fastembed  # noqa: F401
            except ImportError:
                return 11
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('DAERI connection preparation failed. Check the runtime and user storage.', file=sys.stderr)
        raise SystemExit(1)
