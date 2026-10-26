"""Local source setup registers host observations without installing a program."""

from contextlib import closing
import os
from pathlib import Path

import pytest

from daeri.config import setup
from daeri.config.onboarding import apply_draft, create_draft, validate_draft
from daeri.config.setup_yaml import read_yaml, write_yaml
from daeri.core.errors import StoreError
from daeri.infrastructure.adapters import get_adapter
from daeri.infrastructure.storage import Store


def test_local_source_template_has_no_program_or_authentication(tmp_path):
    with closing(Store(tmp_path / 'data')) as store:
        resource = setup.profile_template(store, 'source-local', 'service', 'source')['resources'][0]
        assert resource['type'] == 'Source' and resource['adapter'] == 'local_source'
        assert resource['identity_ref'] == 'local-user'
        assert resource['connection'] == {'transport': 'host'}
        assert resource['capabilities'] == [{'capability_id': 'source-observe',
                                             'tool_name': 'source_observe', 'constraints': {}}]
        assert setup.setup_schema('source-local')['authentication']['credential_environment'] == []
        assert setup.resource_credential_fields(resource) == {}
        assert not (store.data_dir / 'tools').exists()


def test_multiple_source_paths_apply_and_prepare_without_subprocess(tmp_path, monkeypatch):
    roots = [tmp_path / 'project-one', tmp_path / 'project-two']
    for root in roots:
        root.mkdir()
    monkeypatch.setattr(setup.subprocess, 'run', lambda *args, **kwargs: pytest.fail('Host sources need no program'))
    with closing(Store(tmp_path / 'data')) as store:
        draft = create_draft(store, 'service', recipes=['source-local', 'source-local'], embedding='keyword_only')
        document = read_yaml(draft['path'])
        assert list(document['resources']) == ['source', 'source-2']
        for block, root in zip(document['resources'].values(), roots):
            assert block == {'adapter': 'source-local', 'inputs': {'path': None}}
            block['inputs']['path'] = str(root)
        Path(draft['path']).write_text(write_yaml(document), encoding='utf-8')
        checked = validate_draft(store, 'service')
        assert checked['valid'], checked['issues']
        saved = apply_draft(store, 'service', checked['confirmation_token'])
        assert [item['target'] for item in saved['resources']] == [{'path': str(root)} for root in roots]
        prepared = setup.prepare_resources(store, 'service')
        assert prepared['ready'] is True
        assert all(item['transport'] == 'host' and item['ready'] for item in prepared['resources'])
        assert not (store.data_dir / 'tools').exists()


@pytest.mark.parametrize('value', ['relative/project', '/missing-local-source', '', 'https://example.test'])
def test_local_source_rejects_invalid_root_before_publication(tmp_path, value):
    with closing(Store(tmp_path / 'data')) as store:
        with pytest.raises(StoreError, match='INVALID_INPUT'):
            setup.build_setup_resource(store, 'source-local', 'service', 'source', {'path': value})
        assert store.list_profiles() == []


def test_local_source_observation_returns_host_summary_without_reading_content(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'private.txt').write_text('PRIVATE_FILE_CONTENT', encoding='utf-8')
    adapter = get_adapter('local_source')
    resource = {'target': {'path': str(root)}, 'connection': {'transport': 'host'}}
    result = adapter.observe(resource, {'summary': '호스트가 변경 사항을 확인했습니다.', 'paths': ['private.txt']})
    assert result == {'path': str(root), 'summary': '호스트가 변경 사항을 확인했습니다.',
                      'paths': ['private.txt'], 'observation_source': 'host'}
    tool = adapter.host_tools()[0]
    assert tool['name'] == 'source_observe'
    assert tool['inputSchema']['required'] == ['summary']
    assert tool['inputSchema']['additionalProperties'] is False
    assert adapter.observe(resource, {'summary': '프로젝트 확인'})['paths'] == []


@pytest.mark.parametrize('path', ['../outside', '/absolute', 'a/../file', 'a\\file', './file'])
def test_local_source_normalize_rejects_ambiguous_relative_paths(path):
    with pytest.raises(StoreError, match='SCOPE_VIOLATION'):
        get_adapter('local_source').normalize({'summary': '확인', 'paths': [path]}, {}, {})


@pytest.mark.skipif(os.name == 'nt', reason='POSIX symlink boundary')
def test_local_source_observation_rejects_missing_and_escaped_paths(tmp_path):
    root, outside = tmp_path / 'project', tmp_path / 'outside'
    root.mkdir()
    outside.mkdir()
    (root / 'escape').symlink_to(outside, target_is_directory=True)
    resource = {'target': {'path': str(root)}, 'connection': {'transport': 'host'}}
    adapter = get_adapter('local_source')
    with pytest.raises(StoreError, match='NOT_FOUND'):
        adapter.observe(resource, {'summary': '확인', 'paths': ['missing']})
    with pytest.raises(StoreError, match='SCOPE_VIOLATION'):
        adapter.observe(resource, {'summary': '확인', 'paths': ['escape']})


def test_unavailable_project_keeps_profile_readable_and_returns_prepare_failure(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    with closing(Store(tmp_path / 'data')) as store:
        resource = setup.build_setup_resource(store, 'source-local', 'service', 'source', {'path': str(root)})
        saved = store.put_profile('service', resources=[resource])
        root.rmdir()
        assert store.get_profile('service') == saved
        assert setup.export_profile(store, 'service')['resources'] == [resource]
        assert setup.prepare_resources(store, 'service') == {'ready': False, 'resources': [{
            'instance_id': 'source', 'recipe': 'source-local', 'ready': False,
            'error': {'code': 'INVALID_INPUT'}}]}


@pytest.mark.parametrize('automatic', [False, True])
def test_setup_reopens_a_moved_project_and_applies_its_new_path(tmp_path, automatic):
    root, moved = tmp_path / 'project', tmp_path / 'moved-project'
    root.mkdir()
    with closing(Store(tmp_path / 'data')) as store:
        resource = setup.build_setup_resource(store, 'source-local', 'service', 'source', {'path': str(root)})
        store.put_profile('service', resources=[resource], embedding={'provider': 'keyword_only'})
        root.rename(moved)
        draft = (create_draft(store, 'service', recipes=['source-local']) if automatic else
                 create_draft(store, 'service', {'source': 'source-local'}))
        document = read_yaml(draft['path'])
        assert list(document['resources']) == ['source']
        assert document['resources']['source']['inputs'] == {'path': str(root)}
        document['resources']['source']['inputs']['path'] = str(moved)
        Path(draft['path']).write_text(write_yaml(document), encoding='utf-8')
        checked = validate_draft(store, 'service')
        assert checked['valid'], checked['issues']
        saved = apply_draft(store, 'service', checked['confirmation_token'])
        assert saved['resources'][0]['target'] == {'path': str(moved)}
        assert setup.prepare_resources(store, 'service')['ready'] is True


@pytest.mark.parametrize('parameters', [
    {'summary': ''}, {'summary': '  '}, {'summary': '확인', 'paths': ['a', 'a']},
    {'summary': '확인', 'paths': [str(number) for number in range(101)]},
    {'summary': '확인', 'command': 'git status'},
])
def test_local_source_rejects_invalid_host_observation_inputs(tmp_path, parameters):
    resource = {'target': {'path': str(tmp_path)}, 'connection': {'transport': 'host'}}
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        get_adapter('local_source').observe(resource, parameters)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX symlink boundary')
def test_setup_canonicalizes_a_project_symlink_and_rejects_later_root_redirection(tmp_path):
    root, link, outside = tmp_path / 'project', tmp_path / 'link', tmp_path / 'outside'
    root.mkdir()
    outside.mkdir()
    link.symlink_to(root, target_is_directory=True)
    with closing(Store(tmp_path / 'data')) as store:
        resource = setup.build_setup_resource(store, 'source-local', 'service', 'source', {'path': str(link)})
        assert resource['target'] == {'path': str(root.resolve())}
        root.rmdir()
        root.symlink_to(outside, target_is_directory=True)
        with pytest.raises(StoreError, match='CONTEXT_CHANGED'):
            get_adapter('local_source').check_available(resource)


@pytest.mark.parametrize('path', ['/missing-local-project', r'C:\Projects\missing-local-project'])
def test_local_source_structure_preserves_unavailable_cross_platform_paths(path):
    resource = {'target': {'path': path}, 'connection': {'transport': 'host'}}
    adapter = get_adapter('local_source')
    adapter.validate_connection(resource)
    assert resource['target']['path'] == path
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        adapter.check_available(resource)


def test_yaml_explicit_adapter_selection_replaces_unavailable_connection_without_old_metadata(tmp_path, monkeypatch):
    import daeri.infrastructure.adapters as adapters
    with closing(Store(tmp_path / 'data')) as store:
        old = setup.build_setup_resource(store, 'document-confluence', 'service', 'source',
                                         {'server': 'https://docs.example.test', 'username': 'reader'})
        old['policy'] = {'allowed_model_fields': []}
        before = store.put_profile('service', resources=[old], embedding={'provider': 'keyword_only'})
        monkeypatch.delitem(adapters.BUILTIN_ADAPTERS, 'confluence_read')
        draft = create_draft(store, 'service', {'source': 'source-local'})
        document = read_yaml(draft['path'])
        assert document['resources']['source'] == {'adapter': 'source-local', 'inputs': {'path': None}}
        document['resources']['source']['inputs']['path'] = str(tmp_path)
        Path(draft['path']).write_text(write_yaml(document), encoding='utf-8')
        checked = validate_draft(store, 'service')
        assert checked['valid'], checked['issues']
        assert checked['preview']['changes']['updated_resources'] == ['source']
        assert store.get_profile('service') == before
        saved = apply_draft(store, 'service', checked['confirmation_token'])
        expected = setup.build_setup_resource(store, 'source-local', 'service', 'source', {'path': str(tmp_path)})
        assert saved['resources'] == [expected]
        assert before['resources'] == [old]


def test_explicit_selection_replaces_an_existing_yaml_product_block(tmp_path):
    with closing(Store(tmp_path / 'data')) as store:
        first = create_draft(store, 'service', {'shared': 'document-confluence'}, embedding='keyword_only')
        document = read_yaml(first['path'])
        document['resources']['shared']['inputs'].update(server='https://docs.example.test', username='reader')
        Path(first['path']).write_text(write_yaml(document), encoding='utf-8')
        create_draft(store, 'service', {'shared': 'source-local'})
        changed = read_yaml(first['path'])['resources']['shared']
        assert changed == {'adapter': 'source-local', 'inputs': {'path': None}}
