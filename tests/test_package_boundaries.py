"""Core and application imports remain independent of outer layers and SDKs."""

from pathlib import Path
import subprocess
import sys


def test_core_and_application_load_without_outer_layers_or_external_sdks(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'src'
    script = '''
import importlib
import importlib.abc
import pkgutil
import sys
sys.path.insert(0, sys.argv[1])

class Boundary(importlib.abc.MetaPathFinder):
    blocked = ('daeri.application', 'daeri.infrastructure', 'daeri.config',
               'boto3', 'botocore', 'httpx', 'pydantic', 'yaml')

    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in self.blocked):
            raise AssertionError('Inner layer loaded an outer dependency: ' + fullname)

boundary = Boundary()
sys.meta_path.insert(0, boundary)

def load(package_name):
    package = importlib.import_module(package_name)
    for module in pkgutil.walk_packages(package.__path__, package_name + '.'):
        importlib.import_module(module.name)

load('daeri.core')
boundary.blocked = boundary.blocked[1:]
load('daeri.application')
'''
    result = subprocess.run([sys.executable, '-I', '-c', script, str(source)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
