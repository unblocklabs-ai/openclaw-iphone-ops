from __future__ import annotations

from pathlib import Path
import os
import tempfile
import time


def evidence_dir(base: str | None = None) -> Path:
    root = Path(base or os.environ.get("OPENCLAW_IPHONE_EVIDENCE_DIR") or tempfile.gettempdir())
    path = root / "openclaw-iphone-ops"
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifact_path(prefix: str, suffix: str = ".json", *, base: str | None = None) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return evidence_dir(base) / f"{stamp}-{prefix}{suffix}"

