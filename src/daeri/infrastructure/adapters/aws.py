"""AWS read command, provider-file binding, and explicit login interpretation."""

import configparser
from pathlib import Path
import re
import shlex

from daeri.infrastructure.adapters.base import ReadAdapter, _guard, denied
from daeri.infrastructure.provider_login import PROFILE_NAME_PATTERN, provider_section
from daeri.core.errors import StoreError


def _aws(parameters, constraints, scope):
    field = constraints.get("command_field", "cli_command")
    command = parameters.get(field)
    _guard(parameters, {key: value for key, value in constraints.items() if key not in {'region', 'command_field'}})
    _guard(parameters, scope)
    if isinstance(command, list) and command:
        return {'commands': [_aws_command(value, constraints.get('region')) for value in command]}
    return _aws_command(command, constraints.get('region'))


def _aws_command(command, region):
    if not isinstance(command, str) or any(char in command for char in "\n\r;|&`$<>\\"):
        denied("Only a parsed, approved read operation is supported.")
    try:
        tokens = shlex.split(command)
    except ValueError:
        denied()
    if len(tokens) < 3 or tokens[0] != "aws" or any(token.startswith("-") for token in tokens[1:3]):
        denied()
    if any(any(scheme in value.lower() for scheme in ("file:", "http:", "https:")) for value in tokens):
        denied()
    forbidden = {"--endpoint-url", "--profile", "--debug", "--no-verify-ssl", "--ca-bundle"}
    flags = [token.split("=", 1)[0] for token in tokens[3:] if token.startswith("--")]
    if any(any(option.startswith(flag) for option in forbidden) for flag in flags):
        denied()
    if any("--region".startswith(flag) and flag != "--region" for flag in flags) or flags.count("--region") > 1:
        denied()
    if not isinstance(region, str) or not region:
        denied("A configured Infra region is required.")
    for index, token in enumerate(tokens):
        if token == '--region':
            if index + 1 >= len(tokens) or tokens[index + 1] != region:
                denied()
        elif token.startswith('--region=') and token.split('=', 1)[1] != region:
            denied()
    return {"service": tokens[1], "operation": tokens[2], "region": region}


class AwsReadAdapter(ReadAdapter):
    adapter_id = "aws_read"
    resource_type = "Infra"
    read_tools = frozenset({
        'aws_api_call',
        'call_aws',
        'call_aws_api',
    })
    target_template = {"profile": "SET-ME", "region": "SET-ME", "account": "SET-ME"}
    setup_fields = {
        "profile": {"schema": {"title": "Infra 제공자 profile", "description": "DAERI profile과 구분되는 제공자 인증 설정의 profile입니다.",
                               "type": "string", "minLength": 1, "maxLength": 128,
                               "pattern": PROFILE_NAME_PATTERN, "examples": ["operations-reader"]},
                    "required": True, "paths": [["target", "profile"], ["connection", "env", "AWS_PROFILE"]]},
        "region": {"schema": {"title": "Infra 리전", "description": "연결 대상과 조회 명령에 동일하게 적용할 제공자 리전입니다.",
                              "type": "string", "minLength": 1, "maxLength": 64,
                              "pattern": r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)+$", "examples": ["ap-northeast-2"]},
                   "required": True, "paths": [["target", "region"], ["connection", "env", "AWS_REGION"],
                                               ["capabilities", 0, "constraints", "region"]]},
        "account": {"schema": {"title": "Infra 계정 식별자", "description": "제공자 인증 결과와 대조할 12자리 계정 식별자입니다. 숫자 또는 문자열을 입력하세요.",
                               "type": ["string", "integer"], "pattern": r"^[0-9]{12}$",
                               "minLength": 12, "maxLength": 12,
                               "minimum": 100000000000, "maximum": 999999999999,
                               "examples": [111111111111, "111111111111"]},
                    "required": True, "paths": [["target", "account"]]},
    }
    environment_prefixes = ("AWS_",)
    control_fields = frozenset({
        "READ_OPERATIONS_ONLY", "MCP_READ_ONLY", "READ_ONLY_MODE", "ENABLED_TOOLS",
        "EXPERIMENTAL_AGENT_SCRIPTS", "ENDPOINT_SUGGEST_AWS_COMMANDS", "CUSTOM_SCRIPTS_DIR",
    })
    executable = "awslabs.aws-api-mcp-server"
    normalize_results = True
    login_recipe_id = "infra-aws"
    login_helper = "provider_login.py"

    def normalize_setup_values(self, values):
        values = super().normalize_setup_values(values)
        if type(values.get("account")) is int:
            values["account"] = str(values["account"])
        return values

    def validate_connection(self, config):
        super().validate_connection(config)
        connection = config["connection"]
        if connection["transport"] != "stdio":
            return
        env = connection.get("env", {})
        if any(item['constraints'].get('region', config['target'].get('region')) != config['target'].get('region')
               for item in config.get('capabilities', [])):
            raise StoreError('CONTEXT_CHANGED', 'The Infra command region differs from its registered target.')
        allowed = {"AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_API_MCP_PROFILE_NAME", "AWS_API_MCP_TELEMETRY"}
        if (any(key.startswith("AWS_") and key not in allowed for key in env)
                or any(key in env for key in ("EXPERIMENTAL_AGENT_SCRIPTS", "ENDPOINT_SUGGEST_AWS_COMMANDS", "CUSTOM_SCRIPTS_DIR"))
                or connection.get("auth_env")):
            raise StoreError("UNSUPPORTED_CONSTRAINT", "Infra endpoint, executable, or credential-provider overrides are unsupported.")
        for key, bound in (("AWS_API_MCP_PROFILE_NAME", env.get("AWS_PROFILE")), ("AWS_DEFAULT_REGION", env.get("AWS_REGION"))):
            if key in env and env[key] != bound:
                raise StoreError("UNSUPPORTED_CONSTRAINT", "The Infra provider profile and location must remain bound to the target.")
        if "READ_OPERATIONS_ONLY" in env and env["READ_OPERATIONS_ONLY"].lower() != "true":
            raise StoreError("UNSUPPORTED_CONSTRAINT", "Infra read-only mode cannot be disabled.")

    def binding_files(self, config, credentials):
        connection, target = config["connection"], config["target"]
        if connection["transport"] != "stdio":
            return {}
        env = connection.get("env", {})
        profile_name = env.get("AWS_PROFILE")
        if not profile_name or target.get("profile") != profile_name or target.get("region") != env.get("AWS_REGION"):
            raise StoreError("CONTEXT_CHANGED", "The Infra provider profile or region changed.")
        aws = Path.home() / ".aws"
        config_path = aws / "config"
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(config_path)
            credential_settings = configparser.ConfigParser(interpolation=None)
            credential_settings.read(aws / "credentials")
            if parser.has_section("plugins") and list(parser["plugins"].items()):
                raise ValueError
            alias = aws / "cli" / "alias"
            if alias.exists() and (alias.is_symlink() or alias.read_bytes().strip()):
                raise ValueError
            section = provider_section(parser, "profile", profile_name)
            settings = parser[section]
            if settings.get("region", target["region"]) != target["region"]:
                raise ValueError
            visited, current = set(), profile_name
            while current:
                if current in visited or len(visited) >= 16:
                    raise ValueError
                visited.add(current)
                selected = provider_section(parser, "profile", current)
                values = dict(parser[selected]) if parser.has_section(selected) else {}
                if credential_settings.has_section(current):
                    values.update(credential_settings[current])
                if any(key in values for key in ("credential_process", "credential_source", "web_identity_token_file", "login_session", "services")):
                    raise ValueError
                if any("endpoint" in key or "endpoint_url" in value.lower() for key, value in values.items()):
                    raise ValueError
                current = values.get("source_profile")
            account = settings.get("sso_account_id")
            role = settings.get("role_arn", "")
            if account and role:
                raise ValueError
            if role:
                match = re.fullmatch(r"arn:aws(?:-[a-z0-9-]+)?:iam::([0-9]{12}):role/[^\s]+", role)
                if not match:
                    raise ValueError
                account = match[1]
            if not account or not re.fullmatch(r"[0-9]{12}", account) or target.get("account") != account:
                raise ValueError
        except (OSError, ValueError, KeyError, configparser.Error):
            raise StoreError("UNSUPPORTED_CONSTRAINT", "Infra binding requires a configured SSO account or explicit role account matching the target.") from None
        return {}

    def launch_connection(self, config):
        connection = super().launch_connection(config)
        if connection["transport"] == "stdio":
            env = connection.setdefault("env", {})
            env.update({"AWS_API_MCP_PROFILE_NAME": env["AWS_PROFILE"], "AWS_DEFAULT_REGION": env["AWS_REGION"],
                        "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true", "AWS_EC2_METADATA_DISABLED": "true",
                        "READ_OPERATIONS_ONLY": "true", "AWS_API_MCP_TELEMETRY": "false", "EXPERIMENTAL_AGENT_SCRIPTS": "false",
                        "AWS_API_MCP_ALLOW_UNRESTRICTED_LOCAL_FILE_ACCESS": "no-access"})
        return connection

    def normalize(self, parameters, constraints, scope):
        return _aws(parameters, constraints, scope)

    def provider_error(self, value):
        data = value.get("data", value.get("result", value))
        entries = data if isinstance(data, list) else [data]
        if any(isinstance(entry, dict) and entry.get("error") for entry in entries):
            raise StoreError("EXTERNAL_EXECUTION_FAILED", "The lower Infra MCP reported a failed read operation.")

    def login_request(self, resource, *, start_url=None, sso_region=None, role=None, no_browser=False):
        connection, target = resource["connection"], resource["target"]
        if (connection.get("env", {}).get("AWS_PROFILE") != target.get("profile")
                or connection.get("env", {}).get("AWS_REGION") != target.get("region")):
            raise StoreError("INVALID_INPUT", "The selected provider profile differs from the resource target.")
        return {"profile": target.get("profile"), "account": target.get("account"), "region": target.get("region"),
                "start_url": start_url, "sso_region": sso_region, "role": role, "no_browser": no_browser}

    def validate_login_prompt(self, data, resource):
        if (not isinstance(data, dict) or set(data) != {"provider", "verification_uri", "user_code", "provider_profile"}
                or data["provider_profile"] != resource["target"]["profile"]):
            raise StoreError("INVALID_INPUT", "Provider login returned an invalid prompt.")

    def validate_login_result(self, data, resource):
        if (not isinstance(data, dict) or set(data) != {"authenticated", "provider_profile", "account", "role", "expires_at"}
                or data["authenticated"] is not True or data["provider_profile"] != resource["target"]["profile"]
                or data["account"] != resource["target"]["account"]):
            raise StoreError("INVALID_INPUT", "Provider login returned an invalid completion.")
