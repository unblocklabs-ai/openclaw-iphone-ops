#!/usr/bin/env sh
set -eu

tmpdir="${TMPDIR:-/tmp}/openclaw-iphone-ops"
mkdir -p "$tmpdir"

devices_json="$tmpdir/devices.json"

echo "Checking CoreDevice visibility..."
xcrun devicectl list devices --json-output "$devices_json" >/dev/null
echo "Wrote device list JSON: $devices_json"

echo
echo "Human-readable device list:"
xcrun devicectl list devices

cat <<'EOF'

Next:
  1. Pick the physical iPhone identifier from the device list.
  2. Run:
       DEVICE_ID="<udid-or-device-name>" ./snippets/iphone-lock-state.sh
EOF

