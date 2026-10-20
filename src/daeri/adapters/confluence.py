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
    target_template = {"server": "https://SET-ME.invalid", "space": "SET-ME"}
    environment_prefixes = ("CONFLUENCE_", "OAUTH_")
    control_fields = frozenset({
        "READ_OPERATIONS_ONLY", "MCP_READ_ONLY", "READ_ONLY_MODE", "ENABLED_TOOLS",
        "EXPERIMENTAL_AGENT_SCRIPTS", "ENDPOINT_SUGGEST_AWS_COMMANDS", "CUSTOM_SCRIPTS_DIR",
    })
    authentication_fields = frozenset({"CONFLUENCE_API_TOKEN", "CONFLUENCE_PERSONAL_TOKEN"})
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
