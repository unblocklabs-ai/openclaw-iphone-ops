"""Single-threaded task ownership; no daemon, automatic unlock, or action replay."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from types import TracebackType

from .control_lock import control_lock
from .devicectl import Device, DeviceCtl
from .errors import DeviceSelectionError, WDAUnavailable
from .execution import Budget, Metrics, TaskStopped
from .wda import WDAClient


class TaskConnection:
    """One physical device, deadline, lock and session for a synchronous task.

    Low-level WDA calls remain available to trusted Python callers. Callers must
    invalidate on transport errors; the higher-level executor does this itself.
    Recovery is explicit, read-failure-only, and never resumes an unknown write.
    """

    def __init__(self, ctl: DeviceCtl, *, device: str | None = None,
                 seconds: float = 60, lock_path: Path | None = None) -> None:
        self.ctl = ctl
        self.requested = device
        self.budget = Budget.seconds(seconds)
        self.metrics = Metrics()
        self.lock_path = lock_path
        self.device: Device | None = None
        self.wda: WDAClient | None = None
        self.generation = 0
        self.valid = False
        self.uncertain = False
        self.cleanup_failed = False
        self.recoveries = 0
        self._ownership = ExitStack()
        self._sessions = ExitStack()
        self._entered = False

    def __enter__(self) -> TaskConnection:
        if self._entered:
            raise TaskStopped("Task connections are single-use.")
        self._entered = True
        self.budget.remaining()
        self._ownership.enter_context(control_lock(self.lock_path))
        # Do not leave the caller's DeviceCtl carrying an expired task budget.
        runner = self.ctl.runner
        previous = runner.budget, runner.metrics
        self._ownership.callback(self._restore_runner, *previous)
        runner.budget, runner.metrics = self.budget, self.metrics
        try:
            self._connect(self.requested)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _restore_runner(self, budget: Budget | None, metrics: Metrics) -> None:
        self.ctl.runner.budget, self.ctl.runner.metrics = budget, metrics

    def _connect(self, selector: str | None) -> None:
        self.budget.remaining()
        device = self.ctl.select_device(selector)
        if not device.udid or self.device is not None and device.udid != self.device.udid:
            raise DeviceSelectionError("A task requires the same explicit physical UDID; device identity unavailable or changed.")
        self.ctl.require_unlocked(device.identifier)
        url, _ = self.ctl.coredevice_wda_url(device.identifier)
        wda = WDAClient(url=url, timeout=self.ctl.runner.timeout)
        wda.budget, wda.metrics = self.budget, self.metrics
        if not wda.is_ready():
            raise WDAUnavailable("WDA is not ready; no task action dispatched.")
        wda.require_unlocked()
        self.device, self.wda = device, wda
        self._sessions.enter_context(wda.session())
        self.generation += 1
        self.valid = True

    def require_active(self) -> WDAClient:
        self.budget.remaining()
        if not self.valid or self.uncertain or self.wda is None:
            raise TaskStopped("Task connection invalid; do not replay actions.")
        return self.wda

    def invalidate(self, *, uncertain: bool = False) -> None:
        self.valid = False
        self.uncertain |= uncertain
        self.generation += 1

    def recover_read(self) -> None:
        """One explicit reacquisition after a failed read, on the original UDID."""
        self.budget.remaining()
        if self.valid or self.uncertain or self.recoveries or self.device is None:
            raise TaskStopped("Recovery requires a failed read, known outcome and unused recovery allowance.")
        self.recoveries += 1
        self._close_session()
        self._connect(self.device.udid)

    def _close_session(self) -> None:
        self._sessions.close()
        if self.wda is not None:
            self.cleanup_failed |= self.wda._session.cleanup_failed

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None, tb: TracebackType | None) -> None:
        self.valid = False
        try:
            self._close_session()
        finally:
            self._ownership.close()
