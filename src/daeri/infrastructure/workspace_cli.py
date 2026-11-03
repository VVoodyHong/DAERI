"""Prepare or verify a task worktree using the provisioned project Git sources."""

import argparse
import json
from pathlib import Path

from daeri.core.errors import StoreError
from daeri.infrastructure.workspaces import GitWorkspaces


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('operation', choices=['prepare', 'status', 'verify'])
    parser.add_argument('task_id')
    parser.add_argument('repository')
    parser.add_argument('--reference', default='HEAD')
    parser.add_argument('--test', nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    try:
        config = json.loads(Path(arguments.config).read_text(encoding='utf-8'))
        if not isinstance(config, dict) or set(config) != {'root', 'repositories'}:
            raise StoreError('INVALID_INPUT', 'Supply workspace root and project repositories.')
        service = GitWorkspaces(config['root'], config['repositories'])
        if arguments.operation == 'prepare':
            result = service.prepare(arguments.task_id, arguments.repository, arguments.reference)
        elif arguments.operation == 'verify':
            result = service.verify(arguments.task_id, arguments.repository, arguments.reference, arguments.test)
        else:
            result = service.status(arguments.task_id, arguments.repository)
        print(json.dumps(result, ensure_ascii=False))
    except (StoreError, OSError, ValueError, TypeError) as error:
        message = error.message if isinstance(error, StoreError) else 'Check the workspace configuration and current repository state.'
        parser.exit(2, message + '\n')


if __name__ == '__main__':
    main()
