"""Typed command construction and execution contracts for orchestration.

Route handlers may request a playbook or module operation through these
builders, but they do not assemble executable command arrays themselves.
Execution remains in the platform run worker during the incremental refactor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class CommandSpec:
    """A command plus its temporary artifact paths."""

    argv: tuple[str, ...]
    temporary_paths: tuple[Path, ...] = ()

    @classmethod
    def from_argv(cls, argv: Sequence[str], temporary_paths: Sequence[Path | str] = ()) -> "CommandSpec":
        if not argv:
            raise ValueError("Orchestration command must not be empty")
        return cls(tuple(str(value) for value in argv), tuple(Path(path) for path in temporary_paths))


class ExecutionStatus(StrEnum):
    """Explicit outcomes for remote operations."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ExecutionReceipt:
    """Redacted result of a command at the orchestration boundary."""

    status: ExecutionStatus
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status is ExecutionStatus.SUCCEEDED


class OrchestrationGateway(Protocol):
    """Public provider-neutral gateway used by route/domain modules."""

    def execute(self, command: CommandSpec, *, timeout: float | None = None) -> ExecutionReceipt: ...

    def cleanup(self, paths: Sequence[Path]) -> tuple[Path, ...]: ...


def ansible_module(
    inventory: Path | str,
    target: str,
    module: str,
    args: str,
    private_key: Path | str,
) -> list[str]:
    return [
        "ansible",
        target,
        "-i",
        str(inventory),
        "-m",
        module,
        "-a",
        args,
        "-o",
        "--private-key",
        str(private_key),
    ]


def ansible_playbook(
    inventory: Path | str,
    playbook: Path | str,
    limit: str,
    private_key: Path | str,
    extra_vars: Path | str | None = None,
) -> list[str]:
    command = [
        "ansible-playbook",
        "-i",
        str(inventory),
        str(playbook),
        "--limit",
        limit,
        "--private-key",
        str(private_key),
    ]
    if extra_vars is not None:
        command.extend(["--extra-vars", "@" + str(extra_vars)])
    return command


def redacted_command(command: Sequence[str]) -> list[str]:
    """Redact inline secret-looking command arguments before persistence."""

    secret_markers = ("password", "token", "secret", "passphrase", "private_key")
    result: list[str] = []
    redact_next = False
    for value in command:
        if redact_next:
            result.append("[REDACTED]")
            redact_next = False
            continue
        result.append(value)
        normalized = value.lower().replace("-", "_")
        redact_next = any(marker in normalized for marker in secret_markers)
    return result
