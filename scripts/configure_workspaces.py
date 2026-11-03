"""Provision project Git workspaces as a versioned native harness S3 Skill."""

import argparse
import json

from daeri.config.project import ProjectSettings
from daeri.config.server import HarnessReadiness
from daeri.config.yaml import read_yaml
from daeri.infrastructure.workspace_deployment import configure_workspaces, workspace_plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-config', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--skill-uri', required=True)
    parser.add_argument('--apply', action='store_true')
    arguments = parser.parse_args()
    try:
        project = ProjectSettings.from_yaml(arguments.project_config)
        config = read_yaml(arguments.config)
        plan = workspace_plan(project, config, arguments.skill_uri)
        if arguments.apply:
            import boto3
            from botocore.config import Config
            options = {'region_name': project.region, 'config': Config(retries={'total_max_attempts': 1})}
            with (boto3.client('bedrock-agentcore-control', **options) as control,
                  boto3.client('s3', **options) as s3, boto3.client('ssm', **options) as ssm):
                result = configure_workspaces(control, s3, ssm, project, config, arguments.skill_uri,
                    HarnessReadiness(control, project.approval_hook_arn).check)
        else:
            result = {'project_id': project.project_id, 'workspace': config, 'skill_uri': plan['skill_uri'],
                      'files': list(plan['files'])}
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        parser.exit(2, 'Workspace configuration failed; check project resources, repository settings and the persistent mount.\n')


if __name__ == '__main__':
    main()
