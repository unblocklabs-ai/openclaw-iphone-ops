"""Exclusive ownership of one physical phone; no task policy or write replay."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import threading
from typing import Iterator

from . import cache
from .control_lock import control_lock
from .devicectl import Device, DeviceCtl
from .errors import DeviceSelectionError, WDASetupError, WDAUnavailable
from .execution import Budget, Metrics
from .wda import WDAClient


class Connection:
    def __init__(self, ctl: DeviceCtl, *, device: str | None = None,
                 seconds: float = 30, lock_path: Path | None = None,
                 read_timeout: float | None = None) -> None:
        Budget.seconds(seconds)
        self.ctl, self.requested = ctl, device
        self.seconds, self.read_timeout, self.lock_path = seconds, read_timeout, lock_path
        self.metrics = Metrics()
        self.device: Device | None = None
        self.wda: WDAClient | None = None
        self.valid = False
        self.cleanup_failed = False
        self.reconnects = 0
        self.owner = None
        self._ownership = ExitStack()
        self._sessions = ExitStack()
        self._entered = False
        self._reconnect = threading.Lock()  # Observation lanes may both find the session gone.

    def __enter__(self) -> Connection:
        if self._entered:
            raise ValueError("Connections are single-use.")
        self._entered = True
        self.owner = self._ownership.enter_context(control_lock(self.lock_path))
        previous = self.ctl.runner.budget, self.ctl.runner.metrics
        self._ownership.callback(self._restore_runner, *previous)
        self.ctl.runner.metrics = self.metrics
        try:
            with self.operation():
                self._connect(self.requested)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _restore_runner(self, budget: Budget | None, metrics: Metrics) -> None:
        self.ctl.runner.budget, self.ctl.runner.metrics = budget, metrics

    @contextmanager
    def operation(self) -> Iterator[None]:
        """A fresh deadline only while handling an operation."""
        budget = Budget.seconds(self.seconds)
        previous = self.ctl.runner.budget
        self.ctl.runner.budget = budget
        if self.wda:
            self.wda.budget = budget
        try:
            yield
        finally:
            self.ctl.runner.budget = previous
            if self.wda:
                self.wda.budget = None

    def _connect(self, selector: str | None) -> None:
        use_cache = (
            self.device is None
            and type(self.ctl) is DeviceCtl
            and os.environ.get("OPENCLAW_IPHONE_DISABLE_CACHE") != "1"
        )
        if use_cache:
            cached = cache.get_cached_wda_endpoint(selector)
            if cached is not None:
                try:
                    probe = WDAClient(url=cached["url"], timeout=min(2, self.ctl.runner.timeout), read_timeout=1.5)
                    probe.budget = self.ctl.runner.budget
                    status = probe.status()
                except (WDAUnavailable, WDASetupError, ValueError):
                    status = None
                if status is not None and status.ready is True:
                    try:
                        wda = WDAClient(url=cached["url"], timeout=self.ctl.runner.timeout, read_timeout=self.read_timeout)
                        wda.budget, wda.metrics = self.ctl.runner.budget, self.metrics
                        self._sessions.enter_context(wda.session())
                    except (WDAUnavailable, WDASetupError, ValueError):
                        pass
                    else:
                        self.device = Device(
                            name=cached.get("name", ""),
                            identifier=cached["identifier"],
                            state="connected",
                            model=cached.get("model", ""),
                            udid=cached["udid"],
                        )
                        self.wda = wda
                        self.valid = True
                        return
                cache.clear_cached_wda_endpoint(cached.get("udid"))

        device = self.ctl.select_device(selector)
        if not device.udid or self.device is not None and device.udid != self.device.udid:
            raise DeviceSelectionError("Physical device identity unavailable or changed.")
        self.device = device
        url, _ = self.ctl.coredevice_wda_url(device.identifier)
        wda = WDAClient(url=url, timeout=self.ctl.runner.timeout, read_timeout=self.read_timeout)
        wda.budget, wda.metrics = self.ctl.runner.budget, self.metrics
        self.wda = wda
        self._sessions.enter_context(wda.session())
        self.valid = True
        if type(self.ctl) is DeviceCtl and os.environ.get("OPENCLAW_IPHONE_DISABLE_CACHE") != "1":
            cache.set_cached_wda_endpoint(
                udid=device.udid,
                identifier=device.identifier,
                name=device.name,
                model=device.model,
                url=url,
            )

    def require_active(self) -> WDAClient:
        with self._reconnect:
            if not self.valid:
                self.reconnects += 1
                self._close_session()
                if self.device is None:
                    raise WDAUnavailable("No pinned device.")
                self._connect(self.device.udid)
        if self.wda is None:
            raise WDAUnavailable("No active WDA session.")
        return self.wda

    def touch(self, *, request: bool = False) -> None:
        if self.owner:
            try:
                self.owner.touch(request=request)
            except OSError:
                # Advisory metadata must never hide an input acknowledgement.
                pass

    def invalidate(self, wda: WDAClient | None = None) -> None:
        with self._reconnect:
            if wda is None or wda is self.wda:
                self.valid = False

    def _close_session(self) -> None:
        if self.wda is None:
            self._sessions.close()
            return
        wda = self.wda
        previous = wda.budget
        wda.budget = Budget.seconds(2)
        try:
            self._sessions.close()
        finally:
            wda.budget = previous
            self.cleanup_failed |= wda._session.cleanup_failed

    def __exit__(self, *args: object) -> None:
        self.valid = False
        try:
            self._close_session()
        finally:
            self._ownership.close()
