"""Publish the tested archive, or verify an identical version on a rerun."""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from urllib.request import urlopen


def integrity(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


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
        archive = tarball.read_bytes()
        if metadata != integrity(archive):
            # Node/zlib versions can produce different gzip bytes for the exact
            # same tar stream. Keep npm's immutable bytes in the GitHub assets,
            # but only after verifying both registry integrity and full content.
            filename = f"{package['name'].split('/')[-1]}-{package['version']}.tgz"
            with urlopen(f"{registry}{package['name']}/-/{filename}", timeout=30) as response:
                published = response.read()
            if integrity(published) != metadata:
                raise ValueError("Published archive failed registry integrity verification.")
            if gzip.decompress(published) != gzip.decompress(archive):
                raise ValueError(f"{spec} already exists with different contents; do not overwrite or retag it.")
            tarball.write_bytes(published)
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
