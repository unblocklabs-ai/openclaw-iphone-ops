"""One foreground CLI workflow per host, including watchdog recovery."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
from typing import Iterator

from .errors import OpenClawIPhoneError


@contextmanager
def control_lock(path: Path | None = None) -> Iterator[None]:
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
            raise OpenClawIPhoneError("Another iPhone workflow is active; no device actions performed.") from exc
        yield
    finally:
        os.close(fd)
