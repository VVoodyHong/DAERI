"""Build and optionally upload a content-addressed Git Skill to S3."""

import argparse
import json

from daeri.config.yaml import read_yaml
from daeri.core.errors import StoreError
from daeri.infrastructure.workspace_deployment import publish_workspace_skill, workspace_plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-id', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--skill-uri', required=True)
    parser.add_argument('--region')
    parser.add_argument('--apply', action='store_true')
    arguments = parser.parse_args(argv)
    try:
        config = read_yaml(arguments.config)
        plan = workspace_plan(arguments.project_id, config, arguments.skill_uri)
        if arguments.apply:
            import boto3
            from botocore.config import Config
            with boto3.client('s3', region_name=arguments.region,
                              config=Config(retries={'total_max_attempts': 1})) as s3:
                publish_workspace_skill(s3, plan)
        result = {'project_id': arguments.project_id, 'skill_uri': plan['skill_uri'],
                  'mount_path': plan['mount_path'], 'files': list(plan['files'])}
        print(json.dumps(result, ensure_ascii=False))
    except StoreError as error:
        parser.exit(2, json.dumps({'error': {'code': error.code, 'message': error.message}}, ensure_ascii=False) + '\n')
    except Exception:
        parser.exit(2, json.dumps({'error': {'code': 'DEPLOYMENT_FAILED',
            'message': 'Workspace Skill publication failed. Check the S3 destination, region and permissions.'}}) + '\n')


if __name__ == '__main__':
    main()
