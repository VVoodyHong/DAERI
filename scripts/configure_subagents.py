"""Register project-scoped managed subagents using an existing Gateway and Lambda."""

import argparse
import json

from daeri.config.delegation import DelegationSettings
from daeri.config.project import ProjectSettings
from daeri.config.server import HarnessReadiness
from daeri.config.yaml import read_yaml
from daeri.core.errors import StoreError
from daeri.infrastructure.delegation import configure_subagents, gateway_target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--project-config', required=True)
    parser.add_argument('--apply', action='store_true')
    arguments = parser.parse_args()
    try:
        settings = DelegationSettings.model_validate(read_yaml(arguments.config))
        project = ProjectSettings.from_yaml(arguments.project_config)
        if settings.project_id != project.project_id:
            raise StoreError('INVALID_INPUT', 'Use the same project for subagents and their orchestrator.')
        if arguments.apply:
            import boto3
            from botocore.config import Config
            options = {'region_name': project.region, 'config': Config(retries={'total_max_attempts': 1})}
            with (boto3.client('bedrock-agentcore-control', **options) as control,
                  boto3.client('lambda', **options) as lambdas, boto3.client('ssm', **options) as ssm):
                result = configure_subagents(control, lambdas, ssm, settings, project,
                    HarnessReadiness(control, project.approval_hook_arn).check)
        else:
            result = gateway_target(settings)
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        parser.exit(2, 'Subagent configuration failed; check the project, Gateway and current resource state.\n')


if __name__ == '__main__':
    main()
