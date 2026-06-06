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


class DeviceLocked(OpenClawIPhoneError):
    """Raised when foreground automation is blocked by lock state."""


class WDAUnavailable(OpenClawIPhoneError):
    """Raised when WebDriverAgent cannot be reached or understood."""


class WDASetupError(OpenClawIPhoneError):
    """Raised when WebDriverAgent cannot be built, run, or tunneled."""
