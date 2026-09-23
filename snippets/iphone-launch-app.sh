#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
. "$SCRIPT_DIR/iphone-lib.sh"
REPO_DIR="$(resolve_openclaw_repo_dir "$SCRIPT_DIR")"

if [ -z "${BUNDLE_ID:-}" ]; then
  echo "Set BUNDLE_ID to the app bundle identifier to launch." >&2
  exit 2
fi

cd "$REPO_DIR"
export PYTHONPATH="$REPO_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
if [ -n "${DEVICE_ID:-}" ]; then
  exec python3 -m openclaw_iphone apps launch --device "$DEVICE_ID" "$BUNDLE_ID"
fi
exec python3 -m openclaw_iphone apps launch "$BUNDLE_ID"
