"""Preview or explicitly archive/rebuild one project's Memory evidence."""

import argparse
import json

from daeri.config.memory import MemoryArchiveSettings, build_memory_archive
from daeri.core.errors import StoreError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--restore-to', help='Fresh, independently owned target Memory identifier.')
    parser.add_argument('--apply', action='store_true', help='Write immutable archives or replay into the selected target.')
    args = parser.parse_args(argv)
    try:
        settings = MemoryArchiveSettings.from_yaml(args.config)
        with build_memory_archive(settings) as archive:
            options = {'catalog': settings.project_parameter_prefix, 'apply': args.apply}
            if args.restore_to:
                result = archive.replay(settings.project_id, settings.source_memory_id, args.restore_to, **options)
            elif settings.source_memory_id == 'receipts':
                result = archive.capture_receipts(settings.project_id, **options)
            else:
                result = archive.capture(settings.project_id, settings.source_memory_id, **options)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except StoreError as error:
        print(json.dumps({'error': {'code': error.code, 'message': error.message}}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
