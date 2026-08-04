"""Reusable command-run sequencing for platform-owned workers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from pathlib import Path


async def execute_logged_command(
    run_id: int,
    command: Sequence[str],
    *,
    add_log: Callable[[int, str], None],
    stream_command: Callable[[Sequence[str], Callable[[str], None]], int],
) -> bool:
    add_log(run_id, "$ " + " ".join(command) + "\n")
    try:
        return await asyncio.to_thread(stream_command, command, lambda line: add_log(run_id, line)) == 0
    except Exception as error:
        add_log(run_id, "Runner error: " + str(error) + "\n")
        return False


async def run_commands(
    run_id: int,
    commands: Sequence[tuple[Sequence[str], object]],
    *,
    result_handler: Callable[[object, str, bool], None] | None,
    temporary_paths: Sequence[Path],
    add_log: Callable[[int, str], None],
    stream_command: Callable[[Sequence[str], Callable[[str], None]], int],
    finish_run: Callable[[int, str], None],
) -> None:
    succeeded = True
    try:
        for command, metadata in commands:
            output_lines: list[str] = []
            add_log(run_id, "$ " + " ".join(command) + "\n")
            try:
                def record_line(line: str):
                    output_lines.append(line)
                    add_log(run_id, line)

                status = await asyncio.to_thread(stream_command, command, record_line)
                output = "".join(output_lines)
            except Exception as error:
                output = "Runner error: " + str(error) + "\n"
                add_log(run_id, output)
                status = 1
            if result_handler:
                result_handler(metadata, output, status == 0)
            if status:
                succeeded = False
                break
    finally:
        for path in temporary_paths:
            Path(path).unlink(missing_ok=True)
    finish_run(run_id, "succeeded" if succeeded else "failed")


class RunLifecycleService:
    """Execute a streamed run and finalize its controller-side effects.

    Domain repositories and enrollment/filebeat callbacks are injected so the
    platform owns run sequencing without importing feature modules.  The
    compatibility application can continue to patch the same command and
    callback seams while new workers use this public service directly.
    """

    def __init__(
        self,
        *,
        db_factory: Callable,
        stream_command: Callable[[Sequence[str], Callable[[str], None]], int],
        add_log: Callable[[int, str], None],
        append_log_in_connection: Callable,
        context_and_log: Callable,
        finish_run: Callable,
        workload_repository,
        host_repository,
        identity_repository,
        open_config: Callable[[str], dict],
        seal_config: Callable[[str], str],
        unique_node_name: Callable,
        enrollment_hostname: Callable,
        write_event: Callable,
        launch_filebeat: Callable[[int, str], int],
        http_exception_type: type[Exception] | None = None,
    ):
        self._db = db_factory
        self._stream = stream_command
        self._add_log = add_log
        self._append_log = append_log_in_connection
        self._context_and_log = context_and_log
        self._finish = finish_run
        self._workloads = workload_repository
        self._hosts = host_repository
        self._identity = identity_repository
        self._open_config = open_config
        self._seal_config = seal_config
        self._unique_name = unique_node_name
        self._hostname = enrollment_hostname
        self._write_event = write_event
        self._launch_filebeat = launch_filebeat
        self._http_exception_type = http_exception_type or Exception

    async def execute(self, run_id: int, command: Sequence[str], temporary_paths: Sequence[Path] = ()) -> None:
        self._add_log(run_id, "$ " + " ".join(command) + "\n")
        try:
            returncode = await asyncio.to_thread(self._stream, command, lambda line: self._add_log(run_id, line))
            status = "succeeded" if returncode == 0 else "failed"
        except Exception as error:
            self._add_log(run_id, "Runner error: " + str(error) + "\n")
            status = "failed"
        finally:
            for path in temporary_paths:
                Path(path).unlink(missing_ok=True)
        await self.finalize(run_id, status)

    async def finalize(self, run_id: int, status: str) -> None:
        filebeat_cluster_id = None
        with self._db() as connection:
            context, run_log = self._context_and_log(connection, run_id)
            workloads = self._workloads.from_connection(connection)
            hosts = self._hosts.from_connection(connection)
            identity = self._identity.from_connection(connection)
            if status == "succeeded" and context.get("purge_assignment_id"):
                workloads.delete_assignment_in_connection(connection, context["purge_assignment_id"])
            if status == "failed" and context.get("rollback_assignment_id"):
                workloads.restore_config_in_connection(
                    connection,
                    context["rollback_assignment_id"],
                    self._seal_config(json.dumps(context["previous_config"])),
                )
                self._append_log(
                    connection,
                    run_id,
                    "Controller configuration restored after failed resource reconciliation\n",
                )
            if status == "succeeded" and context.get("enrollment_node_id"):
                node_id = context["enrollment_node_id"]
                key_id = context.get("enrollment_key_id", "")
                if context.get("enrollment_install_key") and key_id:
                    if identity.state_for_key_in_connection(connection, key_id) == "candidate":
                        hosts.mark_candidate_key_installed_in_connection(
                            connection, node_id, key_id, bool(context.get("enrollment_enabled"))
                        )
                        self._write_event(
                            connection,
                            context.get("enrollment_username", "system"),
                            "controller_ssh_candidate_installed",
                            item_id=str(node_id),
                            detail=key_id,
                        )
                    else:
                        hosts.mark_controller_key_installed_in_connection(
                            connection, node_id, key_id, bool(context.get("enrollment_enabled"))
                        )
                        self._write_event(
                            connection,
                            context.get("enrollment_username", "system"),
                            "controller_ssh_key_installed",
                            item_id=str(node_id),
                            detail=key_id,
                        )
                elif context.get("enrollment_existing_key"):
                    hosts.mark_legacy_enrollment_in_connection(
                        connection, node_id, bool(context.get("enrollment_enabled"))
                    )
                else:
                    hosts.mark_enrollment_pending_in_connection(connection, node_id)
                if context.get("enrollment_auto_name"):
                    discovered_name = self._unique_name(
                        connection, self._hostname(run_log), node_id
                    )
                    if discovered_name:
                        hosts.rename_in_connection(connection, node_id, discovered_name)
                        self._write_event(
                            connection,
                            context.get("enrollment_username", "system"),
                            "host_inventory_name_discovered",
                            item_id=str(node_id),
                            detail=discovered_name,
                        )
                    else:
                        self._append_log(
                            connection,
                            run_id,
                            "Remote hostname was unavailable; keeping the temporary inventory name.\n",
                        )
            if status == "succeeded" and context.get("delete_node_after_revoke"):
                hosts.delete_in_connection(connection, context["delete_node_after_revoke"])
            if status == "succeeded" and context.get("filebeat_reconcile_cluster_id"):
                filebeat_cluster_id = int(context["filebeat_reconcile_cluster_id"])
            self._finish(connection, run_id, status)
        if filebeat_cluster_id:
            try:
                companion_run_id = self._launch_filebeat(filebeat_cluster_id, "system")
                self._add_log(run_id, f"Scheduled Filebeat reconciliation run #{companion_run_id}.\n")
            except self._http_exception_type as error:
                detail = getattr(error, "detail", str(error))
                self._add_log(run_id, f"Filebeat reconciliation was not scheduled: {detail}\n")
