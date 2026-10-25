"""Validate the scoped Claude plugin worker entry point without launching a host."""
import json
from pathlib import Path
import subprocess

import yaml

from daeri.contracts import INPUT_MODELS


ROOT = Path(__file__).resolve().parents[1]


def test_claude_worker_has_a_resolvable_single_gateway_allowlist(plugin_environment):
    manifest = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
    assert "agents" not in manifest  # Claude discovers the standard root directory.
    entry = ROOT / "agents/worker.md"
    assert entry.is_file(), "The plugin must package its native worker entry point."
    _, frontmatter, body = entry.read_text().split("---", 2)
    agent = yaml.safe_load(frontmatter)
    assert set(agent) <= {"name", "description", "tools"}
    assert agent["name"] == "worker" and agent["description"]
    subprocess.run([
        str(plugin_environment / '.venv/bin/python.exe'), '-I',
        str(plugin_environment / 'scripts/register_mcp.py'),
    ], check=True)
    servers = json.loads((plugin_environment / ".mcp.json").read_text())["mcpServers"]
    assert "operations" in servers
    gateway = f"mcp__plugin_{manifest['name']}_operations__daeri_worker"
    assert set(agent["tools"]) == {"Read", "ToolSearch", gateway}
    assert "daeri_worker" in INPUT_MODELS
    reference = "${CLAUDE_PLUGIN_ROOT}/skills/work/references/worker.md"
    assert reference in body
    assert Path(reference.replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT))).is_file()
