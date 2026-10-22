"""Confluence token connection and bounded page-identifier reads."""

from .base import ReadAdapter, denied
from ..storage import StoreError


class ConfluenceReadAdapter(ReadAdapter):
    adapter_id = "confluence_read"
    aliases = ("document_read",)
    resource_type = "Document"
    read_tools = frozenset({
        'confluence_get_page',
        'confluence_get_page_children',
        'confluence_get_page_history',
        'confluence_search',
        'getConfluencePage',
        'getConfluencePageDescendants',
        'getConfluenceSpaces',
        'getPagesInConfluenceSpace',
    })
    target_template = {"server": "https://SET-ME.invalid"}
    setup_fields = {
        "server": {"schema": {"title": "Document 서버 주소", "description": "인증 정보와 쿼리를 포함하지 않은 HTTPS 서버 주소입니다.",
                              "type": "string", "minLength": 1, "maxLength": 2048,
                              "pattern": r"^https://[^/?#@\s\\\x00-\x1f\x7f]+(?:/[^?#\s\\\x00-\x1f\x7f]*)?$", "examples": ["https://docs.example.test/wiki"]},
                   "required": True, "paths": [["target", "server"], ["connection", "env", "CONFLUENCE_URL"]]},
        "username": {"schema": {"title": "Document 사용자 이름", "description": "API 토큰과 함께 인증할 계정 이름입니다.",
                                "type": "string", "minLength": 1, "maxLength": 320,
                                "pattern": r"^[^\x00-\x1f\x7f]+$", "examples": ["reader@example.test"]},
                     "required": True, "paths": [["connection", "env", "CONFLUENCE_USERNAME"]]},
    }
    environment_prefixes = ("CONFLUENCE_", "OAUTH_")
    control_fields = frozenset({
        "READ_OPERATIONS_ONLY", "MCP_READ_ONLY", "READ_ONLY_MODE", "ENABLED_TOOLS",
        "EXPERIMENTAL_AGENT_SCRIPTS", "ENDPOINT_SUGGEST_AWS_COMMANDS", "CUSTOM_SCRIPTS_DIR",
    })
    authentication_fields = frozenset({"CONFLUENCE_API_TOKEN", "CONFLUENCE_PERSONAL_TOKEN"})
    setup_credentials = {name: {'field': 'token', 'description': '문서 서비스 접속에 사용할 토큰입니다.'}
                         for name in authentication_fields}
    executable = "mcp-atlassian"
    explicit_credentials = True
    decode_result_strings = True
    preserve_inner_schema = False

    def validate_connection(self, config):
        super().validate_connection(config)
        connection = config["connection"]
        if connection["transport"] == "stdio" and any("OAUTH" in key for key in connection.get("env", {})):
            raise StoreError("UNSUPPORTED_CONSTRAINT", "The local Document adapter uses an explicit token reference, not an alternate OAuth tenant.")

    def binding_files(self, config, credentials):
        connection, target = config["connection"], config["target"]
        if connection["transport"] == "stdio":
            if not isinstance(target.get("server"), str) or not target["server"] or connection.get("env", {}).get("CONFLUENCE_URL", "").rstrip("/") != target["server"].rstrip("/"):
                raise StoreError("CONTEXT_CHANGED", "The resource server differs from its configured target.")
        return {}

    def normalize(self, parameters, constraints, scope):
        result = super().normalize(parameters, constraints, scope)
        if any(field in parameters for field in ("query", "cql", "url")):
            raise StoreError("UNSUPPORTED_CONSTRAINT", "Unparsed Document query languages and indirect URLs are unsupported; use page/space IDs.")
        if "page_id" in parameters and (not isinstance(parameters["page_id"], str) or not parameters["page_id"].isdigit()):
            denied("Use an explicit numeric page ID instead of an indirect URL or tiny link.")
        return result
