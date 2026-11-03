"""Authenticated artifact listing and version-pinned downloads without AWS."""

from dataclasses import replace
import time
from urllib.parse import unquote
from uuid import uuid4

import pytest

from daeri.core.access import AuthenticatedUser
from daeri.core.cloud import AccessContext, CloudTask
from daeri.core.errors import StoreError


class S3:
    def __init__(self):
        self.calls = []
        self.contents = []
        self.pages = {}

    def list_objects_v2(self, **request):
        self.calls.append(('list', request))
        return self.pages.get(request.get('ContinuationToken'), {'Contents': self.contents})

    def head_object(self, **request):
        self.calls.append(('head', request))
        return {'VersionId': 'version-one', 'ContentLength': 12, 'ContentType': 'text/csv'}

    def generate_presigned_url(self, operation, **request):
        self.calls.append(('sign', operation, request))
        return 'https://example.test/download?signature=example'


def setup():
    from daeri.infrastructure.artifacts import ArtifactService
    s3 = S3()
    service = ArtifactService(s3, 'company-daeri', 'daeri/')
    task = CloudTask(str(uuid4()), str(uuid4()), AccessContext('alice', 'project'))
    user = AuthenticatedUser('alice', frozenset({'project'}), int(time.time()) + 120)
    return service, s3, user, task


def test_listing_and_download_are_bound_to_task_and_current_login():
    service, s3, user, task = setup()
    location = service.location(task)
    s3.contents = [{'Key': location['prefix'] + '국가별 충전소.csv', 'Size': 12}]
    page = service.list(user, task)
    item = page['items'][0]
    assert item['name'] == '국가별 충전소.csv'
    download = service.download(user, task, item['id'])
    assert download['name'] == item['name'] and download['expires_at'] <= user.expires_at
    signed = next(call[2] for call in s3.calls if call[0] == 'sign')
    assert signed['ExpiresIn'] <= 120 and signed['Params']['VersionId'] == 'version-one'
    assert signed['Params']['Bucket'] == 'company-daeri'
    assert signed['Params']['Key'] == location['prefix'] + item['name']
    assert unquote(signed['Params']['ResponseContentDisposition']).endswith(item['name'])


def test_other_users_and_projects_cannot_read_or_sign_task_files():
    service, s3, user, task = setup()
    bob = replace(user, actor_id='bob')
    for caller, selected in ((bob, task), (replace(user, project_ids=frozenset()), task),
                             (user, replace(task, context=AccessContext('alice', 'other')))):
        with pytest.raises(StoreError, match='ACCESS_DENIED'):
            service.list(caller, selected)
    assert not s3.calls


@pytest.mark.parametrize('relative', ['../secret.csv', '/absolute', 'file\r\nInjected: yes', 'folder\\secret'])
def test_crafted_ids_cannot_escape_the_task_prefix(relative):
    import base64
    service, s3, user, task = setup()
    crafted = base64.urlsafe_b64encode(relative.encode()).decode().rstrip('=')
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        service.download(user, task, crafted)
    assert not s3.calls


def test_pagination_cursor_is_tied_to_the_selected_task_and_page_size():
    service, s3, user, task = setup()
    location = service.location(task)
    s3.pages[None] = {'Contents': [{'Key': location['prefix'] + 'first.csv', 'Size': 12}],
                      'IsTruncated': True, 'NextContinuationToken': 'native-next'}
    cursor = service.list(user, task, limit=1)['next_cursor']
    service.list(user, task, cursor=cursor, limit=1)
    assert s3.calls[-1][1]['ContinuationToken'] == 'native-next'
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        service.list(user, replace(task, id=str(uuid4())), cursor=cursor, limit=1)
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        service.list(user, task, cursor=cursor, limit=20)


def test_unversioned_files_and_storage_failures_do_not_issue_download_links():
    service, s3, user, task = setup()
    location = service.location(task)
    s3.contents = [{'Key': location['prefix'] + 'result.csv', 'Size': 12}]
    artifact_id = service.list(user, task)['items'][0]['id']
    s3.head_object = lambda **request: {'ContentLength': 12}
    with pytest.raises(StoreError, match='ARTIFACT_UNAVAILABLE'):
        service.download(user, task, artifact_id)
    assert not any(call[0] == 'sign' for call in s3.calls)
