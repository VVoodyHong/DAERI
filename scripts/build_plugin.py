"""Build an operator-configured plugin marketplace archive without installing it."""

import argparse
import json
from pathlib import Path
import tomllib
from daeri.core.endpoints import mcp_endpoint
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]


def build_plugin(endpoint: str, client_id: str, callback_port: int, output: Path) -> None:
    mcp_endpoint(endpoint)
    if (not client_id or any(character.isspace() or ord(character) < 32 for character in client_id)
            or type(callback_port) is not int or not 1 <= callback_port <= 65535):
        raise ValueError('Supply a public OAuth client ID and a callback port from 1 to 65535.')
    release = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    source = ROOT / 'plugins/daeri'
    files = {
        path.relative_to(ROOT).as_posix(): path.read_bytes()
        for path in (source / name for name in (
            '.claude-plugin/plugin.json', '.codex-plugin/plugin.json',
            '.mcp.json', 'skills/daeri/SKILL.md',
        ))
    }
    connection = json.loads(files['plugins/daeri/.mcp.json'])
    runtime = connection['mcpServers']['runtime']
    runtime['url'] = endpoint
    runtime['oauth'].update(clientId=client_id, callbackPort=callback_port)
    files['plugins/daeri/.mcp.json'] = _json(connection)
    for host in ('.claude-plugin', '.codex-plugin'):
        path = 'plugins/daeri/' + host + '/plugin.json'
        manifest = json.loads(files[path])
        manifest['version'] = release
        files[path] = _json(manifest)
    files['.claude-plugin/marketplace.json'] = _json({
        'name': 'daeri', 'owner': {'name': 'DAERI'},
        'description': '프로젝트 업무와 누적 경험을 함께 사용하는 DAERI 플러그인',
        'plugins': [{'name': 'daeri', 'source': './plugins/daeri'}],
    })
    files['.agents/plugins/marketplace.json'] = _json({
        'name': 'daeri', 'interface': {'displayName': 'DAERI'},
        'plugins': [{
            'name': 'daeri', 'source': {'source': 'local', 'path': './plugins/daeri'},
            'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_INSTALL'},
            'category': 'Productivity',
        }],
    })
    output = Path(output)
    destination = output.open('xb')
    try:
        with destination:
            with ZipFile(destination, 'w', compression=ZIP_DEFLATED) as archive:
                for name, body in sorted(files.items()):
                    archive.writestr(name, body)
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def _json(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--client-id', required=True)
    parser.add_argument('--callback-port', required=True, type=int)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        build_plugin(args.endpoint, args.client_id, args.callback_port, args.output)
    except (ValueError, OSError):
        parser.exit(2, 'Build failed: check the HTTPS URL, public client ID, callback port, and new output path.\n')


if __name__ == '__main__':
    main()
