from __future__ import annotations


class OpenClawIPhoneError(Exception):
    """Base error for expected operational failures."""


class CommandFailed(OpenClawIPhoneError):
    def __init__(
        self,
        message: str,
        *,
        command: list[str],
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
    ) -> None:
        super().__init__(message)
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out


class DeviceSelectionError(OpenClawIPhoneError):
    """Raised when no unambiguous device can be selected."""


class AppNotFound(OpenClawIPhoneError):
    """Raised when an app cannot be found on the selected device."""


class WDAUnavailable(OpenClawIPhoneError):
    """Raised when WebDriverAgent cannot be reached or understood."""

    def __init__(self, message: str, *, category: str = "wda_unavailable", phase: str = "wda") -> None:
        super().__init__(message)
        self.category, self.phase = category, phase


class WDATransportUnavailable(WDAUnavailable):
    """The transport or native session was lost; pinned read recovery is safe."""


class WDAReadUnavailable(WDAUnavailable):
    """One read lane failed, not evidence that the device/session needs recreation."""


class WDAUnsupportedCommand(WDAUnavailable):
    """The server explicitly rejected an unsupported route; fallback is safe."""


class WDAStaleElement(WDAUnavailable):
    """A read used an expired native reference; re-resolving for a read is safe."""


class WDAOutcomeUnknown(WDAUnavailable):
    """A mutating request may have executed. Observe state before any retry."""


class SessionOutputUnavailable(OpenClawIPhoneError):
    """The planner stopped consuming output; ownership must be released."""


class WDASetupError(OpenClawIPhoneError):
    """Raised when WebDriverAgent cannot be built, run, or tunneled."""


def diagnostic(error: BaseException) -> dict[str, object]:
    """Public categories only: never copy an exception message or command values."""
    from .execution import TaskStopped
    from .runner import devicectl_phase
    from .control_lock import ControlBusy
    from .observations import ObservationRejected
    if isinstance(error, ObservationRejected):
        return {"category": "observation_rejected", "phase": "observation"}
    if isinstance(error, ControlBusy):
        return {"category": "control_busy", "phase": "ownership", "owner": error.owner}
    if isinstance(error, WDAUnsupportedCommand):
        return {"category": "unsupported", "phase": "response"}
    if isinstance(error, WDAUnavailable):
        return {"category": error.category, "phase": error.phase}
    if isinstance(error, CommandFailed):
        return {"category": "deadline" if error.timed_out else "command_failed",
                "phase": devicectl_phase(error.command) or "subprocess"}
    if isinstance(error, TaskStopped):
        return {"category": "deadline", "phase": "operation"}
    if isinstance(error, DeviceSelectionError):
        return {"category": "device_selection", "phase": "discovery"}
    if isinstance(error, WDASetupError):
        return {"category": "wda_setup", "phase": "setup"}
    if isinstance(error, KeyboardInterrupt):
        return {"category": "interrupted", "phase": "operation"}
    if isinstance(error, (ValueError, TypeError, OverflowError, UnicodeError, RecursionError)):
        return {"category": "invalid_request", "phase": "validation"}
    if isinstance(error, OSError):
        return {"category": "local_io", "phase": "local"}
    return {"category": "unavailable", "phase": "observation"}
