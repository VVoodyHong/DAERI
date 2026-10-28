"""Prepared plugin fixtures that leave the developer's environment unchanged."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig

import pytest


@pytest.fixture
def plugin_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('DAERI_DATA_DIR', str(tmp_path / 'data'))
    plugin = tmp_path / 'plugin with spaces'
    shutil.copytree(Path(__file__).resolve().parents[1] / 'scripts', plugin / 'scripts')
    environment = plugin / '.venv'
    subprocess.run([sys.executable, '-I', '-m', 'venv', '--without-pip', str(environment)], check=True)
    portable = environment / 'bin/python.exe'
    portable.parent.mkdir(exist_ok=True)
    if os.name == 'nt':
        shutil.copy2(environment / 'Scripts/python.exe', portable)
        site_packages = environment / 'Lib/site-packages'
    else:
        portable.symlink_to('python')
        site_packages = environment / f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'
    # Reuse installed test dependencies without changing or installing any packages.
    (site_packages / 'test-dependencies.pth').write_text(
        'import site; site.addsitedir(' + repr(sysconfig.get_paths()['purelib']) + ')\n')
    return plugin
