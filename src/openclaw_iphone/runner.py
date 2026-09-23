from __future__ import annotations

from dataclasses import dataclass
import os
import math
import subprocess
import time

from .errors import CommandFailed
from .execution import Budget, Metrics


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


class Runner:
    def __init__(self, *, env: dict[str, str] | None = None, timeout: int = 30) -> None:
        self.env = dict(env or {})
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Command timeout must be finite and positive.")
        self.timeout = timeout
        self.budget: Budget | None = None
        self.metrics = Metrics()

    def run(self, command: list[str], *, timeout: int | None = None) -> CommandResult:
        seconds = timeout or self.timeout
        if self.budget is not None:
            seconds = min(seconds, self.budget.remaining())
        # Only fixed executable/subcommand names enter telemetry, never argv values.
        operation = "devicectl" if command[:2] == ["xcrun", "devicectl"] else "subprocess"
        phase = devicectl_phase(command) if operation == "devicectl" else None
        with self.metrics.measure(operation):
            if phase:
                with self.metrics.measure(phase):
                    return self._run(command, timeout=seconds, phase=phase)
            return self._run(command, timeout=seconds, phase=operation if operation == "devicectl" else None)

    def _run(self, command: list[str], *, timeout: float, phase: str | None = None) -> CommandResult:
        env = os.environ.copy()
        env.update(self.env)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                timeout=timeout or self.timeout,
                check=False,
                umask=0o077,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandFailed(
                (f"{phase} timed out after {time.monotonic() - started:.3f}s "
                 f"(limit {timeout or self.timeout:g}s).") if phase else
                f"Command timed out after {timeout or self.timeout}s: {format_command(command)}",
                command=command,
                stdout=decode_output(exc.stdout),
                stderr=decode_output(exc.stderr),
                timed_out=True,
            ) from exc
        except OSError as exc:
            raise CommandFailed(f"Could not start {command[0]}: {exc}", command=command) from exc

        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
            raise CommandFailed(
                f"Command failed: {format_command(command)}\n{detail}",
                command=command,
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        return CommandResult(command, proc.returncode, proc.stdout, proc.stderr)


def devicectl_phase(command: list[str]) -> str | None:
    """Fixed labels only; never include device selectors, output paths, or app names."""
    parts = command[2:5]
    if parts[:2] == ["list", "devices"]:
        return "devicectl list devices"
    if parts[:2] == ["device", "info"] and len(parts) == 3:
        return {
            "details": "devicectl device details",
            "lockState": "devicectl device lock state",
            "apps": "devicectl device apps",
        }.get(parts[2])
    if parts == ["device", "process", "launch"]:
        return "devicectl process launch"
    return None


def decode_output(value: str | bytes | None) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""


def format_command(command: list[str]) -> str:
    return " ".join(shell_quote(part) for part in command)


def shell_quote(value: str) -> str:
    if not value:
        return "''"
    safe = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_+-=.,/:@%")
    if all(char in safe for char in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"
