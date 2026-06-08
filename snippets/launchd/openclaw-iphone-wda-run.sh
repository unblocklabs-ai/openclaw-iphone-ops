#!/usr/bin/env sh
set -eu

REPO_DIR="${OPENCLAW_IPHONE_REPO_DIR:-/Users/pearlperelel/.openclaw/service-env/iphone-wda}"
WDA_PATH="${OPENCLAW_IPHONE_WDA_PATH:-/Users/pearlperelel/.openclaw/iphone/WebDriverAgent}"
RUNNER_BUNDLE_ID="${OPENCLAW_IPHONE_RUNNER_BUNDLE_ID:-ai.unblocklabs.openclaw.WebDriverAgentRunner}"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export OPENCLAW_IPHONE_WDA_PATH="$WDA_PATH"
export PYTHONPATH="$REPO_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

cd "$REPO_DIR"

if [ -n "${OPENCLAW_IPHONE_DEVICE:-}" ]; then
  exec python3 -m openclaw_iphone wda run \
    --device "$OPENCLAW_IPHONE_DEVICE" \
    --runner-bundle-id "$RUNNER_BUNDLE_ID" \
    --allow-provisioning-updates
fi

exec python3 -m openclaw_iphone wda run \
  --runner-bundle-id "$RUNNER_BUNDLE_ID" \
  --allow-provisioning-updates
