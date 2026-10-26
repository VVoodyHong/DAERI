from pathlib import Path

import pytest

from scripts.check_docs import check_docs


def write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def test_resolves_relative_links_images_and_url_encoded_korean_anchors(tmp_path):
    write(tmp_path, 'README.md', '''# 안내
[계약](docs/사용%20안내.md#%ED%95%9C%EA%B5%AD%EC%96%B4-api-%EA%B3%84%EC%95%BD)
[코드](src/example.py)
![동작](docs/assets/flow.svg "동작 그림")
''')
    write(tmp_path, 'docs/사용 안내.md', '''# 사용 안내
## 한국어 `API` 계약!
[현재 절](#한국어-api-계약)
[처음](../README.md#안내)
[공백 경로](<사용 안내.md#한국어-api-계약>)
''')
    write(tmp_path, 'docs/assets/flow.svg', '<svg/>')
    write(tmp_path, 'src/example.py', 'pass')

    assert check_docs(tmp_path) == []


@pytest.mark.parametrize('link', ['[문서](missing.md)', '![그림](missing.svg)'])
def test_reports_missing_document_and_image_with_source_location(tmp_path, link):
    write(tmp_path, 'README.md', '# 안내\n' + link)

    issues = check_docs(tmp_path)

    assert len(issues) == 1
    assert 'README.md:2:' in issues[0]
    assert 'missing.' in issues[0]


@pytest.mark.parametrize('destination', ['#없는-절', 'docs/guide.md#없는-절'])
def test_reports_broken_self_and_cross_document_anchors(tmp_path, destination):
    write(tmp_path, 'README.md', '# 안내\n[링크](' + destination + ')')
    write(tmp_path, 'docs/guide.md', '# 안내\n## 있는 절')

    issues = check_docs(tmp_path)

    assert len(issues) == 1
    assert destination in issues[0]


def test_duplicate_headings_get_distinct_github_anchors(tmp_path):
    write(tmp_path, 'README.md', '''# 안내
## 반복
## 반복
## 반복-1
## 반복
[둘째](#반복-1) [충돌 회피](#반복-1-1) [셋째](#반복-2)
[없는 넷째](#반복-3)
''')

    issues = check_docs(tmp_path)

    assert len(issues) == 1
    assert '#반복-3' in issues[0]


def test_ignores_external_links_and_code_examples(tmp_path):
    write(tmp_path, 'README.md', '''# 안내
[웹](https://example.invalid/missing#anchor)
[메일](mailto:person@example.invalid)
[상대 프로토콜](//example.invalid/missing)
`[인라인 예시](missing.md)`
````md
[코드 예시](missing.md)
```
## 실제로 없는 제목
````
~~~md
![예시 그림](missing.svg)
~~~
''')

    assert check_docs(tmp_path) == []


def test_code_block_headings_do_not_satisfy_real_anchor_links(tmp_path):
    write(tmp_path, 'README.md', '''# 안내
```md
## 코드 제목
```
[깨진 절](#코드-제목)
''')

    issues = check_docs(tmp_path)

    assert len(issues) == 1
    assert '#코드-제목' in issues[0]


def test_scans_host_instructions_skills_and_agents_without_git(tmp_path):
    for name in ('AGENTS.md', 'CLAUDE.md', 'docs/nested/guide.md',
                 'skills/work/SKILL.md', 'agents/subagent.md'):
        write(tmp_path, name, '[깨진 문서](missing.md)')
    write(tmp_path, '.venv/README.md', '[환경 내부](missing.md)')
    write(tmp_path, 'unrelated/README.md', '[검사 범위 밖](missing.md)')

    issues = check_docs(tmp_path)

    assert len(issues) == 5
    assert all(any(name + ':1:' in issue for issue in issues) for name in (
        'AGENTS.md', 'CLAUDE.md', 'docs/nested/guide.md',
        'skills/work/SKILL.md', 'agents/subagent.md'))


def test_plugin_root_link_is_resolved_and_still_checks_missing_files(tmp_path):
    write(tmp_path, 'agents/subagent.md', '''# Subagent
[계약](${CLAUDE_PLUGIN_ROOT}/skills/work/SKILL.md#업무)
[잘못된 계약](${CLAUDE_PLUGIN_ROOT}/skills/work/missing.md)
''')
    write(tmp_path, 'skills/work/SKILL.md', '# 업무')

    issues = check_docs(tmp_path)

    assert len(issues) == 1
    assert 'missing.md' in issues[0]


def test_repository_documentation_links_resolve():
    assert check_docs(Path(__file__).resolve().parents[1]) == []
