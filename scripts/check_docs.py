"""Check local Markdown links in the repository's documentation, without Git."""

import html
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


_LINK = re.compile(
    r'!?\[(?:\\.|[^\]\\])*\]\(\s*'
    r'(?:<([^>\n]*)>|((?:\\.|[^\\\s()]|\((?:\\.|[^\\()])*\))*))'
    r'''(?:\s+(?:"[^"\n]*"|'[^'\n]*'|\([^\n)]*\)))?\s*\)''')


def _documents(root: Path) -> list[Path]:
    paths = [root / name for name in ('AGENTS.md', 'CLAUDE.md', 'README.md')
             if (root / name).is_file()]
    for name in ('docs', 'skills', 'agents'):
        for directory, folders, files in os.walk(root / name):
            folders[:] = [folder for folder in folders
                          if not folder.startswith('.')
                          and folder not in ('__pycache__', 'node_modules')]
            paths.extend(Path(directory) / file for file in files if file.endswith('.md'))
    return sorted(paths)


def _without_fences(text: str) -> str:
    lines = []
    fence = ''
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) \
                    and not marker[2].strip():
                fence = ''
            lines.append('\n' if line.endswith('\n') else '')
        elif marker:
            fence = marker[1]
            lines.append('\n' if line.endswith('\n') else '')
        else:
            lines.append(line)
    return ''.join(lines)


def _anchors(text: str) -> set[str]:
    anchors = set()
    for match in re.finditer(r'^ {0,3}#{1,6}[ \t]+(.+?)\s*#*\s*$', text, re.MULTILINE):
        title = _LINK.sub(lambda link: link[0][link[0].find('[') + 1:link[0].find(']')], match[1])
        title = html.unescape(re.sub(r'<[^>]*>', '', title))
        slug = re.sub(r'[^\w\- ]', '', title.lower()).replace(' ', '-')
        anchor, number = slug, 0
        while anchor in anchors:
            number += 1
            anchor = f'{slug}-{number}'
        anchors.add(anchor)
    return anchors


def check_docs(root: Path) -> list[str]:
    """Return source locations for broken local inline links, images and anchors."""
    root = root.resolve()
    documents = {path: _without_fences(path.read_text(encoding='utf-8'))
                 for path in _documents(root)}
    anchors = {path: _anchors(text) for path, text in documents.items()}
    issues = []
    for source, text in documents.items():
        # Preserve line numbers while excluding literal Markdown in inline code.
        text = re.sub(r'(`+)(?!`)(.*?)\1(?!`)',
                      lambda match: re.sub(r'[^\n]', ' ', match[0]), text, flags=re.DOTALL)
        for match in _LINK.finditer(text):
            destination = html.unescape(match[1] if match[1] is not None else match[2])
            destination = re.sub(r'\\([\\() ])', r'\1', destination)
            url = urlsplit(destination)
            if url.scheme or url.netloc:
                continue
            path = unquote(url.path).replace('${CLAUDE_PLUGIN_ROOT}', root.as_posix())
            target = (source.parent / path).resolve() if path else source
            error = None
            if not target.is_relative_to(root) or not target.exists():
                error = 'missing local target'
            elif url.fragment and target.suffix.lower() == '.md':
                if target not in anchors:
                    anchors[target] = _anchors(_without_fences(target.read_text(encoding='utf-8')))
                if unquote(url.fragment) not in anchors[target]:
                    error = 'missing anchor'
            if error:
                line = text.count('\n', 0, match.start()) + 1
                issues.append(f'{source.relative_to(root).as_posix()}:{line}: {error}: {destination}')
    return issues
