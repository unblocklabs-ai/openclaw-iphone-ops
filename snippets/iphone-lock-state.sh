#!/usr/bin/env sh
set -eu

if [ -z "${DEVICE_ID:-}" ]; then
  echo "Set DEVICE_ID to a device UDID, serial number, or device name." >&2
  exit 2
fi

tmpdir="${TMPDIR:-/tmp}/openclaw-iphone-ops"
mkdir -p "$tmpdir"

lock_json="$tmpdir/lock-state.json"

xcrun devicectl device info lockState \
  --device "$DEVICE_ID" \
  --json-output "$lock_json"

echo "Wrote lock-state JSON: $lock_json"

