#!/usr/bin/env sh
set -eu

if [ -z "${DEVICE_ID:-}" ]; then
  echo "Set DEVICE_ID to a device UDID, serial number, or device name." >&2
  exit 2
fi

tmpdir="${TMPDIR:-/tmp}/openclaw-iphone-ops"
mkdir -p "$tmpdir"

apps_json="$tmpdir/installed-apps.json"

xcrun devicectl device info apps \
  --device "$DEVICE_ID" \
  --json-output "$apps_json"

echo "Wrote installed-apps JSON: $apps_json"

