"""Apps whose accessibility tree is too slow to read in full, remembered per host.

X's home timeline took 21-38 s per full read (and sometimes closed the app); capped at 22 levels it reads in about
0.2 s and still lists the account menu and tabs. Other apps read in 0.1-0.8 s at any depth, and every target in the
navigator's 213 lab cases sits at level 25 or above, so only apps measured slow are capped.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time

DEPTH = 22            # tree levels read in a slow app
DEFAULT_DEPTH = 50    # WDA's own default, restored for every other app
SLOW_SECONDS = 3.0    # a full read slower than this marks the app slow
KEEP_SECONDS = 14 * 24 * 3600  # re-measure after two weeks (an app update may fix it)
# Measured slow on a dedicated iPhone, so no host pays the first full read: X (21-38 s, and it closed twice).
KNOWN = frozenset({"com.atebits.Tweetie2"})


def path() -> Path:
    return Path.home() / ".openclaw/iphone/shallow-apps.json"


def load() -> set[str]:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    now = time.time()
    return set(KNOWN) | ({app for app, seen in data.items() if isinstance(app, str) and isinstance(seen, (int, float))
                         and now - seen < KEEP_SECONDS} if isinstance(data, dict) else set())


def remember(app: str) -> None:
    """Record `app` as slow (owner-only, atomic); a failure to write only costs the next session one slow read."""
    target = path()
    try:
        data = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
        data = data if isinstance(data, dict) else {}
        data[app] = time.time()
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream)
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except (OSError, ValueError):
        pass
