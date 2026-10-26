"""Profile portability preserves separately managed source projects."""

from contextlib import closing
import json

import pytest

from daeri.application.runtime import Runtime
from daeri.config.portable_archive import open_bundle
from daeri.config.profile_portability import backup_profile, inspect_backup, restore_profile
from daeri.config.setup import build_setup_resource, prepare_resources
from daeri.core.errors import StoreError
from daeri.infrastructure.resources import ResourceRegistry
from daeri.infrastructure.storage import Store


PASSWORD = 'synthetic-source-profile-password'
SOURCE_BYTES = b'LOCAL_SOURCE_BYTES_NOT_IN_PROFILE_BACKUP\n'
GIT_BYTES = b'[remote "origin"]\n\turl = ssh://git.example.test/team/service\n'


def source_project(directory):
    root = directory / 'project'
    root.mkdir()
    (root / 'app.py').write_bytes(SOURCE_BYTES)
    (root / '.git').mkdir()
    (root / '.git/config').write_bytes(GIT_BYTES)
    return root.resolve()


def test_encrypted_profile_restores_unavailable_source_path_without_copying_project(tmp_path):
    root = source_project(tmp_path)
    archive = tmp_path / 'profile.daeri'
    with closing(Runtime(tmp_path / 'source-data')) as source:
        resource = build_setup_resource(source.store, 'source-local', 'service', 'source',
                                         {'path': str(root)})
        profile = source.store.put_profile('service', resources=[resource],
                                            embedding={'provider': 'keyword_only'})
        backup_profile(source, 'service', archive, PASSWORD)
        assert (root / 'app.py').read_bytes() == SOURCE_BYTES
        assert (root / '.git/config').read_bytes() == GIT_BYTES
        with open_bundle(archive, PASSWORD) as (manifest, packed):
            assert manifest['profile']['resources'] == [resource]
            assert packed.namelist() == ['manifest.json']
            assert SOURCE_BYTES.decode().strip() not in json.dumps(manifest)
            assert resource['connection'] == {'transport': 'host'}

        offline = tmp_path / 'project-offline'
        root.rename(offline)
        preview = inspect_backup(archive, PASSWORD)
        # Configuration and recorded history remain portable while the project is offline.
        backup_profile(source, 'service', tmp_path / 'offline-profile.daeri', PASSWORD)
        with closing(Store(tmp_path / 'restored-data')) as target:
            restored = restore_profile(target, archive, PASSWORD, preview['confirmation_token'])
            assert restored['profile_id'] == profile['profile_id']
            saved = target.get_profile('service')
            assert saved['resources'] == [resource]
            assert not root.exists()
            assert not (target.data_dir / 'tools').exists()
            prepared = prepare_resources(target, 'service')
            assert prepared == {'ready': False, 'resources': [{
                'instance_id': 'source', 'recipe': 'source-local', 'ready': False,
                'error': {'code': 'INVALID_INPUT'}}]}
            with pytest.raises(StoreError, match='INVALID_INPUT'):
                ResourceRegistry(target).diagnose(saved, 'source')
            assert target.get_profile('service') == saved
        assert (offline / 'app.py').read_bytes() == SOURCE_BYTES
        assert (offline / '.git/config').read_bytes() == GIT_BYTES
