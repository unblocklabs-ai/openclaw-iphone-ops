#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_FROM_SCRIPT="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

CONFIG_REPO_DIR="$(
  SCRIPT_REPO_DIR="$REPO_FROM_SCRIPT" python3 - <<'PY'
from pathlib import Path
import os
import sys

script_repo = Path(os.environ["SCRIPT_REPO_DIR"])
sys.path.insert(0, str(script_repo / "src"))

from openclaw_iphone.config import load_config

value = load_config(cwd=script_repo).get("OPENCLAW_IPHONE_REPO_DIR")
if value:
    print(value)
PY
)"
REPO_DIR="${CONFIG_REPO_DIR:-$REPO_FROM_SCRIPT}"
TIMEOUT="${OPENCLAW_IPHONE_WATCHDOG_TIMEOUT:-20}"

export PYTHONPATH="$REPO_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

cd "$REPO_DIR"

exec python3 -m openclaw_iphone --timeout "$TIMEOUT" watchdog once
