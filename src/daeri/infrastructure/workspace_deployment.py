"""Publish a self-contained Git Skill through native S3 Skill and session storage APIs."""

from hashlib import sha256
from importlib.resources import files
from io import BytesIO
import json
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from daeri.core.errors import StoreError
from daeri.infrastructure.project_catalog import SsmProjectCatalog
from daeri.infrastructure.project_deployment import ProjectDeployment
from daeri.infrastructure.workspaces import GitWorkspaces


def workspace_files(config):
    """The zip application uses only Python's standard library and Git."""
    GitWorkspaces(config['root'], config['repositories'])
    package = files('daeri')
    buffer = BytesIO()
    with ZipFile(buffer, 'w', compression=ZIP_DEFLATED) as archive:
        for name in ('__init__.py', 'core/__init__.py', 'infrastructure/__init__.py',
                     'core/errors.py', 'infrastructure/workspaces.py', 'infrastructure/workspace_cli.py'):
            body = package.joinpath(name).read_bytes()
            archive.writestr(ZipInfo('daeri/' + name), body, compress_type=ZIP_DEFLATED)
        archive.writestr(ZipInfo('__main__.py'), 'from daeri.infrastructure.workspace_cli import main\nmain()\n',
                         compress_type=ZIP_DEFLATED)
    return {'SKILL.md': package.joinpath('assets/source-workspace/SKILL.md').read_bytes(),
            'scripts/workspace.pyz': buffer.getvalue(),
            'assets/workspaces.json': json.dumps(config, sort_keys=True).encode()}


def workspace_plan(settings, config, skill_uri):
    root = PurePosixPath(config.get('root', ''))
    uri = urlsplit(skill_uri)
    if (set(config) != {'root', 'repositories'} or not root.is_absolute()
            or len(root.parts) < 3 or root.parts[1] != 'mnt' or '..' in root.parts
            or uri.scheme != 's3' or not uri.netloc or uri.query or uri.fragment):
        raise StoreError('INVALID_INPUT', 'Supply project repositories, a workspace path under /mnt and an S3 Skill directory.')
    bodies = workspace_files(config)
    digest = sha256(b''.join(name.encode() + body for name, body in sorted(bodies.items()))).hexdigest()
    prefix = '/'.join(filter(None, (uri.path.strip('/'), settings.project_id))) + '/'
    directory = prefix + digest + '/'
    deployed_uri = 's3://' + uri.netloc + '/' + directory
    return {'files': bodies, 'bucket': uri.netloc, 'prefix': directory,
            'owned_prefix': 's3://' + uri.netloc + '/' + prefix, 'skill_uri': deployed_uri}


def configure_workspaces(control, s3, ssm, settings, config, skill_uri, check_harness):
    plan = workspace_plan(settings, config, skill_uri)
    root = PurePosixPath(config['root'])
    project = SsmProjectCatalog(ssm, settings.project_parameter_prefix).get(settings.project_id)
    if project.harness_arn != settings.harness_arn or project.name != settings.name:
        raise StoreError('PROJECT_NOT_READY', 'The published orchestrator differs from the project deployment.')
    check_harness(project)
    harness = control.get_harness(harnessId=settings.harness_arn.rsplit('/', 1)[1])['harness']
    environment = harness.get('environment', {}).get('agentCoreRuntimeEnvironment', {})
    environment = {key: value for key, value in environment.items() if key in {
        'lifecycleConfiguration', 'networkConfiguration', 'filesystemConfigurations'}}
    existing_environment = dict(environment)
    mounts = environment.get('filesystemConfigurations', [])
    covered = any(root.is_relative_to(PurePosixPath(value['mountPath']))
                  for mount in mounts for value in mount.values())
    if not covered:
        if any('sessionStorage' in mount for mount in mounts):
            raise StoreError('INVALID_INPUT', 'Place task workspaces under the existing managed session mount.')
        environment['filesystemConfigurations'] = mounts + [{'sessionStorage': {'mountPath': str(root)}}]
    skills = [skill for skill in harness.get('skills', [])
              if not skill.get('s3', {}).get('uri', '').startswith(plan['owned_prefix'])]
    skills.append({'s3': {'uri': plan['skill_uri']}})
    for name, body in plan['files'].items():
        s3.put_object(Bucket=plan['bucket'], Key=plan['prefix'] + name, Body=body)
    if harness.get('skills') != skills or existing_environment != environment:
        control.update_harness(harnessId=settings.harness_arn.rsplit('/', 1)[1], skills=skills,
                               environment={'agentCoreRuntimeEnvironment': environment})
    return {'skill_uri': plan['skill_uri'], 'project': ProjectDeployment(control, ssm, check_harness).apply(settings)}
