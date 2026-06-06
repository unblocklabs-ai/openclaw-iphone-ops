from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess

from .errors import CommandFailed


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


class Runner:
    def __init__(self, *, env: dict[str, str] | None = None, timeout: int = 30) -> None:
        self.env = dict(env or {})
        self.timeout = timeout

    def run(self, command: list[str], *, timeout: int | None = None) -> CommandResult:
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
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandFailed(
                f"Command timed out after {timeout or self.timeout}s: {format_command(command)}",
                command=command,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                timed_out=True,
            ) from exc

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


def format_command(command: list[str]) -> str:
    return " ".join(shell_quote(part) for part in command)


def shell_quote(value: str) -> str:
    if not value:
        return "''"
    safe = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_+-=.,/:@%")
    if all(char in safe for char in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"

