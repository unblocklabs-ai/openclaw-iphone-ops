#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SCRIPT_REPO_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)"
TEMPLATE="$SCRIPT_DIR/com.openclaw.iphone-wda-run.plist.template"
TARGET="$HOME/Library/LaunchAgents/com.openclaw.iphone-wda-run.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/openclaw"

SCRIPT_REPO_DIR="$SCRIPT_REPO_DIR" TEMPLATE="$TEMPLATE" TARGET="$TARGET" python3 - <<'PY'
from pathlib import Path
import os
import plistlib
import sys

script_repo = Path(os.environ["SCRIPT_REPO_DIR"])
sys.path.insert(0, str(script_repo / "src"))

from openclaw_iphone.config import load_config

config = load_config(cwd=script_repo)
repo_dir = config.get("OPENCLAW_IPHONE_REPO_DIR") or str(script_repo)
repo_path = Path(repo_dir).expanduser()
wrapper = repo_path / "snippets/launchd/openclaw-iphone-wda-run.sh"
package = repo_path / "src/openclaw_iphone"
if not package.is_dir():
    raise SystemExit(f"Repo dir does not contain src/openclaw_iphone: {repo_path}")
if not wrapper.is_file():
    raise SystemExit(f"WDA launchd wrapper is missing: {wrapper}")

template = Path(os.environ["TEMPLATE"])
with template.open("rb") as fh:
    data = plistlib.load(fh)

def replace(value):
    if isinstance(value, str):
        return value.replace("__HOME__", str(Path.home())).replace("__REPO_DIR__", str(repo_path))
    if isinstance(value, list):
        return [replace(item) for item in value]
    if isinstance(value, dict):
        return {key: replace(item) for key, item in value.items()}
    return value

rendered = replace(data)
if "__HOME__" in str(rendered) or "__REPO_DIR__" in str(rendered):
    raise SystemExit("Rendered plist still contains unresolved placeholders.")

with Path(os.environ["TARGET"]).open("wb") as fh:
    plistlib.dump(rendered, fh)
PY

plutil -lint "$TARGET"
echo "Installed $TARGET"
echo "Run: launchctl bootout \"gui/$(id -u)\" \"$TARGET\" 2>/dev/null || true"
echo "Then: launchctl bootstrap \"gui/$(id -u)\" \"$TARGET\""
