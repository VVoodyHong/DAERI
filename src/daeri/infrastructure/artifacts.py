"""Task-scoped S3 artifact discovery and short-lived, version-pinned delivery."""

import base64
from hashlib import sha256
import json
import time
from urllib.parse import quote
from uuid import UUID

from daeri.core.errors import StoreError
from daeri.infrastructure.cloud_s3 import normalize_storage_prefix, validate_storage_bucket


def _encode(value):
    return base64.urlsafe_b64encode(value.encode('utf-8')).decode('ascii').rstrip('=')


def _decode(value):
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise ValueError('Invalid artifact handle')
    return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True).decode('utf-8')


def _relative(value):
    if (not isinstance(value, str) or not value or '\\' in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(part in {'', '.', '..'} for part in value.split('/'))):
        raise ValueError('Invalid artifact path')
    return value


class ArtifactService:
    def __init__(self, client, bucket, prefix='daeri/'):
        self.client = client
        self.bucket, self.prefix = validate_storage_bucket(bucket), normalize_storage_prefix(prefix)

    def location(self, task):
        project, actor = (sha256(value.encode('utf-8')).hexdigest()
                          for value in (task.context.project_id, task.context.actor_id))
        task_id = str(UUID(task.id))
        prefix = f'{self.prefix}projects/{project}/actors/{actor}/tasks/{task_id}/artifacts/'
        return {'bucket': self.bucket, 'prefix': prefix, 'uri': f's3://{self.bucket}/{prefix}'}

    def _authorized(self, user, task):
        if user.context(task.context.project_id) != task.context:
            raise StoreError('ACCESS_DENIED', 'The files belong to another task owner.')
        return self.location(task)

    def list(self, user, task, cursor=None, limit=20):
        location = self._authorized(user, task)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise StoreError('INVALID_INPUT', 'Supply an artifact page size from 1 through 100.')
        request = {'Bucket': self.bucket, 'Prefix': location['prefix'], 'MaxKeys': limit}
        try:
            if cursor is not None:
                value = json.loads(_decode(cursor))
                if (not isinstance(value, dict) or set(value) != {'prefix', 'limit', 'token'}
                        or value['prefix'] != location['prefix'] or type(value['limit']) is not int
                        or value['limit'] != limit or not isinstance(value['token'], str) or not value['token']):
                    raise ValueError('Cursor belongs to another page')
                request['ContinuationToken'] = value['token']
        except (ValueError, TypeError, UnicodeError):
            raise StoreError('INVALID_INPUT', 'The file cursor does not match this task and page.') from None
        try:
            response = self.client.list_objects_v2(**request)
            items = []
            for item in response.get('Contents', []):
                key = item['Key']
                if not isinstance(key, str) or not key.startswith(location['prefix']):
                    raise ValueError('File is outside this task')
                if key.endswith('/'):
                    continue
                relative = _relative(key[len(location['prefix']):])
                if type(item['Size']) is not int or item['Size'] < 0:
                    raise ValueError('Invalid artifact size')
                items.append({'id': _encode(relative), 'name': relative.rsplit('/', 1)[-1], 'size': item['Size']})
            cursor = None
            if response.get('IsTruncated'):
                token = response['NextContinuationToken']
                if not isinstance(token, str) or not token:
                    raise ValueError('Missing next page')
                cursor = _encode(json.dumps({'prefix': location['prefix'], 'limit': limit, 'token': token}))
            return {'items': items, 'next_cursor': cursor}
        except Exception:
            raise StoreError('ARTIFACT_UNAVAILABLE', 'The task files could not be listed.') from None

    def download(self, user, task, artifact_id):
        location = self._authorized(user, task)
        try:
            relative = _relative(_decode(artifact_id))
        except (ValueError, TypeError, UnicodeError):
            raise StoreError('INVALID_INPUT', 'Select a file from the current task.') from None
        name = relative.rsplit('/', 1)[-1]
        try:
            key = location['prefix'] + relative
            head = self.client.head_object(Bucket=self.bucket, Key=key)
            version = head.get('VersionId')
            if not isinstance(version, str) or not version or version == 'null':
                raise ValueError('Artifacts require S3 versioning')
            user.context(task.context.project_id)
            now = int(time.time())
            ttl = min(300, user.expires_at - now)
            if ttl <= 0:
                raise StoreError('AUTHENTICATION_REQUIRED', 'Sign in before downloading the file.')
            url = self.client.generate_presigned_url('get_object', Params={
                'Bucket': self.bucket, 'Key': key, 'VersionId': version,
                'ResponseContentDisposition': "attachment; filename*=UTF-8''" + quote(name, safe=''),
            }, ExpiresIn=ttl)
            return {'name': name, 'url': url, 'expires_at': now + ttl, 'size': head['ContentLength']}
        except StoreError:
            raise
        except Exception:
            raise StoreError('ARTIFACT_UNAVAILABLE', 'The selected file version could not be prepared.') from None
