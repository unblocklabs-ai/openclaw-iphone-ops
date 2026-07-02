from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from openclaw_iphone.config import load_config, parse_env_file


class ConfigTests(unittest.TestCase):
    def test_load_config_reads_file_expands_home_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.env"
            config_path.write_text(
                "\n".join(
                    [
                        'OPENCLAW_IPHONE_DEVICE="Configured iPhone"',
                        'OPENCLAW_IPHONE_REPO_DIR="$HOME/.openclaw/repos/openclaw-iphone"',
                        'OPENCLAW_IPHONE_WDA_PATH="${HOME}/.openclaw/iphone/WebDriverAgent"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            config = load_config(
                env={
                    "OPENCLAW_IPHONE_CONFIG": str(config_path),
                    "HOME": "/tmp/example-home",
                }
            )

        self.assertEqual(config.device, "Configured iPhone")
        self.assertEqual(config.get("OPENCLAW_IPHONE_REPO_DIR"), "/tmp/example-home/.openclaw/repos/openclaw-iphone")
        self.assertEqual(config.get("OPENCLAW_IPHONE_WDA_PATH"), "/tmp/example-home/.openclaw/iphone/WebDriverAgent")

    def test_environment_overrides_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.env"
            config_path.write_text('OPENCLAW_IPHONE_DEVICE="File iPhone"\n', encoding="utf-8")

            config = load_config(
                env={
                    "OPENCLAW_IPHONE_CONFIG": str(config_path),
                    "OPENCLAW_IPHONE_DEVICE": "Env iPhone",
                }
            )

        self.assertEqual(config.device, "Env iPhone")

    def test_default_config_path_uses_supplied_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            config_dir = home / ".openclaw/iphone"
            config_dir.mkdir(parents=True)
            (config_dir / "config.env").write_text(
                'OPENCLAW_IPHONE_DEVICE="Temp Home iPhone"\n',
                encoding="utf-8",
            )

            config = load_config(env={"HOME": str(home)}, cwd=Path(tmp))

        self.assertEqual(config.device, "Temp Home iPhone")

    def test_explicit_missing_config_path_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.env"

            with self.assertRaisesRegex(ValueError, "OPENCLAW_IPHONE_CONFIG points to a missing config file"):
                load_config(env={"OPENCLAW_IPHONE_CONFIG": str(missing)})

    def test_parse_env_file_ignores_unknown_keys_and_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.env"
            config_path.write_text(
                "\n".join(
                    [
                        "# comment",
                        'export OPENCLAW_IPHONE_DEVICE="Pearl\'s iPhone" # inline comment',
                        "UNRELATED=value",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            values = parse_env_file(config_path)

        self.assertEqual(values, {"OPENCLAW_IPHONE_DEVICE": "Pearl's iPhone"})


if __name__ == "__main__":
    unittest.main()
