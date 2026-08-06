from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.modules.maintenance.ansible_runtime import RunBoundMaintenanceAnsibleRunner
from app.modules.maintenance.controller_io import AnsibleInvocation, ControllerNodeRecord
from app.modules.maintenance.runtime import ExecutionOutcome, PlaybookExecutionRequest


NOW = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)


class RunBoundMaintenanceAnsibleRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_only_the_allowlisted_playbook_and_removes_ephemeral_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            variables_dir = root / "variables"
            inventory_path = root / "inventory.yaml"
            inventory_path.write_text("all: {}\n", encoding="ascii")
            private_key = root / "controller.key"
            known_hosts = root / "known_hosts"
            private_key.write_text("private", encoding="ascii")
            known_hosts.write_text("host", encoding="ascii")
            commands = []
            progress = []

            def command_for(invocation, inventory, variables):
                self.assertEqual(invocation.node.node_id, 7)
                self.assertEqual(inventory, inventory_path)
                self.assertEqual(os.stat(variables).st_mode & 0o777, 0o600)
                self.assertEqual(variables.parent, variables_dir)
                commands.append((invocation.request.playbook, inventory, variables))
                return ("ansible-playbook", str(invocation.request.playbook), "--extra-vars", "@" + str(variables))

            def stream(command, on_line):
                self.assertEqual(command[0], "ansible-playbook")
                on_line("password=never-persisted\n")
                return 0

            runner = RunBoundMaintenanceAnsibleRunner(
                run_id=91,
                variables_dir=variables_dir,
                inventory_for_node=lambda node_id: inventory_path,
                command_for_invocation=command_for,
                stream_command=stream,
                append_progress=lambda run_id, message: progress.append((run_id, message)),
                allowed_playbooks=frozenset({"host-maintenance-reboot.yml"}),
                clock=lambda: NOW,
            )
            invocation = AnsibleInvocation(
                request=PlaybookExecutionRequest(
                    node_id=7,
                    playbook="host-maintenance-reboot.yml",
                    variables={"maintenance_host_reboot_enabled": True},
                ),
                node=ControllerNodeRecord(
                    node_id=7,
                    name="node-seven",
                    address="192.0.2.7",
                    ssh_port=22,
                    ssh_user="root",
                ),
                private_key_path=str(private_key),
                known_hosts_path=str(known_hosts),
                host_key_args=("UserKnownHostsFile=" + str(known_hosts), "StrictHostKeyChecking=yes"),
            )

            receipt = await runner.run(invocation)

            self.assertEqual(receipt.outcome, ExecutionOutcome.SUCCEEDED)
            self.assertEqual(len(commands), 1)
            self.assertEqual(progress, [(91, "Running approved host-maintenance Ansible action.\n")])
            self.assertFalse(inventory_path.exists())
            self.assertEqual(list(variables_dir.iterdir()), [])

    async def test_rejects_an_unallowlisted_playbook_before_creating_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = []
            runner = RunBoundMaintenanceAnsibleRunner(
                run_id=91,
                variables_dir=root / "variables",
                inventory_for_node=lambda node_id: calls.append(node_id),
                command_for_invocation=lambda *_args: (),
                stream_command=lambda _command, _line: 0,
                append_progress=lambda _run_id, _message: None,
                allowed_playbooks=frozenset({"host-maintenance-reboot.yml"}),
                clock=lambda: NOW,
            )
            invocation = AnsibleInvocation(
                request=PlaybookExecutionRequest(
                    node_id=7,
                    playbook="host-maintenance-executor-stage.yml",
                    variables={"maintenance_executor_stage_enabled": True},
                ),
                node=ControllerNodeRecord(
                    node_id=7,
                    name="node-seven",
                    address="192.0.2.7",
                    ssh_port=22,
                    ssh_user="root",
                ),
                private_key_path=str(root / "controller.key"),
                known_hosts_path=str(root / "known_hosts"),
                host_key_args=("StrictHostKeyChecking=yes",),
            )

            receipt = await runner.run(invocation)

            self.assertEqual(receipt.outcome, ExecutionOutcome.FAILED)
            self.assertEqual(receipt.error_category, "playbook-not-allowlisted")
            self.assertEqual(calls, [])
            self.assertFalse((root / "variables").exists())


if __name__ == "__main__":
    unittest.main()
