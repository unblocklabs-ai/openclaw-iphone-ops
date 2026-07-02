from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex


CONFIG_ENV_VAR = "OPENCLAW_IPHONE_CONFIG"
DEFAULT_CONFIG_RELATIVE_PATH = Path(".openclaw/iphone/config.env")
REPO_CONFIG_PATH = Path(".env")

CONFIG_KEYS = {
    "OPENCLAW_IPHONE_DEVICE",
    "OPENCLAW_IPHONE_WDA_PATH",
    "OPENCLAW_IPHONE_REPO_DIR",
    "OPENCLAW_IPHONE_RUNNER_BUNDLE_ID",
    "OPENCLAW_IPHONE_DEVELOPMENT_TEAM",
    "OPENCLAW_IPHONE_DESTINATION_TIMEOUT",
    "OPENCLAW_IPHONE_WDA_URL",
}


@dataclass(frozen=True)
class IPhoneConfig:
    values: dict[str, str]
    path: Path | None = None

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.values.get(key, default)

    @property
    def device(self) -> str | None:
        return self.get("OPENCLAW_IPHONE_DEVICE")

    @property
    def wda_url(self) -> str | None:
        return self.get("OPENCLAW_IPHONE_WDA_URL")


def load_config(*, env: dict[str, str] | None = None, cwd: Path | None = None) -> IPhoneConfig:
    source_env = env if env is not None else os.environ
    path = config_path(source_env, cwd=cwd)
    file_values = parse_env_file(path) if path else {}
    expansion_env = dict(source_env)
    expansion_env.update(file_values)
    values = {
        key: expand_config_value(value, expansion_env)
        for key, value in file_values.items()
    }
    for key in CONFIG_KEYS:
        value = source_env.get(key)
        if value:
            values[key] = value
    return IPhoneConfig(values=values, path=path)


def config_path(env: dict[str, str], *, cwd: Path | None = None) -> Path | None:
    explicit = env.get(CONFIG_ENV_VAR)
    if explicit:
        explicit_path = expand_user_path(explicit, env)
        if not explicit_path.is_file():
            raise ValueError(f"{CONFIG_ENV_VAR} points to a missing config file: {explicit_path}")
        return explicit_path

    default_path = default_config_path(env)
    if default_path.exists():
        return default_path

    repo_path = (cwd or Path.cwd()) / REPO_CONFIG_PATH
    if repo_path.exists():
        return repo_path

    return None


def default_config_path(env: dict[str, str]) -> Path:
    home = env.get("HOME")
    if home:
        return Path(home) / DEFAULT_CONFIG_RELATIVE_PATH
    return Path.home() / DEFAULT_CONFIG_RELATIVE_PATH


def expand_user_path(value: str, env: dict[str, str]) -> Path:
    if value == "~" or value.startswith("~/"):
        home = env.get("HOME")
        if home:
            return Path(home + value[1:])
    return Path(value).expanduser()


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if not path.is_file():
        raise ValueError(f"Config path is not a file: {path}")

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise ValueError(f"Invalid config line {line_number} in {path}: expected KEY=value.")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key not in CONFIG_KEYS:
            continue
        values[key] = parse_env_value(raw_value.strip(), path=path, line_number=line_number)
    return values


def parse_env_value(raw_value: str, *, path: Path, line_number: int) -> str:
    try:
        parts = shlex.split(raw_value, comments=True, posix=True)
    except ValueError as exc:
        raise ValueError(f"Invalid config line {line_number} in {path}: {exc}") from exc
    if not parts:
        return ""
    return " ".join(parts)


def expand_config_value(value: str, env: dict[str, str]) -> str:
    if value == "~" or value.startswith("~/"):
        home = env.get("HOME")
        expanded = home + value[1:] if home else os.path.expanduser(value)
    else:
        expanded = os.path.expanduser(value)

    def replace(match: re.Match[str]) -> str:
        braced = match.group("braced")
        bare = match.group("bare")
        key = braced or bare
        return env.get(key, match.group(0))

    return re.sub(r"\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|\$(?P<bare>[A-Za-z_][A-Za-z0-9_]*)", replace, expanded)
