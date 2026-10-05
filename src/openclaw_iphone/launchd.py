"""Shared plist rendering for the two explicit launchd installers."""
import os
from pathlib import Path
import plistlib

from .config import load_config


def install_launchagent(script_repo: Path, template: Path, target: Path,
                        wrapper_name: str, *, interval: int | None = None) -> None:
    config = load_config(cwd=script_repo)
    repo = Path(config.get("OPENCLAW_IPHONE_REPO_DIR") or script_repo).expanduser()
    if not (repo / "src/openclaw_iphone").is_dir():
        raise SystemExit(f"Repo dir does not contain src/openclaw_iphone: {repo}")
    if not (repo / "snippets/launchd" / wrapper_name).is_file():
        raise SystemExit("Launchd wrapper is missing.")

    def replace(value):
        if isinstance(value, str):
            return value.replace("__HOME__", str(Path.home())).replace("__REPO_DIR__", str(repo))
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        return value

    with template.open("rb") as stream:
        rendered = replace(plistlib.load(stream))
    environment = {}
    if config.path:
        environment["OPENCLAW_IPHONE_CONFIG"] = str(config.path.resolve())
    if os.environ.get("OPENCLAW_IPHONE_PYTHON"):
        environment["OPENCLAW_IPHONE_PYTHON"] = os.environ["OPENCLAW_IPHONE_PYTHON"]
    if environment:
        rendered["EnvironmentVariables"] = environment
    if interval is not None:
        rendered["StartInterval"] = interval
    if "__HOME__" in str(rendered) or "__REPO_DIR__" in str(rendered):
        raise SystemExit("Rendered plist still contains unresolved placeholders.")
    serialized = plistlib.dumps(rendered)
    for key in ("StandardOutPath", "StandardErrorPath"):
        fd = os.open(rendered[key], os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_nlink != 1:
                raise SystemExit("Refusing a hardlinked service log.")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    target.write_bytes(serialized)
