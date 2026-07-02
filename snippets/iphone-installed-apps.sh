#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
. "$SCRIPT_DIR/iphone-lib.sh"
REPO_DIR="$(resolve_openclaw_repo_dir "$SCRIPT_DIR")"
DEVICE_ID="$(resolve_openclaw_device_id "$REPO_DIR")"

tmpdir="${TMPDIR:-/tmp}/openclaw-iphone-ops"
mkdir -p "$tmpdir"

apps_json="$tmpdir/installed-apps.json"

xcrun devicectl device info apps \
  --device "$DEVICE_ID" \
  --json-output "$apps_json"

echo "Device: $DEVICE_ID"
echo "Wrote installed-apps JSON: $apps_json"
