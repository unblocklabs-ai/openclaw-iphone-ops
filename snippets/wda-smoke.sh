#!/usr/bin/env sh
set -eu
umask 077

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_DIR="${OPENCLAW_IPHONE_REPO_DIR:-$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)}"

cd "$REPO_DIR"
export PYTHONPATH="$REPO_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
if [ -n "${WDA_URL:-}" ]; then
  export OPENCLAW_IPHONE_WDA_URL="$WDA_URL"
fi
python3 -m openclaw_iphone wda status
python3 -m openclaw_iphone ui source
echo "WDA smoke check completed."
