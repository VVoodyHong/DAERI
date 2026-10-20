import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.name == "nt", "POSIX launch tests; Windows requires separate validation")
class PluginLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="daeri plugin ")
        self.addCleanup(self.temp.cleanup)
        self.plugin = Path(self.temp.name) / "plugin with spaces"
        self.scripts = self.plugin / "scripts"
        self.scripts.mkdir(parents=True)
        for name in ("launch.sh", "setup.sh", "dependencies.env"):
            shutil.copy2(ROOT / "scripts" / name, self.scripts / name)
        self.env = dict(os.environ, PATH="/usr/bin:/bin")

    def run_script(self, name, *args):
        return subprocess.run(
            ["/bin/sh", str(self.scripts / name), *args], cwd=self.temp.name,
            env=self.env, text=True, capture_output=True, timeout=10,
        )

    def make_python(self, exit_code=0):
        python = self.plugin / ".venv/bin/python.exe"
        python.parent.mkdir(parents=True)
        python.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "assert not any(key.startswith(('UV_', 'PYTHON', '_PYTHON', 'CONDA_')) or key in ['VIRTUAL_ENV', 'VIRTUAL_ENV_PROMPT', '__PYVENV_LAUNCHER__', '_OLD_VIRTUAL_PATH', '_OLD_VIRTUAL_PYTHONHOME', 'PYLAUNCHER_DEBUG'] for key in os.environ)\n"
            "print(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd()}))\n"
            f"sys.exit({exit_code})\n"
        )
        python.chmod(0o755)

    def test_launch_missing_environment_explains_setup_without_stdout(self):
        result = self.run_script("launch.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("setup.sh", result.stderr)
        self.assertIn("--prepare", result.stderr)
        self.assertFalse((self.plugin / ".venv").exists())

    def test_launch_uses_plugin_python_from_any_working_directory(self):
        self.make_python(exit_code=17)
        self.env.update(UV_NO_PROJECT='true', PYTHONHOME='foreign', PYTHONPATH='foreign',
                        VIRTUAL_ENV='foreign', VIRTUAL_ENV_PROMPT='foreign', CONDA_PREFIX='foreign',
                        _OLD_VIRTUAL_PYTHONHOME='foreign', __PYVENV_LAUNCHER__='foreign')
        result = self.run_script("launch.sh", "--test-marker")
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "args": ["-I", "-m", "daeri.server", "--test-marker"],
            "cwd": str(self.plugin.resolve()),
        })
        self.assertEqual(result.stderr, "")

    def test_setup_check_does_not_prepare_missing_environment(self):
        result = self.run_script("setup.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--prepare", result.stderr)
        self.assertFalse((self.plugin / ".venv").exists())

    def test_setup_unknown_arguments_are_rejected_before_preparation(self):
        result = self.run_script("setup.sh", "--prepare", "unexpected")
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.plugin / ".venv").exists())


if __name__ == "__main__":
    unittest.main()
