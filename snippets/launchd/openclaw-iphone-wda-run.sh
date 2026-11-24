#!/usr/bin/env sh
set -eu
umask 077

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_FROM_SCRIPT="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
IPHONE_PYTHON="${OPENCLAW_IPHONE_PYTHON:-python3}"

CONFIG_REPO_DIR="$(
  SCRIPT_REPO_DIR="$REPO_FROM_SCRIPT" "$IPHONE_PYTHON" - <<'PY'
from pathlib import Path
import os
import sys

script_repo = Path(os.environ["SCRIPT_REPO_DIR"])
sys.path.insert(0, str(script_repo / "src"))

from openclaw_iphone.config import load_config

config = load_config(cwd=script_repo)
if not config.device:
    raise SystemExit("Unattended service requires OPENCLAW_IPHONE_DEVICE; use the dedicated iPhone UDID.")
value = config.get("OPENCLAW_IPHONE_REPO_DIR")
if value:
    print(value)
PY
)"
REPO_DIR="${CONFIG_REPO_DIR:-$REPO_FROM_SCRIPT}"

set -- \
  "$IPHONE_PYTHON" -m openclaw_iphone wda run \
  --allow-provisioning-updates

export PYTHONPATH="$REPO_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

# Prevent crash-looping logs from growing unbounded (rotate at 20 MiB, keeping 1 backup).
LOG_DIR="$HOME/Library/Logs/openclaw"
for log_file in "$LOG_DIR/iphone-wda-run.log" "$LOG_DIR/iphone-wda-run.err.log"; do
  if [ ! -L "$log_file" ] && [ -f "$log_file" ] && [ ! -L "${log_file}.1" ]; then
    log_size="$(wc -c < "$log_file" 2>/dev/null | tr -d ' ' || echo 0)"
    if [ "${log_size:-0}" -gt 20971520 ]; then
      rm -f "${log_file}.1" 2>/dev/null || true
      cp -f "$log_file" "${log_file}.1" 2>/dev/null || true
      : > "$log_file" 2>/dev/null || true
    fi
  fi
done

cd "$REPO_DIR"

exec "$@"
