from __future__ import annotations

import base64
import json
import unittest
from datetime import datetime, timezone
from pathlib import PurePosixPath

from app.maintenance_executor import executor_instance_unit, executor_paths
from app.maintenance_post_return import CleanupStatus
from app.maintenance_reboot import ReconnectObservation
from app.maintenance_runtime import (
    ExecutionOutcome,
    ExecutionReceipt,
    MaintenanceRuntimeFlags,
    ManagedFileObservation,
    PlaybookExecutionRequest,
    RebootRequestReceipt,
    RemoteOutcomeUnknown,
    RuntimeMutationDisabled,
)
from app.maintenance_controller_io import (
    AnsibleInvocation,
    ControllerMaintenanceIOAdapter,
    ControllerNodeRecord,
    SSHCommandRequest,
    SSHCommandResult,
    normalize_node,
)


NOW = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)
OPERATION_ID = "b" * 32


class FakeSSH:
    def __init__(self, result: SSHCommandResult | Exception | None = None):
        self.result = result or SSHCommandResult(return_code=0, stdout=b"ok\n")
        self.requests = []

    async def run(self, request):
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeAnsible:
    def __init__(self):
        self.invocations = []

    async def run(self, invocation):
        self.invocations.append(invocation)
        return ExecutionReceipt(
            outcome=ExecutionOutcome.SUCCEEDED,
            invocation_id="ansible-1",
            observed_at=NOW,
        )


class FakeReboot:
    def __init__(self, result=None):
        self.result = result or RebootRequestReceipt(
            operation_id=OPERATION_ID,
            invocation_id="reboot-1",
            outcome=ExecutionOutcome.SUCCEEDED,
            observed_at=NOW,
        )
        self.requests = []

    async def request(self, *, node, operation_id):
        self.requests.append((node, operation_id))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def node_payload(**overrides):
    payload = {
        "id": 1,
        "name": "node-a",
        "address": "192.0.2.10",
        "ssh_port": 22,
        "ssh_user": "root",
    }
    payload.update(overrides)
    return payload


class ControllerMaintenanceIOTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ssh = FakeSSH()
        self.ansible = FakeAnsible()
        self.reboot = FakeReboot()
        self.policy_calls = []

        def policy(node, known_hosts):
            self.policy_calls.append((node, known_hosts))
            return ("-o", f"UserKnownHostsFile={known_hosts}", "-o", "StrictHostKeyChecking=yes")

        self.adapter = ControllerMaintenanceIOAdapter(
            node_resolver=lambda node_id: node_payload(id=node_id),
            active_key_path=lambda: "/run/secrets/controller.key",
            known_hosts_path=lambda ids: "/run/runtime/known-hosts-" + "-".join(map(str, ids)),
            host_key_args=policy,
            ssh_runner=self.ssh,
            ansible_runner=self.ansible,
            reboot_runner=self.reboot,
            endpoint_probes={"elastic-api": self.endpoint_probe},
            flags=MaintenanceRuntimeFlags(
                executor_staging_enabled=True,
                reboot_enabled=True,
                cleanup_enabled=True,
            ),
            clock=lambda: NOW,
        )

    async def endpoint_probe(self, *, node, endpoint_ref):
        return node.address == "192.0.2.10" and endpoint_ref == "elastic-api"

    async def test_literal_ip_and_absolute_path_validation(self):
        with self.assertRaises(ValueError):
            normalize_node(node_payload(address="node.example"))
        with self.assertRaises(ValueError):
            normalize_node(node_payload(address="192.0.2.10", ssh_user="root user"))
        with self.assertRaises(ValueError):
            SSHCommandRequest(
                node=ControllerNodeRecord(
                    node_id=1,
                    name="node-a",
                    address="192.0.2.10",
                    ssh_port=22,
                    ssh_user="root",
                ),
                argv=("true",),
                key_path="relative.key",
                known_hosts_path="/run/known-hosts",
                host_key_args=("StrictHostKeyChecking=yes",),
                timeout_seconds=5,
            )

    async def test_playbook_propagates_active_key_and_strict_host_key_policy_without_secrets(self):
        request = PlaybookExecutionRequest(
            node_id=1,
            playbook="host-maintenance-executor-stage.yml",
            variables={"maintenance_executor_stage_enabled": True, "public_key": "configured"},
        )
        await self.adapter.run_playbook(request)
        invocation = self.ansible.invocations[0]
        self.assertEqual(invocation.private_key_path, "/run/secrets/controller.key")
        self.assertEqual(invocation.known_hosts_path, "/run/runtime/known-hosts-1")
        self.assertIn("StrictHostKeyChecking=yes", invocation.host_key_args)
        self.assertIn("UserKnownHostsFile=/run/runtime/known-hosts-1", invocation.host_key_args)
        self.assertEqual(invocation.redacted()["variables"]["public_key"], "configured")
        self.assertNotIn("password", json.dumps(invocation.redacted()).lower())
        self.assertEqual(self.policy_calls[0][0]["address"], "192.0.2.10")

    async def test_missing_host_key_policy_is_rejected_before_runner(self):
        adapter = ControllerMaintenanceIOAdapter(
            node_resolver=lambda node_id: node_payload(id=node_id),
            active_key_path=lambda: "/run/secrets/controller.key",
            known_hosts_path=lambda ids: "/run/runtime/known-hosts-1",
            host_key_args=lambda node, known_hosts: ("StrictHostKeyChecking=yes",),
            ssh_runner=self.ssh,
            ansible_runner=self.ansible,
            flags=MaintenanceRuntimeFlags(executor_staging_enabled=True),
        )
        with self.assertRaises(ValueError):
            await adapter.run_playbook(
                PlaybookExecutionRequest(
                    node_id=1,
                    playbook="host-maintenance-executor-stage.yml",
                    variables={"enabled": True},
                )
            )
        self.assertEqual(self.ansible.invocations, [])

    async def test_mutations_are_disabled_by_default(self):
        adapter = ControllerMaintenanceIOAdapter(
            node_resolver=lambda node_id: node_payload(id=node_id),
            active_key_path=lambda: "/run/secrets/controller.key",
            known_hosts_path=lambda ids: "/run/runtime/known-hosts-1",
            host_key_args=lambda node, known_hosts: (
                "UserKnownHostsFile=" + known_hosts,
                "StrictHostKeyChecking=yes",
            ),
            ssh_runner=self.ssh,
            ansible_runner=self.ansible,
        )
        request = PlaybookExecutionRequest(
            node_id=1,
            playbook="host-maintenance-executor-stage.yml",
            variables={"enabled": True},
        )
        with self.assertRaises(RuntimeMutationDisabled):
            await adapter.run_playbook(request)
        with self.assertRaises(RuntimeMutationDisabled):
            await adapter.request_reboot(node_id=1, operation_id=OPERATION_ID)
        with self.assertRaises(RuntimeMutationDisabled):
            await adapter.cleanup_executor(
                node_id=1,
                unit=executor_instance_unit(OPERATION_ID),
                paths=(str(executor_paths(OPERATION_ID).result),),
            )
        self.assertEqual(self.ansible.invocations, [])
        self.assertEqual(self.reboot.requests, [])

    async def test_ambiguous_reboot_transport_is_not_converted_to_success(self):
        self.reboot.result = RemoteOutcomeUnknown("lost connection")
        with self.assertRaises(RemoteOutcomeUnknown):
            await self.adapter.request_reboot(node_id=1, operation_id=OPERATION_ID)
        self.assertEqual(self.reboot.requests[0][1], OPERATION_ID)

    async def test_disconnect_reconnect_and_boot_id_behavior(self):
        self.ssh.result = SSHCommandResult(return_code=255, stderr=b"connection closed")
        disconnected = await self.adapter.wait_for_disconnect(node_id=1, invocation_id="reboot-1")
        self.assertTrue(disconnected.disconnected)

        self.ssh.result = SSHCommandResult(
            return_code=0,
            stdout=b"01234567-89ab-cdef-0123-456789abcdef\n",
        )
        reconnected = await self.adapter.wait_for_reconnect(node_id=1)
        self.assertTrue(reconnected.connected)
        self.assertEqual(reconnected.boot_id, "01234567-89ab-cdef-0123-456789abcdef")
        self.assertEqual(await self.adapter.read_boot_id(node_id=1), reconnected.boot_id)

        self.ssh.result = SSHCommandResult(return_code=0, stdout=b"not-a-boot-id\n")
        failed = await self.adapter.wait_for_reconnect(node_id=1)
        self.assertFalse(failed.connected)
        self.assertIsNone(failed.boot_id)

    async def test_secure_file_observer_parses_bounded_base64_payload(self):
        payload = {
            "exists": True,
            "regular": True,
            "symlink": False,
            "owner_uid": 0,
            "mode": 0o600,
            "content": base64.b64encode(b"manifest").decode("ascii"),
        }
        self.ssh.result = SSHCommandResult(return_code=0, stdout=json.dumps(payload).encode())
        observed = await self.adapter.observe_file(
            node_id=1,
            path="/var/lib/elastic-control/maintenance/operations/" + OPERATION_ID + "/manifest.json",
            maximum_bytes=100,
        )
        self.assertEqual(observed.content, b"manifest")
        request = self.ssh.requests[-1]
        self.assertEqual(request.key_path, "/run/secrets/controller.key")
        self.assertEqual(request.host_key_args[-1], "StrictHostKeyChecking=yes")
        self.assertIn("O_NOFOLLOW", request.argv[2])

        with self.assertRaises(ValueError):
            await self.adapter.observe_file(node_id=1, path="/tmp/../etc/shadow", maximum_bytes=100)

    async def test_injected_file_observer_and_endpoint_allowlist(self):
        seen = []

        async def observer(*, node, path, maximum_bytes):
            seen.append((node.node_id, path, maximum_bytes))
            return ManagedFileObservation(
                path=path,
                exists=False,
            )

        self.adapter.remote_file_observer = observer
        path = "/var/lib/elastic-control/maintenance/operations/" + OPERATION_ID + "/result.json"
        observed = await self.adapter.observe_file(node_id=1, path=path, maximum_bytes=100)
        self.assertFalse(observed.exists)
        self.assertEqual(seen, [(1, path, 100)])
        self.assertTrue(await self.adapter.endpoint_ready(node_id=1, endpoint_ref="elastic-api"))
        with self.assertRaises(ValueError):
            await self.adapter.endpoint_ready(node_id=1, endpoint_ref="https://untrusted.example")

    async def test_executor_cleanup_is_operation_owned_and_bounded(self):
        paths = executor_paths(OPERATION_ID)
        proof = await self.adapter.cleanup_executor(
            node_id=1,
            unit=executor_instance_unit(OPERATION_ID),
            paths=(str(paths.result), str(paths.manifest)),
        )
        self.assertEqual(proof.status, CleanupStatus.VERIFIED)
        request = self.ssh.requests[-1]
        self.assertEqual(request.argv[3], executor_instance_unit(OPERATION_ID))
        self.assertIn(str(paths.result), request.argv)
        with self.assertRaises(ValueError):
            await self.adapter.cleanup_executor(
                node_id=1,
                unit=executor_instance_unit(OPERATION_ID),
                paths=("/tmp/unrelated",),
            )


if __name__ == "__main__":
    unittest.main()
