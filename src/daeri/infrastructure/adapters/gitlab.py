"""GitLab token connection and explicit repository/ref/path reads."""

from daeri.infrastructure.adapters.base import ReadAdapter, clean_path, denied
from daeri.core.errors import StoreError


class GitlabReadAdapter(ReadAdapter):
    adapter_id = "gitlab_read"
    aliases = ("source_read",)
    resource_type = "Source"
    discover_annotations = True
    tool_prefix = 'gitlab_'
    read_only_environment = 'MCP_READ_ONLY'
    read_tools = frozenset({
        'get_commit',
        'get_file_content',
        'get_file_contents',
        'get_merge_request',
        'get_merge_request_diffs',
        'get_project',
        'get_repository_file',
        'gitlab_get_commit',
        'gitlab_get_file',
        'gitlab_get_mr',
        'gitlab_get_project',
        'gitlab_list_branches',
        'gitlab_list_mrs',
        'gitlab_list_tree',
        'gitlab_mr_changes',
        'list_branches',
        'list_merge_requests',
        'list_repository_tree',
    })
    target_template = {"server": "https://SET-ME.invalid"}
    setup_fields = {
        "server": {"schema": {"title": "Source 서버 주소", "description": "인증 정보와 쿼리를 포함하지 않은 HTTPS 서버 주소입니다.",
                              "type": "string", "minLength": 1, "maxLength": 2048,
                              "pattern": r"^https://[^/?#@\s\\\x00-\x1f\x7f]+(?:/[^?#\s\\\x00-\x1f\x7f]*)?$", "examples": ["https://git.example.test"]},
                   "required": True, "paths": [["target", "server"], ["connection", "env", "GITLAB_URL"]]},
    }
    environment_prefixes = ("GITLAB_",)
    control_fields = frozenset({
        "READ_OPERATIONS_ONLY", "MCP_READ_ONLY", "READ_ONLY_MODE", "ENABLED_TOOLS",
        "EXPERIMENTAL_AGENT_SCRIPTS", "ENDPOINT_SUGGEST_AWS_COMMANDS", "CUSTOM_SCRIPTS_DIR",
    })
    authentication_fields = frozenset({"GITLAB_TOKEN", "GITLAB_PAT", "GITLAB_PERSONAL_ACCESS_TOKEN", "GITLAB_API_TOKEN"})
    setup_credentials = {name: {'field': 'token', 'description': '소스 코드 서비스 접속에 사용할 토큰입니다.'}
                         for name in authentication_fields}
    executable = "mcp-gitlab"
    explicit_credentials = True
    decode_result_strings = True

    def validate_connection(self, config):
        super().validate_connection(config)
        connection = config["connection"]
        if connection["transport"] != "stdio":
            return
        env = connection.get("env", {})
        if env.get("GITLAB_AUTH", "token") != "token" or any(key.startswith("GITLAB_OAUTH_") for key in env):
            raise StoreError("UNSUPPORTED_CONSTRAINT", "The local Source adapter uses an explicit token reference, not an alternate OAuth server.")

    def binding_files(self, config, credentials):
        connection, target = config["connection"], config["target"]
        if connection["transport"] == "stdio":
            if not isinstance(target.get("server"), str) or not target["server"] or connection.get("env", {}).get("GITLAB_URL", "").rstrip("/") != target["server"].rstrip("/"):
                raise StoreError("CONTEXT_CHANGED", "The resource server differs from its configured target.")
        return {}

    def normalize(self, parameters, constraints, scope):
        result = super().normalize(parameters, constraints, scope)
        for field in ("project_id", "id"):
            if field in parameters and isinstance(parameters[field], str):
                clean_path(parameters[field])
        for field in ("file_path", "path"):
            if parameters.get(field) not in (None, ''):
                clean_path(parameters[field])
        if "url" in parameters:
            denied("Use explicit repository/ref/path arguments instead of an indirect URL.")
        return result
