from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess


DEFAULT_XCODE_PATHS = (
    "/Applications/Xcode.app/Contents/Developer",
    "/Applications/Xcode-beta.app/Contents/Developer",
)


def resolve_developer_dir(explicit: str | None = None) -> str | None:
    """Return a Developer dir that contains devicectl, without changing global xcode-select."""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("DEVELOPER_DIR"):
        candidates.append(os.environ["DEVELOPER_DIR"])

    selected = selected_developer_dir()
    if selected:
        candidates.append(selected)

    candidates.extend(DEFAULT_XCODE_PATHS)

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if Path(candidate, "usr/bin/devicectl").exists():
            return candidate
    return None


def selected_developer_dir() -> str | None:
    xcode_select = shutil.which("xcode-select")
    if not xcode_select:
        return None
    proc = subprocess.run(
        [xcode_select, "-p"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None

