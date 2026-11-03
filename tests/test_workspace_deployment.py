"""Native S3 Skills provision the actual Git helper into managed harness sessions."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

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


def test_provisioning_preserves_environment_and_skills_and_publishes_after_upload():
    from daeri.infrastructure.workspace_deployment import configure_workspaces
    from daeri.infrastructure.project_deployment import ProjectDeployment
    from test_project_deployment import Control, Ssm, settings, missing

    class HarnessControl(Control):
        def update_harness(self, **request):
            if 'skills' not in request:
                return super().update_harness(**request)
            self.record('UpdateHarness', request)
            self.harness.update({key: value for key, value in request.items() if key != 'harnessId'})
            self.harness['harnessVersion'] = '3'
            return {'harness': deepcopy(self.harness)}
        def get_harness_endpoint(self, **request):
            if self.endpoint is not None and self.endpoint['endpointName'] != request['endpointName']:
                raise missing('GetHarnessEndpoint')
            return super().get_harness_endpoint(**request)

    class S3:
        def __init__(self):
            self.objects = {}
        def put_object(self, **request):
            self.objects[request['Key']] = request['Body']

    control, ssm, s3, project = HarnessControl(), Ssm(), S3(), settings()
    control.harness['skills'] = [{'path': '/opt/company-skills/review'}]
    control.harness['environment'] = {'agentCoreRuntimeEnvironment': {
        'agentRuntimeArn': 'read-only-output-field', 'networkConfiguration': {'networkMode': 'PUBLIC'},
        'filesystemConfigurations': [{'sessionStorage': {'mountPath': '/mnt/work'}}]}}
    ProjectDeployment(control, ssm, lambda project: None).apply(project)
    result = configure_workspaces(control, s3, ssm, project,
        {'root': '/mnt/work/source', 'repositories': {'api': 'https://example.test/company/api.git'}},
        's3://workspace-skills/skills/', lambda route: None)
    assert result['project']['harness_version'] == '3' and len(s3.objects) == 3
    assert control.harness['skills'][0] == {'path': '/opt/company-skills/review'}
    runtime = control.harness['environment']['agentCoreRuntimeEnvironment']
    assert runtime == {'networkConfiguration': {'networkMode': 'PUBLIC'},
        'filesystemConfigurations': [{'sessionStorage': {'mountPath': '/mnt/work'}}]}
    assert control.harness['skills'][-1] == {'s3': {'uri': result['skill_uri']}}
    result = configure_workspaces(control, s3, ssm, project,
        {'root': '/mnt/work/source', 'repositories': {'api': 'https://example.test/company/api.git'}},
        's3://workspace-skills/skills/', lambda route: None)
    assert len(control.harness['skills']) == 2


@pytest.mark.parametrize('root', ['/tmp/disposable', '/mnt', '/mnt/../tmp/work'])
def test_persistent_mount_preflight_happens_before_aws_mutations(root):
    from daeri.infrastructure.workspace_deployment import configure_workspaces
    from test_project_deployment import settings
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        configure_workspaces(None, None, None, settings(), {'root': root,
            'repositories': {'api': 'https://example.test/api.git'}}, 's3://skills/prefix/', None)


@pytest.mark.parametrize('root,uri', [('/tmp/work', 's3://bucket/skills/'), ('/mnt/work', 'https://example.test/skills')])
def test_preview_uses_the_same_workspace_validation(root, uri):
    from daeri.infrastructure.workspace_deployment import workspace_plan
    from test_project_deployment import settings
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        workspace_plan(settings(), {'root': root, 'repositories': {'api': 'https://example.test/api.git'}}, uri)
