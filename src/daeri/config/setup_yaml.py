"""Bounded setup YAML with JSON-compatible values and safe error messages."""

import math
from pathlib import Path
import re

import yaml

from daeri.core.errors import StoreError


MAX_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 32


def _invalid(message='올바른 UTF-8 YAML 설정 파일이 필요합니다.', mark=None):
    if mark is not None:
        message += f' ({mark.line + 1}행 {mark.column + 1}열)'
    raise StoreError('INVALID_INPUT', message)


class _Loader(yaml.SafeLoader):
    # Avoid YAML 1.1 conversions of ordinary words, dates and base-60 numbers.
    yaml_implicit_resolvers = {}

    def __init__(self, stream):
        super().__init__(stream)
        self._setup_depth = -1

    def compose_node(self, parent, index):
        self._setup_depth += 1
        try:
            if self._setup_depth > MAX_DEPTH:
                _invalid('설정의 중첩은 32단계 이하여야 합니다.')
            return super().compose_node(parent, index)
        finally:
            self._setup_depth -= 1

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if type(key) is not str:
                _invalid('설정의 키는 문자열이어야 합니다.', key_node.start_mark)
            if key == '<<':
                _invalid('YAML 병합은 사용할 수 없습니다.', key_node.start_mark)
            if key in result:
                _invalid('중복된 설정 키를 제거해 주세요.', key_node.start_mark)
            result[key] = self.construct_object(value_node, deep=deep)
        return result


_Loader.add_implicit_resolver('tag:yaml.org,2002:bool',
                              re.compile(r'^(?:true|True|TRUE|false|False|FALSE)$'),
                              list('tTfF'))
_Loader.add_implicit_resolver('tag:yaml.org,2002:null',
                              re.compile(r'^(?:~|null|Null|NULL|)$'),
                              ['~', 'n', 'N', ''])
_Loader.add_implicit_resolver('tag:yaml.org,2002:int',
                              re.compile(r'^[-+]?(?:0|[1-9][0-9]*)$'),
                              list('-+0123456789'))
_Loader.add_implicit_resolver('tag:yaml.org,2002:float', re.compile(
    r'^(?:[-+]?(?:(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?'
    r'|[0-9]+[eE][-+]?[0-9]+|\.(?:inf|Inf|INF))|\.(?:nan|NaN|NAN))$'),
    list('-+0123456789.'))


class _Dumper(yaml.SafeDumper):
    yaml_implicit_resolvers = _Loader.yaml_implicit_resolvers

    def ignore_aliases(self, data):
        return True


def _json_value(value, depth=0):
    if depth > MAX_DEPTH:
        _invalid('설정의 중첩은 32단계 이하여야 합니다.')
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or key == '<<':
                _invalid('설정의 키는 병합 키가 아닌 문자열이어야 합니다.')
            _json_value(item, depth + 1)
    elif type(value) is list:
        for item in value:
            _json_value(item, depth + 1)
    elif type(value) is float:
        if not math.isfinite(value):
            _invalid('숫자는 유한한 값이어야 합니다.')
    elif value is not None and type(value) not in (str, int, bool):
        _invalid('설정은 JSON으로 표현 가능한 값만 사용할 수 있습니다.')


def read_yaml(path):
    """Read one mapping; schema and secret validation belong to the caller."""
    try:
        with Path(path).open('rb') as stream:
            content = stream.read(MAX_BYTES + 1)
    except OSError:
        _invalid()
    return parse_yaml(content)


def parse_yaml(content):
    """Parse the same immutable bytes that a caller fingerprints for approval."""
    if type(content) is not bytes:
        _invalid()
    try:
        if len(content) > MAX_BYTES:
            _invalid('설정 파일은 2 MiB 이하여야 합니다.')
        text = content.decode('utf-8')
        forbidden = (yaml.tokens.AnchorToken, yaml.tokens.AliasToken,
                     yaml.tokens.TagToken, yaml.tokens.DirectiveToken)
        for token in yaml.scan(text):
            if isinstance(token, forbidden):
                _invalid('태그·앵커·별칭·지시문은 사용할 수 없습니다.', token.start_mark)
        value = yaml.load(text, Loader=_Loader)
        if type(value) is not dict:
            _invalid('설정의 최상위 값은 객체여야 합니다.')
        _json_value(value)
        return value
    except yaml.YAMLError as error:
        _invalid(mark=getattr(error, 'problem_mark', None))
    except (ValueError, UnicodeError, RecursionError):
        _invalid()


def write_yaml(value):
    """Render editable YAML without aliases or implementation-specific tags."""
    if type(value) is not dict:
        _invalid('설정의 최상위 값은 객체여야 합니다.')
    _json_value(value)
    try:
        encoded = yaml.dump(value, Dumper=_Dumper, sort_keys=False,
                            allow_unicode=True, default_flow_style=False)
        if len(encoded.encode('utf-8')) > MAX_BYTES:
            _invalid('설정 파일은 2 MiB 이하여야 합니다.')
        return encoded
    except (yaml.YAMLError, ValueError, UnicodeError, RecursionError):
        _invalid()
