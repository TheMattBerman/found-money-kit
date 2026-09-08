"""The diagnostic shell must never prepare an environment or invoke installers."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DoctorReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="doctor fixture ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copy2(ROOT / "doctor.sh", self.root / "doctor.sh")
        for name in [
            "README.md",
            "AGENTS.md",
            "LICENSE",
            "VERSION",
            "CHANGELOG.md",
            "install.sh",
            ".gitignore",
            ".env.example",
            "skills/found-money/SKILL.md",
            "assets/demo.gif",
            "output/sample-public.json",
        ]:
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()
        (self.root / "found_money").mkdir()
        (self.root / "scripts").mkdir()
        for name in ["doctor.py", "public_safety.py"]:
            (self.root / "scripts" / name).write_text(
                "import os\nassert os.environ['PYTHONDONTWRITEBYTECODE'] == '1'\nassert os.environ['PYTHONPATH'].split(os.pathsep)[0] == os.path.dirname(os.path.dirname(__file__))\n"
            )
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.marker = self.root / "installer-ran"
        for name in ["uv", "pip", "pip3"]:
            self.executable(self.bin / name, '#!/bin/sh\ntouch "$INSTALLER_MARKER"\nexit 97\n')
        (self.bin / "python3").symlink_to(sys.executable)
        self.env = dict(
            os.environ,
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            INSTALLER_MARKER=str(self.marker),
        )
        self.env.pop("FOUNDMONEY_DOCTOR_PYTHON", None)

    def executable(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(0o755)

    def run_doctor(self):
        return subprocess.run(
            ["bash", str(self.root / "doctor.sh")],
            cwd=self.root,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=15,
        )

    def test_global_interpreter_never_invokes_available_installers(self):
        result = self.run_doctor()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.root / ".venv").exists())
        self.assertFalse(list(self.root.rglob("__pycache__")))

    def test_existing_venv_is_used_without_uv(self):
        marker = self.root / "venv-used"
        self.env["VENV_MARKER"] = str(marker)
        self.env["EXISTING_PYTHON"] = sys.executable
        self.executable(
            self.root / ".venv/bin/python",
            '#!/bin/sh\ntouch "$VENV_MARKER"\nexec "$EXISTING_PYTHON" "$@"\n',
        )
        result = self.run_doctor()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(marker.exists())
        self.assertFalse(self.marker.exists())

    def test_invalid_explicit_interpreter_blocks_without_fallback(self):
        self.env["FOUNDMONEY_DOCTOR_PYTHON"] = str(self.root / "not installed")
        result = self.run_doctor()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("existing Python 3.11+", result.stdout)
        self.assertNotIn("bootstrap doctor:", result.stdout)
        self.assertFalse(self.marker.exists())

    def test_old_interpreter_blocks_before_package_checks(self):
        self.executable(self.root / "old-python", "#!/bin/sh\nexit 1\n")
        self.env["FOUNDMONEY_DOCTOR_PYTHON"] = str(self.root / "old-python")
        result = self.run_doctor()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("bootstrap doctor:", result.stdout)
        self.assertFalse(self.marker.exists())

    def test_missing_dependency_failure_is_not_repaired(self):
        (self.root / "scripts/public_safety.py").write_text(
            "raise ModuleNotFoundError('synthetic unavailable dependency')\n"
        )
        result = self.run_doctor()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("scripts/public_safety.py failed", result.stdout)
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
