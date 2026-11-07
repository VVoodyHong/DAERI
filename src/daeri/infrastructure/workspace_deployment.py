"""Build and publish a self-contained Git Skill for an AgentCore Harness."""

from contextlib import closing
from hashlib import sha256
from importlib.resources import files
from io import BytesIO
import json
from pathlib import PurePosixPath
import re
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from botocore.exceptions import ClientError

from daeri.core.errors import StoreError
from daeri.core.projects import valid_project_key
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


def workspace_plan(project_id, config, skill_uri):
    if not valid_project_key(project_id):
        raise StoreError('INVALID_INPUT', 'Supply a valid project ID.')
    if not isinstance(config.get('root'), str) or '\x00' in config['root']:
        raise StoreError('INVALID_INPUT', 'Supply a workspace path under /mnt.')
    root = PurePosixPath(config['root'])
    uri = urlsplit(skill_uri)
    if (set(config) != {'root', 'repositories'} or not root.is_absolute()
            or len(root.parts) < 3 or root.parts[1] != 'mnt' or '..' in root.parts
            or uri.scheme != 's3' or not uri.netloc or uri.query or uri.fragment):
        raise StoreError('INVALID_INPUT', 'Supply project repositories, a workspace path under /mnt and an S3 Skill directory.')
    mount_path = '/mnt/' + root.parts[2]
    if len(mount_path) > 200 or not re.fullmatch(r'/mnt/[a-zA-Z0-9._-]+', mount_path):
        raise StoreError('INVALID_INPUT', 'Supply a workspace path with a valid AgentCore mount directory under /mnt.')
    bodies = workspace_files(config)
    digest = sha256(b''.join(name.encode() + body for name, body in sorted(bodies.items()))).hexdigest()
    directory = '/'.join(filter(None, (uri.path.strip('/'), project_id, digest))) + '/'
    deployed_uri = 's3://' + uri.netloc + '/' + directory
    return {'files': bodies, 'bucket': uri.netloc, 'prefix': directory,
            'skill_uri': deployed_uri, 'mount_path': mount_path}


def publish_workspace_skill(s3, plan):
    for name, body in plan['files'].items():
        location = {'Bucket': plan['bucket'], 'Key': plan['prefix'] + name}
        try:
            s3.put_object(**location, Body=body, IfNoneMatch='*')
        except ClientError as error:
            if error.response.get('Error', {}).get('Code') not in {'PreconditionFailed', '412'}:
                raise
            with closing(s3.get_object(**location)['Body']) as existing:
                if existing.read(len(body) + 1) != body:
                    raise StoreError('STORAGE_CONFLICT', 'The workspace Skill key already contains different content.') from None
