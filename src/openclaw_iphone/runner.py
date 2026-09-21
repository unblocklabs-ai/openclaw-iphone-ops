from __future__ import annotations

from dataclasses import dataclass
import os
import math
import subprocess

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
        with self.metrics.measure(operation):
            return self._run(command, timeout=seconds)

    def _run(self, command: list[str], *, timeout: float) -> CommandResult:
        env = os.environ.copy()
        env.update(self.env)
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
