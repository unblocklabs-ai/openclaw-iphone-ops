from __future__ import annotations

from pathlib import Path
import os
import tempfile
import time


def evidence_dir(base: str | None = None) -> Path:
    """Allocate a private run directory, never reuse a predictable shared path."""
    root = Path(base or os.environ.get("OPENCLAW_IPHONE_EVIDENCE_DIR") or tempfile.gettempdir())
    root = root.expanduser().absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="openclaw-iphone-ops-", dir=root))


def validate_prefix(prefix: str) -> str:
    if not prefix or prefix in {".", ".."} or any(char in prefix for char in ("/", "\\", "\x00")):
        raise ValueError("Artifact prefix must be a single non-empty filename component.")
    return prefix


def write_private(path: Path, data: str | bytes, *, encoding: str = "utf-8") -> None:
    """Create owner-only evidence; refuse existing files, hardlinks and symlinks."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data.encode(encoding) if isinstance(data, str) else data)


def artifact_path(prefix: str, suffix: str = ".json", *, base: str | None = None) -> Path:
    validate_prefix(prefix)
    validate_prefix(suffix)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return evidence_dir(base) / f"{stamp}-{prefix}{suffix}"
