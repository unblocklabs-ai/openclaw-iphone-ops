#!/usr/bin/env sh
set -eu
umask 077

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SCRIPT_REPO_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)"
TEMPLATE="$SCRIPT_DIR/com.openclaw.iphone-wda-run.plist.template"
TARGET="$HOME/Library/LaunchAgents/com.openclaw.iphone-wda-run.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/openclaw"

SCRIPT_REPO_DIR="$SCRIPT_REPO_DIR" TEMPLATE="$TEMPLATE" TARGET="$TARGET" "${OPENCLAW_IPHONE_PYTHON:-python3}" - <<'PY'
from pathlib import Path
import os
import sys

script_repo = Path(os.environ["SCRIPT_REPO_DIR"])
sys.path.insert(0, str(script_repo / "src"))

from openclaw_iphone.launchd import install_launchagent
install_launchagent(script_repo, Path(os.environ["TEMPLATE"]), Path(os.environ["TARGET"]),
                    "openclaw-iphone-wda-run.sh")
PY

plutil -lint "$TARGET"
echo "Installed $TARGET"
echo "Run: launchctl bootout \"gui/$(id -u)\" \"$TARGET\" 2>/dev/null || true"
echo "Then: launchctl bootstrap \"gui/$(id -u)\" \"$TARGET\""
