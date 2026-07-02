#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_DIR="${OPENCLAW_IPHONE_REPO_DIR:-$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)}"

if [ -z "${WDA_URL:-}" ]; then
  resolver_output="$(
    PYTHONPATH="$REPO_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
      python3 -m openclaw_iphone wda url
  )"
  WDA_URL="$(printf '%s\n' "$resolver_output" | awk -F': ' '/^url: / {print $2}')"
  if [ -z "$WDA_URL" ]; then
    printf '%s\n' "$resolver_output" >&2
    echo "Could not resolve WDA URL from CoreDevice." >&2
    exit 1
  fi
fi

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
