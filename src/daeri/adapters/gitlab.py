"""GitLab token connection and explicit repository/ref/path reads."""

from .base import ReadAdapter, clean_path, denied
from ..storage import StoreError


class GitlabReadAdapter(ReadAdapter):
    adapter_id = "gitlab_read"
    aliases = ("source_read",)
    resource_type = "Source"
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
    target_template = {"server": "https://SET-ME.invalid", "project": "SET-ME"}
    environment_prefixes = ("GITLAB_",)
    control_fields = frozenset({
        "READ_OPERATIONS_ONLY", "MCP_READ_ONLY", "READ_ONLY_MODE", "ENABLED_TOOLS",
        "EXPERIMENTAL_AGENT_SCRIPTS", "ENDPOINT_SUGGEST_AWS_COMMANDS", "CUSTOM_SCRIPTS_DIR",
    })
    authentication_fields = frozenset({"GITLAB_TOKEN", "GITLAB_PAT", "GITLAB_PERSONAL_ACCESS_TOKEN", "GITLAB_API_TOKEN"})
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

    def prepare_parameters(self, parameters, input_schema):
        for field in ("ref", "path"):
            if field in input_schema.get("properties", {}) and (field not in parameters or parameters[field] is None):
                raise StoreError("UNSUPPORTED_CONSTRAINT", "Source revision and path must be explicit; provider defaults cannot choose the approved target.")

    def normalize(self, parameters, constraints, scope):
        result = super().normalize(parameters, constraints, scope)
        for field in ("project_id", "id"):
            if field in parameters and isinstance(parameters[field], str):
                clean_path(parameters[field])
        for field in ("file_path", "path"):
            if field in parameters:
                clean_path(parameters[field])
        if "url" in parameters:
            denied("Use explicit repository/ref/path arguments instead of an indirect URL.")
        return result

    def source_complete(self, value, invocation):
        if super().source_complete(value, invocation) is False:
            return False
        data = value.get("data", value)
        if isinstance(data, dict) and invocation.operation in ("gitlab_get_file", "get_repository_file"):
            metadata = data.get("metadata", data)
            arguments = invocation.prepared_request["arguments"]
            if any(arguments.get(field) is not None for field in ("start_line", "end_line", "limit")) or arguments.get("offset", 0) != 0:
                return False
            if isinstance(metadata, dict) and metadata.get("truncated") is False:
                return True
        return None
