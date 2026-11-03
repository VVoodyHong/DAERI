"""Core and application imports remain independent of outer layers and SDKs."""

from pathlib import Path
import subprocess
import sys


def test_core_and_application_load_without_outer_layers_or_external_sdks(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'src'
    script = '''
import importlib.abc
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
import daeri.core.cloud
import daeri.core.errors
import daeri.core.serialization
import daeri.core.approval
import daeri.core.access
import daeri.core.endpoints
boundary.blocked = boundary.blocked[1:]
from daeri.application.cloud import CloudWorkflow
from daeri.application.agent import AgentService
'''
    result = subprocess.run([sys.executable, '-I', '-c', script, str(source)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
