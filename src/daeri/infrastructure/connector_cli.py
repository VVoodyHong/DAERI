"""Start an operator-packaged HTTP MCP connector with resolved secret references."""

import json
import os
import re
import sys

from daeri.core.errors import StoreError


_CONFIG = 'DAERI_CONNECTOR_CONFIG'
_ENVIRONMENT_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
_SECRET_ARN = re.compile(
    r'arn:(?:aws|aws-cn|aws-us-gov):secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+\Z')


def _invalid():
    raise StoreError('INVALID_INPUT', 'Check the operator connector launch configuration.')


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _text(value):
    if not isinstance(value, str) or '\x00' in value:
        _invalid()
    value.encode('utf-8')
    return value


def _configuration(value):
    try:
        config = json.loads(value, object_pairs_hook=_object)
        if not isinstance(config, dict) or not {'command'} <= set(config) <= {'command', 'environment', 'secrets'}:
            _invalid()
        command = config['command']
        if not isinstance(command, list) or not command or any(not _text(argument) for argument in command):
            _invalid()
        environment, secrets = config.get('environment', {}), config.get('secrets', {})
        for mapping in (environment, secrets):
            if not isinstance(mapping, dict):
                _invalid()
            for name, setting in mapping.items():
                if not _ENVIRONMENT_NAME.fullmatch(name) or name == _CONFIG:
                    _invalid()
                _text(setting)
        if environment.keys() & secrets.keys() or any(not _SECRET_ARN.fullmatch(arn) for arn in secrets.values()):
            _invalid()
        return command, environment, secrets
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _invalid()


def main(argv=None):
    """Accept launch settings only from the trusted runtime environment."""
    try:
        arguments = sys.argv[1:] if argv is None else argv
        if arguments:
            _invalid()
        command, settings, references = _configuration(os.environ.get(_CONFIG))
        environment = {key: value for key, value in os.environ.items() if key != _CONFIG}
        environment.update(settings)
        try:
            if references:
                import boto3
                from botocore.config import Config

                with boto3.client('secretsmanager', config=Config(
                        retries={'total_max_attempts': 1}, connect_timeout=10, read_timeout=30)) as secrets:
                    for name, arn in references.items():
                        value = secrets.get_secret_value(SecretId=arn).get('SecretString')
                        environment[name] = _text(value)
            os.execvpe(command[0], command, environment)
        except Exception:
            raise StoreError('CONNECTION_NOT_READY', 'Connector startup failed; check its package and credential references.') from None
        return 0
    except StoreError as error:
        print(json.dumps({'error': {'code': error.code, 'message': error.message}}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
