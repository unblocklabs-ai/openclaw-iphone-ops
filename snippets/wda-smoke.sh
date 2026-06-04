#!/usr/bin/env sh
set -eu

WDA_URL="${WDA_URL:-http://127.0.0.1:8100}"

tmpdir="${TMPDIR:-/tmp}/openclaw-iphone-ops"
mkdir -p "$tmpdir"

status_json="$tmpdir/wda-status.json"
source_xml="$tmpdir/wda-source.xml"

echo "Checking WDA status at $WDA_URL..."
curl -fsS "$WDA_URL/status" > "$status_json"
echo "Wrote WDA status: $status_json"

echo "Checking WDA source..."
curl -fsS "$WDA_URL/source" > "$source_xml"
echo "Wrote WDA source: $source_xml"

echo "WDA smoke check completed."

