"""Check or explicitly publish one managed project from operator YAML."""

import argparse
from contextlib import ExitStack
import json

import boto3
from botocore.config import Config

from daeri.config.project import ProjectSettings
from daeri.config.server import HarnessReadiness
from daeri.core.errors import StoreError
from daeri.infrastructure.project_deployment import ProjectDeployment


def main(argv=None):
    parser = argparse.ArgumentParser(description='Verify shared project memory and publish a verified harness release.')
    parser.add_argument('--config', required=True)
    parser.add_argument('--apply', action='store_true', help='Create missing project memory and publish the configured release.')
    arguments = parser.parse_args(argv)
    try:
        settings = ProjectSettings.from_yaml(arguments.config)
        session = boto3.Session()
        with ExitStack() as resources:
            def client(service):
                value = session.client(service, region_name=settings.region,
                    config=Config(retries={'total_max_attempts': 1}, connect_timeout=10, read_timeout=60))
                resources.callback(value.close)
                return value

            control, ssm = client('bedrock-agentcore-control'), client('ssm')
            readiness = HarnessReadiness(control, settings.approval_hook_arn)
            deployment = ProjectDeployment(control, ssm, readiness.check)
            result = deployment.apply(settings) if arguments.apply else deployment.check(settings)
        print(json.dumps({'project': settings.project_id, 'deployment': result}, ensure_ascii=False))
        return 0
    except StoreError as error:
        print(json.dumps({'error': {'code': error.code, 'message': error.message}}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
