"""Start the authenticated DAERI Streamable HTTP service."""

import argparse
import os

from daeri.core.errors import StoreError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('The port must be between 1 and 65535.')
    from daeri.config.server import ServerSettings, build_server
    import uvicorn

    try:
        if args.config:
            settings = ServerSettings.from_yaml(args.config)
        else:
            from pydantic import ValidationError
            try:
                settings = ServerSettings.model_validate_json(os.environ.get('DAERI_SERVER_CONFIG', ''))
            except ValidationError:
                raise StoreError('INVALID_INPUT', 'Supply a configuration file or the Runtime server configuration.') from None
        with build_server(settings) as services:
            uvicorn.run(services.mcp.streamable_http_app(), host=args.host, port=args.port)
    except StoreError as error:
        parser.exit(2, f'{error.code}: {error.message}\n')


if __name__ == '__main__':
    main()
