"""Validate or explicitly apply DAERI MCP Runtime configuration."""

import argparse
import json

from pydantic import ValidationError

from daeri.config.runtime import RuntimeSettings
from daeri.config.yaml import read_yaml
from daeri.core.errors import StoreError
from daeri.infrastructure.runtime_deployment import configure_runtime, runtime_request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        settings = RuntimeSettings.model_validate(read_yaml(args.config))
        if args.apply:
            import boto3
            from botocore.config import Config
            with boto3.client('bedrock-agentcore-control', region_name=settings.server.region,
                              config=Config(retries={'total_max_attempts': 1})) as client:
                output = configure_runtime(client, settings)
        else:
            output = {'name': settings.name, 'runtime_id': settings.runtime_id,
                      'endpoint': settings.endpoint, 'request': runtime_request(settings)}
        print(json.dumps(output, ensure_ascii=False, indent=2))
    except StoreError as error:
        failure = {'error': {'code': error.code, 'message': error.message}}
        if getattr(error, 'runtime_id', None):
            failure['runtime_id'] = error.runtime_id
        parser.exit(2, json.dumps(failure, ensure_ascii=False) + '\n')
    except ValidationError:
        failure = {'error': {'code': 'INVALID_INPUT',
                            'message': 'Runtime configuration failed; check supported deployment inputs.'}}
        parser.exit(2, json.dumps(failure) + '\n')


if __name__ == '__main__':
    main()
