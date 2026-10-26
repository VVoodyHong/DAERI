"""Package boundaries keep policy imports independent and installed assets resolvable."""

from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_core_contracts_and_policy_load_without_runtime_or_external_implementations(tmp_path):
    script = '''
import importlib.abc
import sys

class CoreBoundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('daeri.application', 'daeri.infrastructure', 'daeri.config')):
            raise AssertionError('Core loaded an outer implementation: ' + fullname)

sys.meta_path.insert(0, CoreBoundary())
from daeri.core.contracts import INPUT_MODELS
from daeri.core.errors import StoreError
from daeri.core.policy import effective_profile_memory_policy, retention_deadline
from daeri.core.security import redact
assert len(INPUT_MODELS) == 16
assert retention_deadline(None) is None
assert effective_profile_memory_policy({'resources': []})['target_bindings'] == {}
assert redact('password=private') == 'password=[REDACTED]'
'''
    result = subprocess.run([sys.executable, '-I', '-c', script], cwd=tmp_path,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_installed_package_resolves_catalog_and_bundled_recipes(tmp_path):
    package = tmp_path / 'daeri'
    shutil.copytree(ROOT / 'src/daeri', package, ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(ROOT / 'recipes', package / 'recipes')
    script = '''
import pathlib
import sys
sys.path.insert(0, sys.argv[1])
from daeri.config.setup import _recipe, load_catalog
package = pathlib.Path(sys.argv[1]) / 'daeri'
for name in load_catalog():
    item, project, lock = _recipe(name)
    assert project == package / 'recipes' / name
    assert lock and item['adapter']
assert (package / 'infrastructure/provider_login.py').is_file()
'''
    result = subprocess.run([sys.executable, '-I', '-c', script, str(tmp_path)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
