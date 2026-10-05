#!/usr/bin/env sh
set -eu
umask 077

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SCRIPT_REPO_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)"
INTERVAL="${OPENCLAW_IPHONE_WATCHDOG_INTERVAL:-120}"
TEMPLATE="$SCRIPT_DIR/com.openclaw.iphone-watchdog.plist.template"
TARGET="$HOME/Library/LaunchAgents/com.openclaw.iphone-watchdog.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/openclaw"

SCRIPT_REPO_DIR="$SCRIPT_REPO_DIR" TEMPLATE="$TEMPLATE" TARGET="$TARGET" INTERVAL="$INTERVAL" "${OPENCLAW_IPHONE_PYTHON:-python3}" - <<'PY'
from pathlib import Path
import os
import sys

script_repo = Path(os.environ["SCRIPT_REPO_DIR"])
sys.path.insert(0, str(script_repo / "src"))

from openclaw_iphone.launchd import install_launchagent

try:
    interval = int(os.environ["INTERVAL"])
except ValueError:
    print("OPENCLAW_IPHONE_WATCHDOG_INTERVAL must be a positive integer.", file=sys.stderr)
    raise SystemExit(2)
if interval < 1:
    print("OPENCLAW_IPHONE_WATCHDOG_INTERVAL must be a positive integer.", file=sys.stderr)
    raise SystemExit(2)
try:
    install_launchagent(script_repo, Path(os.environ["TEMPLATE"]), Path(os.environ["TARGET"]),
                        "openclaw-iphone-watchdog.sh", interval=interval)
except OverflowError:
    print("OPENCLAW_IPHONE_WATCHDOG_INTERVAL does not fit a plist integer.", file=sys.stderr)
    raise SystemExit(2)
PY

plutil -lint "$TARGET"
echo "Installed $TARGET"
echo "Run: launchctl bootout \"gui/$(id -u)\" \"$TARGET\" 2>/dev/null || true"
echo "Then: launchctl bootstrap \"gui/$(id -u)\" \"$TARGET\""
