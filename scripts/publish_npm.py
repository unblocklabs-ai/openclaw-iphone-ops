"""Publish the tested archive, or verify an identical version on a rerun."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def publish(tarball: Path) -> None:
    tarball = tarball.resolve()
    root = Path(__file__).resolve().parents[1]
    package = json.loads((root / "package.json").read_text())
    spec = f"{package['name']}@{package['version']}"
    registry = "https://registry.npmjs.org/"
    result = subprocess.run(
        ["npm", "view", spec, "dist.integrity", "--json", "--registry", registry],
        capture_output=True, text=True, timeout=60,
    )
    metadata = json.loads(result.stdout)
    if result.returncode == 0:
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(tarball.read_bytes()).digest()).decode()
        if metadata != integrity:
            raise ValueError(f"{spec} already exists with different contents; do not overwrite or retag it.")
        print(f"{spec} is already published with the identical tarball; no duplicate publish.")
        return
    if metadata.get("error", {}).get("code") != "E404":
        raise ValueError("npm registry lookup failed; resolve authentication/connectivity before publishing.")
    subprocess.run(
        ["npm", "publish", str(tarball), "--access", "public", "--provenance", "--registry", registry],
        check=True, timeout=180,
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python3 scripts/publish_npm.py <tested-package.tgz>")
    publish(Path(sys.argv[1]))
