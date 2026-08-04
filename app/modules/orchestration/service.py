"""Execution boundary for SSH/Ansible/Podman adapters.

The current application still owns the async worker.  This service provides a
small typed seam so adapters can move without changing endpoint contracts.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Callable, Sequence

from .contracts import CommandSpec, ExecutionReceipt, ExecutionStatus, OrchestrationGateway


class OrchestrationError(RuntimeError):
    """A remote operation failed before a reliable outcome was known."""


def temporary_paths(*paths: Path | str) -> tuple[Path, ...]:
    return tuple(Path(path) for path in paths)


class LocalCommandGateway:
    """Small synchronous adapter for tests and the future worker boundary.

    The existing async worker remains the compatibility implementation.  This
    adapter intentionally captures output and returns a typed receipt so new
    modules do not need to know whether a provider uses subprocess, SSH, or
    Ansible underneath.
    """

    def execute(self, command: CommandSpec, *, timeout: float | None = None) -> ExecutionReceipt:
        try:
            completed = subprocess.run(
                command.argv,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            return ExecutionReceipt(
                status=ExecutionStatus.TIMED_OUT,
                stdout=_output_text(error.stdout),
                stderr=_output_text(error.stderr),
            )
        except OSError as error:
            return ExecutionReceipt(status=ExecutionStatus.FAILED, detail={"error": type(error).__name__})
        return ExecutionReceipt(
            status=ExecutionStatus.SUCCEEDED if completed.returncode == 0 else ExecutionStatus.FAILED,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def cleanup(self, paths: Sequence[Path]) -> tuple[Path, ...]:
        """Remove only explicitly registered temporary artifacts."""

        remaining: list[Path] = []
        for path in paths:
            try:
                if path.is_dir():
                    # Temporary directories must already be empty. The
                    # gateway never recursively removes a caller-supplied
                    # tree or an extra bind mount.
                    path.rmdir()
                else:
                    path.unlink(missing_ok=True)
            except OSError:
                remaining.append(path)
        return tuple(remaining)


def _output_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def command_spec(command: Sequence[str], temporary_paths: Sequence[Path | str] = ()) -> CommandSpec:
    """Convert a legacy argv list to the public gateway contract."""

    return CommandSpec.from_argv(command, temporary_paths)


def invoke(command: Sequence[str], runner: Callable[[Sequence[str]], int]) -> int:
    """Invoke an adapter while keeping the command contract typed."""

    try:
        return runner(command)
    except OSError as error:
        raise OrchestrationError("Orchestration command could not be started") from error
