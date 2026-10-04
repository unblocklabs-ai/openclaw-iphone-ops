"""One foreground CLI workflow per host, including watchdog recovery."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import stat
from typing import Iterator

from .errors import OpenClawIPhoneError


class ControlBusy(OpenClawIPhoneError):
    def __init__(self, owner: dict[str, object]) -> None:
        super().__init__("Another iPhone workflow is active; no device actions performed.")
        self.owner = owner


@dataclass
class ControlOwner:
    fd: int
    started_at: str
    requests: int = 0

    def touch(self, *, request: bool = False) -> None:
        self.requests += int(request)
        raw = json.dumps({"pid": os.getpid(), "started_at": self.started_at,
            "last_activity_at": datetime.now(timezone.utc).isoformat(), "requests": self.requests}).encode()
        os.pwrite(self.fd, raw, 0)
        os.ftruncate(self.fd, len(raw))


def owner_metadata(fd: int) -> dict[str, object]:
    try:
        value = json.loads(os.pread(fd, 1024, 0))
        if (not isinstance(value, dict) or type(value.get("pid")) is not int or value["pid"] <= 0
                or type(value.get("requests")) is not int or value["requests"] < 0):
            return {}
        for key in ("started_at", "last_activity_at"):
            if not isinstance(value.get(key), str) or len(value[key]) > 40:
                return {}
            datetime.fromisoformat(value[key])
        return {key: value[key] for key in ("pid", "started_at", "last_activity_at", "requests")}
    except (ValueError, OSError):
        return {}


@contextmanager
def control_lock(path: Path | None = None) -> Iterator[ControlOwner]:
    path = path or Path.home() / ".openclaw/iphone/control.lock"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_nlink != 1 or not stat.S_ISREG(info.st_mode):
            raise OpenClawIPhoneError("Unsafe iPhone control lock file; inspect its ownership and type.")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ControlBusy(owner_metadata(fd)) from exc
        owner = ControlOwner(fd, datetime.now(timezone.utc).isoformat())
        owner.touch()
        yield owner
    finally:
        os.close(fd)
