"""Check or publish one project from CloudFormation deployment outputs."""

import argparse
import json

import boto3
from botocore.config import Config

from daeri.config.project import ProjectSettings
from daeri.config.server import HarnessReadiness
from daeri.core.errors import StoreError
from daeri.infrastructure.project_deployment import ProjectDeployment


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--apply', action='store_true', help='Publish the verified release in the project catalog.')
    arguments = parser.parse_args(argv)
    try:
        settings = ProjectSettings.from_yaml(arguments.config)
        session = boto3.Session()
        options = {'region_name': settings.region,
                   'config': Config(retries={'total_max_attempts': 1}, connect_timeout=10, read_timeout=60)}
        with (session.client('bedrock-agentcore-control', **options) as control,
              session.client('ssm', **options) as ssm):
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
