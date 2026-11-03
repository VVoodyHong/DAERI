"""Blocking harness hook backed by service-owned session and approval records."""

from daeri.core.serialization import validate_storable_json


def handle_approval_hook(event, store):
    """Only trusted Lambda delivery and service-owned S3 writes establish the caller binding."""
    denied = {'decision': 'deny', 'reason': 'A current service-recorded plan approval is required.'}
    try:
        if not isinstance(event, dict) or event.get('event') != 'before_tool_call':
            return denied
        context = event.get('context')
        if (not isinstance(context, dict)
                or not all(isinstance(context.get(key), str) and context[key]
                           for key in ('toolName', 'toolType', 'toolUseId'))):
            return denied
        task = store.hook_get(event.get('harnessArn'), event.get('sessionId'))
        if context['toolName'] == 'confirm_plan':
            store.revoke_approval(task)
        data = context.get('toolInput')
        if context.get('truncated') is True or not isinstance(data, dict):
            return denied
        if data.get('truncated') is True and 'preview' in data:
            return denied
        validate_storable_json(data)
        if context['toolName'] == 'confirm_plan':
            if (context['toolType'] != 'inline_function' or set(data) != {'plan'}
                    or not isinstance(data['plan'], str) or not data['plan'].strip()):
                return denied
            return {'decision': 'allow'}
        if task.approval is not None and task.approval.decision == 'approve':
            return {'decision': 'allow'}
    except Exception:
        return denied
    return denied


def lambda_handler(event, context):
    """AWS entrypoint uses the Lambda service role and explicitly configured S3 scope."""
    import os

    import boto3
    from botocore.config import Config

    from daeri.infrastructure.cloud_s3 import S3CloudStore, normalize_storage_prefix, validate_storage_bucket

    client = None
    try:
        bucket = validate_storage_bucket(os.environ.get('DAERI_STORAGE_BUCKET', ''))
        prefix = normalize_storage_prefix(os.environ.get('DAERI_STORAGE_PREFIX', 'daeri/'))
        client = boto3.client('s3', config=Config(retries={'total_max_attempts': 1}))
        return handle_approval_hook(event, S3CloudStore(client, bucket, prefix))
    except Exception:
        return {'decision': 'deny', 'reason': 'The approval guard configuration or store is unavailable.'}
    finally:
        if client is not None:
            client.close()
