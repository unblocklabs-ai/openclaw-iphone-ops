#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
. "$SCRIPT_DIR/iphone-lib.sh"
REPO_DIR="$(resolve_openclaw_repo_dir "$SCRIPT_DIR")"
DEVICE_ID="$(resolve_openclaw_device_id "$REPO_DIR")"

if [ -z "${BUNDLE_ID:-}" ]; then
  echo "Set BUNDLE_ID to the app bundle identifier to launch." >&2
  exit 2
fi

tmpdir="${TMPDIR:-/tmp}/openclaw-iphone-ops"
mkdir -p "$tmpdir"

lock_json="$tmpdir/lock-state-before-launch.json"
xcrun devicectl device info lockState \
  --device "$DEVICE_ID" \
  --json-output "$lock_json" >/dev/null

echo "Checked lock state: $lock_json"
echo "Launching $BUNDLE_ID on $DEVICE_ID..."

xcrun devicectl device process launch \
  --device "$DEVICE_ID" \
  "$BUNDLE_ID"
