"""Native S3 Skills provision the actual Git helper into managed harness sessions."""

from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError
import pytest

from daeri.core.errors import StoreError


def test_uploaded_skill_runs_without_an_installed_daeri_package(tmp_path):
    from daeri.infrastructure.workspace_deployment import workspace_files
    from test_workspaces import repository
    source = repository(tmp_path / 'source')
    config = {'root': str(tmp_path / 'work'), 'repositories': {'api': str(source)}}
    for name, body in workspace_files(config).items():
        path = tmp_path / 'skill' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    result = subprocess.run([sys.executable, '-I', str(tmp_path / 'skill/scripts/workspace.pyz'),
        '--config', str(tmp_path / 'skill/assets/workspaces.json'), 'prepare', str(uuid4()), 'api'],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert Path(json.loads(result.stdout)['path']).is_dir()


def test_command_previews_mount_and_publishes_only_a_content_addressed_s3_bundle(tmp_path, monkeypatch, capsys):
    from scripts.configure_workspaces import main

    class S3:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def put_object(self, *, Bucket, Key, Body, IfNoneMatch):
            assert Bucket == 'workspace-skills' and IfNoneMatch == '*'
            if Key in objects:
                raise ClientError({'Error': {'Code': 'PreconditionFailed'}}, 'PutObject')
            objects[Key] = Body

        def get_object(self, *, Bucket, Key):
            assert Bucket == 'workspace-skills'
            return {'Body': BytesIO(objects[Key])}

    objects, clients = {}, []

    def connect(service, **options):
        assert service == 's3' and options['region_name'] == 'us-west-2'
        clients.append(service)
        return S3()

    monkeypatch.setattr(boto3, 'client', connect)
    config = tmp_path / 'workspaces.yaml'
    config.write_text('root: /mnt/work/source\nrepositories:\n  api: https://example.test/api.git\n')
    args = ['--project-id', 'payments', '--config', str(config), '--region', 'us-west-2',
            '--skill-uri', 's3://workspace-skills/skills/']

    main(args)
    preview = json.loads(capsys.readouterr().out)
    assert not clients
    assert preview['mount_path'] == '/mnt/work'
    assert preview['skill_uri'].startswith('s3://workspace-skills/skills/payments/')
    prefix = preview['skill_uri'].removeprefix('s3://workspace-skills/')

    main([*args, '--apply'])
    assert json.loads(capsys.readouterr().out) == preview
    assert clients == ['s3']
    assert set(objects) == {prefix + name for name in ['SKILL.md', 'scripts/workspace.pyz', 'assets/workspaces.json']}
    assert json.loads(objects[prefix + 'assets/workspaces.json'])['root'] == '/mnt/work/source'

    main([*args, '--apply'])
    assert json.loads(capsys.readouterr().out) == preview

    objects[prefix + 'SKILL.md'] = b'conflicting content'
    with pytest.raises(SystemExit) as error:
        main([*args, '--apply'])
    assert error.value.code == 2
    assert json.loads(capsys.readouterr().err)['error']['code'] == 'STORAGE_CONFLICT'


@pytest.mark.parametrize('project,root,uri', [
    ('payments', '/tmp/work', 's3://bucket/skills/'),
    ('payments', '/mnt', 's3://bucket/skills/'),
    ('payments', '/mnt/../tmp/work', 's3://bucket/skills/'),
    ('payments', '/mnt/with spaces/source', 's3://bucket/skills/'),
    ('payments', '/mnt/' + 'a' * 196 + '/source', 's3://bucket/skills/'),
    ('payments', '/mnt/work', 'https://example.test/skills'),
    ('../payments', '/mnt/work', 's3://bucket/skills/'),
])
def test_workspace_plan_rejects_invalid_mount_or_destination(project, root, uri):
    from daeri.infrastructure.workspace_deployment import workspace_plan
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        workspace_plan(project, {'root': root, 'repositories': {'api': 'https://example.test/api.git'}}, uri)
