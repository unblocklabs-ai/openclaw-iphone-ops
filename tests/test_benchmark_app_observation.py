import contextlib
import importlib
import io
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/benchmark_app_observation.py"


class BenchmarkSourceTests(unittest.TestCase):
    def invoke(self, source, output, config):
        error = io.StringIO()
        argv = [str(SCRIPT), "--source", str(source), "--output", str(output), "--arm", "baseline"]
        with patch.object(sys, "argv", argv), patch.object(sys, "path", list(sys.path)), \
                patch("openclaw_iphone.config.load_config", config), contextlib.redirect_stderr(error):
            with self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertFalse(output.exists())
        return caught.exception.code, error.getvalue()

    def test_missing_or_empty_source_cannot_fall_back_to_importable_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty = root / "empty"
            (empty / "src/openclaw_iphone").mkdir(parents=True)
            for source in (root / "missing", empty):
                with self.subTest(source=source.name):
                    config = Mock(side_effect=AssertionError("Configuration must not be read"))
                    code, error = self.invoke(source, root / "output", config)
                    self.assertEqual(code, 2)
                    self.assertIn("--source", error)
                    config.assert_not_called()

    def test_preloaded_package_from_another_checkout_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / "src/openclaw_iphone", root / "src/openclaw_iphone",
                            ignore=shutil.ignore_patterns("__pycache__"))
            config = Mock(side_effect=AssertionError("Configuration must not be read"))
            code, error = self.invoke(root, root / "output", config)
            self.assertEqual(code, 2)
            self.assertIn("--source", error)
            config.assert_not_called()

    def test_matching_source_reaches_configuration_without_device_work(self):
        # Legitimate subpackages may already be loaded by a caller.
        importlib.import_module("openclaw_iphone.recipes")
        with tempfile.TemporaryDirectory() as tmp:
            config = Mock(return_value=Mock(device=None))
            code, error = self.invoke(ROOT, Path(tmp) / "output", config)
            self.assertEqual(code, "An existing explicit physical-device configuration is required", error)
            config.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
