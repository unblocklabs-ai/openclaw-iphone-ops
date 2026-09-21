"""Pack, inspect, and install the real npm tarball without touching a phone."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from check_release import ROOT, check_release


def run(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    return subprocess.check_output(args, cwd=cwd, env=env, text=True, timeout=120).strip()


def check_package(output_dir: Path | None = None) -> None:
    version = check_release(ROOT)
    package = json.loads((ROOT / "package.json").read_text())
    expected = {"package.json", "README.md"}
    for pattern in package["files"]:
        expected.update(str(path.relative_to(ROOT)) for path in ROOT.glob(pattern) if path.is_file())

    with tempfile.TemporaryDirectory(prefix="iphone-npm-check-") as directory:
        work = Path(directory)
        [packed] = json.loads(run([
            "npm", "pack", "--json", "--ignore-scripts", "--pack-destination", str(work),
        ], cwd=ROOT))
        paths = {entry["path"] for entry in packed["files"]}
        if paths != expected:
            raise ValueError(f"Unexpected npm contents: missing={expected - paths}, extra={paths - expected}")
        if any(Path(path).suffix in {".pyc", ".png", ".xml", ".log"} or path.startswith("build/") for path in paths):
            raise ValueError("Package contains generated or private evidence.")
        tarball = work / packed["filename"]
        prefix = work / "installed package"
        run([
            "npm", "install", "--global", "--prefix", str(prefix), "--ignore-scripts",
            "--no-audit", "--no-fund", str(tarball),
        ], cwd=work)
        cli = str(prefix / "bin/openclaw-iphone")
        # Run outside the checkout, with a hostile PYTHONPATH, via npm's bin symlink.
        (work / "openclaw_iphone.py").write_text('raise RuntimeError("unbundled import")\n')
        env = dict(os.environ, PYTHONPATH=str(work), OPENCLAW_IPHONE_CONFIG=str(work / "missing.env"))
        if run([cli, "--version"], cwd=work, env=env) != version:
            raise ValueError("Installed CLI version differs from the release.")
        if "Reusable primitives" not in run([cli, "--help"], cwd=work, env=env):
            raise ValueError("Installed CLI help is unavailable.")
        if "--allow-cloud" not in run([cli, "task", "run", "--help"], cwd=work, env=env):
            raise ValueError("Packed task driver is unavailable.")
        if "--include-labels" not in run([cli, "task", "session", "--help"], cwd=work, env=env):
            raise ValueError("Packed planner session is unavailable.")
        if "--include-labels" not in run([cli, "ui", "observe", "--help"], cwd=work, env=env):
            raise ValueError("Packed compact observer is unavailable.")
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
            # Never overwrite a previously tested artifact.
            with tarball.open("rb") as source, (output_dir / tarball.name).open("xb") as target:
                shutil.copyfileobj(source, target)
        print(f"npm {version}: {len(paths)} allowlisted files; isolated packed install passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="Keep the tested tarball in this directory.")
    check_package(parser.parse_args().output_dir)
