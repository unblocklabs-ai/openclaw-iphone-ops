from __future__ import annotations

import json
import base64
import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts.check_release import ROOT, check_release
from scripts.publish_npm import publish


class DistributionTests(unittest.TestCase):
    def run_launcher(self, python: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(ROOT / "bin/openclaw-iphone"), *args],
            env=dict(os.environ, OPENCLAW_IPHONE_PYTHON=str(python)),
            text=True, capture_output=True, timeout=10,
        )

    def fake_python(self, directory: str, source: str) -> Path:
        path = Path(directory) / "python with spaces"
        path.write_text(f"#!{sys.executable}\n{source}\n")
        path.chmod(0o700)
        return path

    def test_missing_python_has_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_launcher(Path(directory) / "missing-python", "--help")
        self.assertEqual(result.returncode, 127)
        self.assertIn("requires Python 3.11+", result.stderr)

    def test_old_python_fails_before_importing_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            python = self.fake_python(directory, "import sys\nsys.version_info = (3, 10)\nexec(sys.argv[3])")
            result = self.run_launcher(python, "--help")
        self.assertEqual(result.returncode, 1)
        self.assertIn("requires Python 3.11+", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_launcher_preserves_arguments_and_exit_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            python = self.fake_python(directory, "import json, sys\nprint(json.dumps(sys.argv[5:]))\nsys.exit(37)")
            args = ("ui", "type", "spaces; $() `literal`", "")
            result = self.run_launcher(python, *args)
        self.assertEqual(result.returncode, 37)
        self.assertEqual(json.loads(result.stdout), list(args))

    def test_launcher_preserves_process_signal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            python = self.fake_python(directory, "import os, signal\nos.kill(os.getpid(), signal.SIGTERM)")
            result = self.run_launcher(python, "--help")
        self.assertEqual(result.returncode, -signal.SIGTERM)

    def test_release_tag_and_version_alignment(self) -> None:
        version = check_release(ROOT)
        self.assertEqual(check_release(ROOT, f"v{version}"), version)
        with self.assertRaisesRegex(ValueError, "tag"):
            check_release(ROOT, "v999.0.0")

    def test_release_rejects_mismatched_python_or_npm_versions(self) -> None:
        paths = ("package.json", "package-lock.json", "pyproject.toml", "src/openclaw_iphone/__init__.py")
        version = check_release(ROOT)
        for changed in paths:
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for relative in paths:
                    target = root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT / relative, target)
                target = root / changed
                target.write_text(target.read_text().replace(version, "999.0.0"))
                with self.assertRaises(ValueError):
                    check_release(root)

    def test_publish_skips_only_identical_registry_tarball(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tarball = Path(directory) / "package.tgz"
            tarball.write_bytes(b"tested archive")
            integrity = "sha512-" + base64.b64encode(hashlib.sha512(tarball.read_bytes()).digest()).decode()
            for registry_integrity in (integrity, "sha512-different"):
                with self.subTest(integrity=registry_integrity), mock.patch("scripts.publish_npm.subprocess.run") as run:
                    run.return_value = subprocess.CompletedProcess([], 0, json.dumps(registry_integrity))
                    if registry_integrity == integrity:
                        publish(tarball)
                    else:
                        with self.assertRaisesRegex(ValueError, "different contents"):
                            publish(tarball)
                    self.assertEqual(run.call_count, 1)

    def test_publish_does_not_treat_registry_auth_failure_as_missing_package(self) -> None:
        for code in ("E404", "E401"):
            with self.subTest(code=code), mock.patch("scripts.publish_npm.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 1, json.dumps({"error": {"code": code}}))
                if code == "E404":
                    publish(Path("tested.tgz"))
                    self.assertEqual(run.call_count, 2)
                    self.assertIn("--provenance", run.call_args.args[0])
                else:
                    with self.assertRaisesRegex(ValueError, "lookup failed"):
                        publish(Path("tested.tgz"))
                    self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
