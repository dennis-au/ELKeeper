"""Platform-owned composition for tracked command runs."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Callable

import yaml

from .command_runs import run_commands as execute_command_group


class PlatformRunOperations:
    """Own run launch and command-group composition without importing app.main."""

    def __init__(
        self,
        *,
        db_factory: Callable,
        variables_dir: Path,
        inventory_factory: Callable,
        run_descriptor: type,
        create_run: Callable,
        start_run: Callable,
        set_running_command: Callable,
        finish_run: Callable,
        redacted_command: Callable,
        lifecycle_service: Callable,
        run_execute: Callable,
        add_log: Callable,
        stream_command: Callable,
        schedule: Callable = asyncio.create_task,
    ) -> None:
        self._db = db_factory
        self._variables = variables_dir
        self._inventory = inventory_factory
        self._descriptor = run_descriptor
        self._create_run = create_run
        self._start_run = start_run
        self._set_running_command = set_running_command
        self._finish_run = finish_run
        self._redacted_command = redacted_command
        self._lifecycle = lifecycle_service
        self._run_execute = run_execute
        self._add_log = add_log
        self._stream = stream_command
        self._schedule = schedule

    async def run(self, run_id: int, command, temporary_paths=()):
        return await self._lifecycle().execute(run_id, command, temporary_paths)

    def launch(
        self,
        kind: str,
        target: str,
        factory: Callable,
        variables: dict | None = None,
        context: dict | None = None,
        inventory_nodes=None,
        private_key=None,
        password_bootstrap: bool = False,
        pinned_host_key_only: bool = False,
    ) -> int:
        with self._db() as connection:
            run_id = self._create_run(
                connection,
                self._descriptor(kind, target, context or {}),
            ).run_id
        inventory = self._inventory(
            run_id,
            private_key=private_key,
            node_ids=inventory_nodes,
            password_bootstrap=password_bootstrap,
            pinned_host_key_only=pinned_host_key_only,
        )
        variables_path = None
        if variables is not None:
            variables_path = self._variables / f"run-{run_id}.yaml"
            variables_path.write_text(yaml.safe_dump(variables, sort_keys=True), encoding="utf-8")
            os.chmod(variables_path, 0o600)
        command = factory(inventory, variables_path)
        with self._db() as connection:
            self._set_running_command(connection, run_id, self._redacted_command(command))
        temporary_paths = [inventory]
        if variables_path:
            temporary_paths.append(variables_path)
        self._schedule(self._run_execute(run_id, command, temporary_paths))
        return run_id

    async def run_commands(self, run_id: int, commands, result_handler=None, temporary_paths=()):
        def finish(run_identifier, status):
            with self._db() as connection:
                self._finish_run(connection, run_identifier, status)

        await execute_command_group(
            run_id,
            commands,
            result_handler=result_handler,
            temporary_paths=temporary_paths,
            add_log=self._add_log,
            stream_command=self._stream,
            finish_run=finish,
        )

    def launch_commands(self, kind: str, target: str, factory: Callable, result_handler=None, context=None) -> int:
        with self._db() as connection:
            run_id = self._start_run(
                connection,
                self._descriptor(kind, target, context or {}),
            ).run_id
        inventory = self._inventory(run_id)
        commands = factory(inventory)
        with self._db() as connection:
            self._set_running_command(
                connection,
                run_id,
                [self._redacted_command(command) for command, _metadata in commands],
            )
        self._schedule(self.run_commands(run_id, commands, result_handler, [inventory]))
        return run_id


__all__ = ["PlatformRunOperations"]
