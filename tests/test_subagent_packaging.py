"""Validate the scoped Claude plugin subagent entry point without launching a host."""
import json
from pathlib import Path
import subprocess

import yaml

from daeri.core.contracts import INPUT_MODELS


ROOT = Path(__file__).resolve().parents[1]


def test_claude_subagent_has_a_resolvable_single_gateway_allowlist(plugin_environment):
    manifest = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
    assert "agents" not in manifest  # Claude discovers the standard root directory.
    entry = ROOT / "agents/subagent.md"
    assert entry.is_file(), "The plugin must package its native subagent entry point."
    _, frontmatter, body = entry.read_text().split("---", 2)
    agent = yaml.safe_load(frontmatter)
    assert set(agent) <= {"name", "description", "tools"}
    assert agent["name"] == "subagent" and agent["description"]
    subprocess.run([
        str(plugin_environment / '.venv/bin/python.exe'), '-I',
        str(plugin_environment / 'scripts/register_mcp.py'),
    ], check=True)
    servers = json.loads((plugin_environment / ".mcp.json").read_text())["mcpServers"]
    assert "runtime" in servers
    gateway = f"mcp__plugin_{manifest['name']}_runtime__daeri_subagent"
    assert set(agent["tools"]) == {"Read", "Grep", "Glob", "Bash", "ToolSearch", gateway}
    assert "daeri_subagent" in INPUT_MODELS
    reference = "${CLAUDE_PLUGIN_ROOT}/skills/work/references/subagent.md"
    assert reference in body
    assert Path(reference.replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT))).is_file()
