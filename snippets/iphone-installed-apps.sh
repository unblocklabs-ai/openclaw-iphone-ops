#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
. "$SCRIPT_DIR/iphone-lib.sh"
REPO_DIR="$(resolve_openclaw_repo_dir "$SCRIPT_DIR")"
DEVICE_ID="$(resolve_openclaw_device_id "$REPO_DIR")"

cd "$REPO_DIR"
export PYTHONPATH="$REPO_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m openclaw_iphone apps list --device "$DEVICE_ID"
