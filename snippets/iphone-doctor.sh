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
  1. Set OPENCLAW_IPHONE_DEVICE in ~/.openclaw/iphone/config.env if more than one device is connected.
  2. Run:
       ./snippets/iphone-lock-state.sh
EOF
