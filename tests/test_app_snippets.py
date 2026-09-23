from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from openclaw_iphone.cli import device_selector_from_args
from openclaw_iphone.config import load_config


ROOT = Path(__file__).resolve().parents[1]


class AppSnippetTests(unittest.TestCase):
    def test_wrappers_use_one_cli_process_and_forward_only_explicit_selector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo with spaces"
            repo.mkdir()
            stub_dir = base / "stub"
            stub_dir.mkdir()
            python = stub_dir / "python3"
            python.write_text(
                '#!/bin/sh\nprintf "called\\n" >> "$STUB_LOG"\n'
                'printf "CWD=%s\\n" "$PWD"\nprintf "ARG=%s\\n" "$@"\n', encoding="utf-8"
            )
            python.chmod(0o755)
            log = base / "calls"
            env = {**os.environ, "PATH": f"{stub_dir}:{os.environ['PATH']}",
                   "OPENCLAW_IPHONE_REPO_DIR": str(repo), "STUB_LOG": str(log),
                   "DEVICE_ID": "Pearl's iPhone", "BUNDLE_ID": "com.example.app with space"}
            cases = (("iphone-launch-app.sh", ["apps", "launch", "--device", "Pearl's iPhone", "com.example.app with space"]),
                     ("iphone-installed-apps.sh", ["apps", "list", "--device", "Pearl's iPhone"]))
            for script, args in cases:
                with self.subTest(script=script):
                    log.unlink(missing_ok=True)
                    result = subprocess.run([str(ROOT / "snippets" / script)], env=env, capture_output=True, text=True, check=True)
                    self.assertEqual(log.read_text(), "called\n")
                    self.assertEqual(result.stdout.splitlines(), [f"CWD={repo}", "ARG=-m", "ARG=openclaw_iphone", *(f"ARG={arg}" for arg in args)])

            env["DEVICE_ID"] = ""
            for script, args in (("iphone-launch-app.sh", ["apps", "launch", "com.example.app with space"]),
                                 ("iphone-installed-apps.sh", ["apps", "list"])):
                with self.subTest(script=script, selector="empty"):
                    log.unlink(missing_ok=True)
                    result = subprocess.run([str(ROOT / "snippets" / script)], env=env, capture_output=True, text=True, check=True)
                    self.assertEqual(log.read_text(), "called\n")
                    self.assertEqual(result.stdout.splitlines()[1:], ["ARG=-m", "ARG=openclaw_iphone", *(f"ARG={arg}" for arg in args)])

    def test_deferred_cli_selection_keeps_explicit_env_file_then_auto_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo with spaces"
            repo.mkdir()
            config_file = repo / ".env"
            config_file.write_text('OPENCLAW_IPHONE_DEVICE="file phone"\n', encoding="utf-8")
            env = {"HOME": tmp, "OPENCLAW_IPHONE_DEVICE": "env phone"}
            config = load_config(env=env, cwd=repo)
            self.assertEqual(device_selector_from_args(argparse.Namespace(device="explicit phone"), config=config), "explicit phone")
            self.assertEqual(device_selector_from_args(argparse.Namespace(device=None), config=config), "env phone")
            self.assertEqual(device_selector_from_args(argparse.Namespace(device=None), config=load_config(env={"HOME": tmp}, cwd=repo)), "file phone")
            config_file.unlink()
            self.assertIsNone(device_selector_from_args(argparse.Namespace(device=None), config=load_config(env={"HOME": tmp}, cwd=repo)))
