"""Run-bound Ansible adapter for the signed host-maintenance executor."""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .controller_io import AnsibleInvocation
from .runtime import ExecutionOutcome, ExecutionReceipt


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RunBoundMaintenanceAnsibleRunner:
    """Execute a narrowly allowlisted maintenance playbook under one run.

    The adapter deliberately emits only fixed progress messages. Playbook
    variables and raw output never enter the platform run log or SSE stream.
    """

    def __init__(
        self,
        *,
        run_id: int,
        variables_dir: Path,
        inventory_for_node: Callable[[int], Path],
        command_for_invocation: Callable[[AnsibleInvocation, Path, Path], Sequence[str]],
        stream_command: Callable[[Sequence[str], Callable[[str], None]], int],
        append_progress: Callable[[int, str], None],
        allowed_playbooks: frozenset[str],
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if run_id < 1:
            raise ValueError("maintenance Ansible run id is invalid")
        if not allowed_playbooks:
            raise ValueError("maintenance Ansible playbook allowlist is required")
        self.run_id = run_id
        self.variables_dir = variables_dir
        self.inventory_for_node = inventory_for_node
        self.command_for_invocation = command_for_invocation
        self.stream_command = stream_command
        self.append_progress = append_progress
        self.allowed_playbooks = allowed_playbooks
        self.clock = clock

    async def run(self, invocation: AnsibleInvocation) -> ExecutionReceipt:
        request = invocation.request
        if request.playbook not in self.allowed_playbooks:
            return self._receipt(ExecutionOutcome.FAILED, "playbook-not-allowlisted")
        inventory = self.inventory_for_node(invocation.node.node_id)
        variables = self._variables_path()
        try:
            self.variables_dir.mkdir(parents=True, exist_ok=True)
            variables.write_text(yaml.safe_dump(dict(request.variables), sort_keys=True), encoding="utf-8")
            os.chmod(variables, 0o600)
            command = tuple(self.command_for_invocation(invocation, inventory, variables))
            if not command:
                return self._receipt(ExecutionOutcome.FAILED, "ansible-command-invalid")
            self.append_progress(self.run_id, "Running approved host-maintenance Ansible action.\n")
            try:
                returncode = await asyncio.to_thread(self.stream_command, command, lambda _line: None)
            except OSError:
                return self._receipt(ExecutionOutcome.AMBIGUOUS, "ansible-command-ambiguous")
            return self._receipt(
                ExecutionOutcome.SUCCEEDED if returncode == 0 else ExecutionOutcome.FAILED,
                None if returncode == 0 else "ansible-playbook-failed",
            )
        finally:
            variables.unlink(missing_ok=True)
            inventory.unlink(missing_ok=True)

    def _variables_path(self) -> Path:
        return self.variables_dir / f"maintenance-{self.run_id}-{secrets.token_hex(12)}.yaml"

    def _receipt(self, outcome: ExecutionOutcome, error_category: str | None) -> ExecutionReceipt:
        return ExecutionReceipt(
            outcome=outcome,
            invocation_id=f"maintenance-ansible-{self.run_id}-{secrets.token_hex(8)}",
            observed_at=self.clock(),
            error_category=error_category,
        )


__all__ = ["RunBoundMaintenanceAnsibleRunner"]
