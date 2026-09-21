"""Check the shared stable version before packaging or releasing."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import runpy
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def check_release(root: Path, tag: str | None = None) -> str:
    package = json.loads((root / "package.json").read_text())
    lock = json.loads((root / "package-lock.json").read_text())
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    version = package["version"]
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ValueError("Releases must use a stable X.Y.Z version.")
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"Release tag {tag!r} does not match v{version}.")
    if package["name"] != "@unblocklabs/openclaw-iphone-ops":
        raise ValueError("Unexpected npm package name.")
    versions = {
        "package-lock.json": lock["version"],
        "package-lock.json root": lock["packages"][""]["version"],
        "pyproject.toml": project["version"],
        "Python __version__": runpy.run_path(str(root / "src/openclaw_iphone/__init__.py"))["__version__"],
    }
    for source, value in versions.items():
        if value != version:
            raise ValueError(f"{source} is {value}, expected {version}.")
    return version


if __name__ == "__main__":
    try:
        version = check_release(ROOT, sys.argv[1] if len(sys.argv) > 1 else os.environ.get("RELEASE_TAG"))
    except ValueError as exc:
        sys.exit(str(exc))
    print(f"Release versions match: {version}")
