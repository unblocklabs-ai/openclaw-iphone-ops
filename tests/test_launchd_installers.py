from __future__ import annotations

import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]


class LaunchdInstallerTests(unittest.TestCase):
    def run_installer(self, script: str, *, home: Path, config: Path, interval: str | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "OPENCLAW_IPHONE_CONFIG": str(config),
            }
        )
        env.pop("OPENCLAW_IPHONE_REPO_DIR", None)
        if interval is not None:
            env["OPENCLAW_IPHONE_WATCHDOG_INTERVAL"] = interval

        return subprocess.run(
            ["sh", str(REPO / script)],
            cwd=REPO,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_installers_render_repo_dir_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            configured_repo = Path(tmp) / "Configured & Repo" / "openclaw-iphone"
            configured_repo.parent.mkdir()
            configured_repo.symlink_to(REPO, target_is_directory=True)
            config = Path(tmp) / "config.env"
            config.write_text(f'OPENCLAW_IPHONE_REPO_DIR="{configured_repo}"\n', encoding="utf-8")

            wda = self.run_installer(
                "snippets/launchd/install-wda-run-launchagent.sh",
                home=home,
                config=config,
            )
            watchdog = self.run_installer(
                "snippets/launchd/install-watchdog-launchagent.sh",
                home=home,
                config=config,
                interval="300",
            )

            self.assertEqual(wda.returncode, 0, wda.stderr + wda.stdout)
            self.assertEqual(watchdog.returncode, 0, watchdog.stderr + watchdog.stdout)

            wda_plist = home / "Library/LaunchAgents/com.openclaw.iphone-wda-run.plist"
            watchdog_plist = home / "Library/LaunchAgents/com.openclaw.iphone-watchdog.plist"
            with wda_plist.open("rb") as fh:
                wda_data = plistlib.load(fh)
            with watchdog_plist.open("rb") as fh:
                watchdog_data = plistlib.load(fh)
            self.assertEqual(
                wda_data["ProgramArguments"],
                [f"{configured_repo}/snippets/launchd/openclaw-iphone-wda-run.sh"],
            )
            self.assertEqual(
                watchdog_data["ProgramArguments"],
                [f"{configured_repo}/snippets/launchd/openclaw-iphone-watchdog.sh"],
            )
            self.assertEqual(watchdog_data["StartInterval"], 300)

    def test_watchdog_installer_rejects_zero_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            config = Path(tmp) / "config.env"
            config.write_text(f'OPENCLAW_IPHONE_REPO_DIR="{REPO}"\n', encoding="utf-8")

            result = self.run_installer(
                "snippets/launchd/install-watchdog-launchagent.sh",
                home=home,
                config=config,
                interval="0",
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("positive integer", result.stderr)

    def test_watchdog_installer_rejects_overflow_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            config = Path(tmp) / "config.env"
            config.write_text(f'OPENCLAW_IPHONE_REPO_DIR="{REPO}"\n', encoding="utf-8")

            result = self.run_installer(
                "snippets/launchd/install-watchdog-launchagent.sh",
                home=home,
                config=config,
                interval="999999999999999999999999999999",
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("between 1 and 86400", result.stderr)

    def test_installer_reads_repo_local_env_from_script_repo_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            script_repo = Path(tmp) / "script-repo"
            script_repo.mkdir()
            (script_repo / "src").symlink_to(REPO / "src", target_is_directory=True)
            shutil.copytree(REPO / "snippets", script_repo / "snippets")
            configured_repo = Path(tmp) / "Configured Repo" / "openclaw-iphone"
            configured_repo.parent.mkdir()
            configured_repo.symlink_to(REPO, target_is_directory=True)
            (script_repo / ".env").write_text(f'OPENCLAW_IPHONE_REPO_DIR="{configured_repo}"\n', encoding="utf-8")
            env = os.environ.copy()
            env["HOME"] = str(home)
            env.pop("OPENCLAW_IPHONE_CONFIG", None)
            env.pop("OPENCLAW_IPHONE_REPO_DIR", None)
            result = subprocess.run(
                ["sh", str(script_repo / "snippets/launchd/install-wda-run-launchagent.sh")],
                cwd=tmp,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            plist_path = home / "Library/LaunchAgents/com.openclaw.iphone-wda-run.plist"
            with plist_path.open("rb") as fh:
                data = plistlib.load(fh)
            self.assertEqual(
                data["ProgramArguments"],
                [f"{configured_repo}/snippets/launchd/openclaw-iphone-wda-run.sh"],
            )


if __name__ == "__main__":
    unittest.main()
