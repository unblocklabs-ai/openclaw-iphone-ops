resolve_openclaw_repo_dir() {
  script_dir="$1"
  if [ -n "${OPENCLAW_IPHONE_REPO_DIR:-}" ]; then
    printf '%s\n' "$OPENCLAW_IPHONE_REPO_DIR"
    return
  fi
  CDPATH= cd -- "$script_dir/.." && pwd
}

resolve_openclaw_device_id() {
  repo_dir="$1"
  if [ -n "${DEVICE_ID:-}" ]; then
    printf '%s\n' "$DEVICE_ID"
    return
  fi

  OPENCLAW_IPHONE_REPO_DIR_FOR_SNIPPET="$repo_dir" \
    PYTHONPATH="$repo_dir/src${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PY'
from pathlib import Path
import os

from openclaw_iphone.config import load_config
from openclaw_iphone.devicectl import DeviceCtl

repo_dir = Path(os.environ["OPENCLAW_IPHONE_REPO_DIR_FOR_SNIPPET"])
selector = load_config(cwd=repo_dir).device
device = DeviceCtl().select_device(selector)
print(device.identifier)
PY
}
