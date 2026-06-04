#!/usr/bin/env sh
set -eu

if [ -z "${DEVICE_ID:-}" ]; then
  echo "Set DEVICE_ID to a device UDID, serial number, or device name." >&2
  exit 2
fi

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
echo "Launching $BUNDLE_ID..."

xcrun devicectl device process launch \
  --device "$DEVICE_ID" \
  "$BUNDLE_ID"

