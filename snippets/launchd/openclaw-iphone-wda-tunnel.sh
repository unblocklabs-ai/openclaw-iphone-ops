#!/usr/bin/env sh
set -eu

REPO_DIR="${OPENCLAW_IPHONE_REPO_DIR:-/Users/pearlperelel/.openclaw/service-env/iphone-wda}"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONPATH="$REPO_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

cd "$REPO_DIR"

if [ -n "${OPENCLAW_IPHONE_DEVICE:-}" ]; then
  exec python3 -m openclaw_iphone wda tunnel --device "$OPENCLAW_IPHONE_DEVICE"
fi

exec python3 -m openclaw_iphone wda tunnel
