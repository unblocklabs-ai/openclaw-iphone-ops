#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
. "$SCRIPT_DIR/iphone-lib.sh"
REPO_DIR="$(resolve_openclaw_repo_dir "$SCRIPT_DIR")"
DEVICE_ID="$(resolve_openclaw_device_id "$REPO_DIR")"

cd "$REPO_DIR"
export PYTHONPATH="$REPO_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
export DEVICE_ID
exec python3 - <<'PY'
import json
import os
from openclaw_iphone.devicectl import DeviceCtl

data, artifact = DeviceCtl().lock_state(os.environ["DEVICE_ID"])
print(json.dumps(data, indent=2))
print(f"evidence: {artifact}")
PY
