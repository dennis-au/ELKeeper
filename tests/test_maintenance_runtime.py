from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr, ValidationError

from app.maintenance_executor import (
    ExecutorUnitResult,
    HostExecutorManifest,
    HostExecutorResult,
    PathExistsCheck,
    executor_instance_unit,
    executor_paths,
    sign_executor_manifest,
)
from app.maintenance_post_return import (
    CleanupProof,
    CleanupStatus,
    ExecutorCleanupTarget,
    NodeIdentityExpectation,
    ServiceBudgetExpectation,
)
from app.maintenance_reboot import ExecutorDiscoveryState, InvocationAmbiguous, ReconnectObservation, SshDisconnectObservation
from app.maintenance_runtime import (
    CaVerifiedElasticsearchClientPool,
    ControllerManagedHostRuntime,
    ElasticsearchPostReturnAdapter,
    ElasticsearchRuntimeConnection,
    ExecutionOutcome,
    ExecutionReceipt,
    MaintenanceRuntimeFlags,
    ManagedFileObservation,
    PlaybookExecutionRequest,
    RebootRequestReceipt,
    RemoteOutcomeUnknown,
    RuntimeIdentityError,
    RuntimeMutationDisabled,
)


NOW = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)
PLAN_ID = "a" * 32
OPERATION_ID = "b" * 32
BOOT_BEFORE = "00000000-1111-2222-3333-444444444444"
BOOT_AFTER = "55555555-6666-7777-8888-999999999999"
UNIT = "ecp-alpha-hot-1.service"


def signed_manifest(key: Ed25519PrivateKey, **overrides):
    paths = executor_paths(OPERATION_ID)
    values = {
        "operation_id": OPERATION_ID,
        "plan_id": PLAN_ID,
        "node_id": 1,
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=30),
        "pre_reboot_boot_id": BOOT_BEFORE,
        "required_units": (UNIT,),
        "checks": (),
        "checkpoint_path": str(paths.checkpoint),
        "result_path": str(paths.result),
    }
    values.update(overrides)
    return sign_executor_manifest(HostExecutorManifest(**values), key)


def executor_result(envelope, **overrides):
    values = {
        "operation_id": OPERATION_ID,
        "plan_id": PLAN_ID,
        "manifest_hash": envelope.signature.payload_sha256,
        "state": "complete",
        "reason_code": "completed",
        "pre_reboot_boot_id": BOOT_BEFORE,
        "observed_boot_id": BOOT_AFTER,
        "units": (ExecutorUnitResult(unit=UNIT, active=True),),
        "checks": (),
        "started_at": NOW + timedelta(seconds=1),
        "completed_at": NOW + timedelta(minutes=1),
    }
    values.update(overrides)
    return HostExecutorResult(**values)


def file_observation(path, content=None, *, exists=True, mode=0o600, owner_uid=0, regular=True, symlink=False):
    return ManagedFileObservation(
        path=str(path),
        exists=exists,
        regular=regular,
        symlink=symlink,
        owner_uid=owner_uid if exists else None,
        mode=mode if exists else None,
        content=content if exists else None,
    )


class FakeControllerIO:
    def __init__(self):
        self.playbook_requests = []
        self.reboot_requests = []
        self.files = {}
        self.cleanup_calls = []
        self.playbook_receipt = ExecutionReceipt(
            outcome=ExecutionOutcome.SUCCEEDED,
            invocation_id="ansible-1",
            observed_at=NOW,
        )
        self.reboot_receipt = RebootRequestReceipt(
            operation_id=OPERATION_ID,
            invocation_id="reboot-1",
            outcome=ExecutionOutcome.SUCCEEDED,
            observed_at=NOW,
        )

    async def run_playbook(self, request):
        self.playbook_requests.append(request)
        return self.playbook_receipt

    async def request_reboot(self, *, node_id, operation_id):
        self.reboot_requests.append((node_id, operation_id))
        if isinstance(self.reboot_receipt, Exception):
            raise self.reboot_receipt
        return self.reboot_receipt

    async def wait_for_disconnect(self, *, node_id, invocation_id):
        return SshDisconnectObservation(disconnected=True, observed_at=NOW)

    async def wait_for_reconnect(self, *, node_id):
        return ReconnectObservation(connected=True, boot_id=BOOT_AFTER, observed_at=NOW)

    async def wait_for_ssh(self, *, node_id, timeout_seconds):
        return True

    async def read_boot_id(self, *, node_id):
        return BOOT_AFTER

    async def observe_file(self, *, node_id, path, maximum_bytes):
        return self.files.get(path, file_observation(path, exists=False))

    async def podman_socket_ready(self, *, node_id):
        return True

    async def quadlet_generator_ready(self, *, node_id):
        return True

    async def generated_units(self, *, node_id, units):
        return frozenset((*units, "ecp-unrequested.service"))

    async def unit_states(self, *, node_id, units):
        return {**{unit: True for unit in units}, "ecp-unrequested.service": True}

    async def endpoint_ready(self, *, node_id, endpoint_ref):
        return endpoint_ref == "elastic-api"

    async def cleanup_executor(self, *, node_id, unit, paths):
        self.cleanup_calls.append((node_id, unit, paths))
        return CleanupProof(status=CleanupStatus.VERIFIED)


class ControllerManagedHostRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.from_private_bytes(b"k" * 32)
        self.io = FakeControllerIO()

    def runtime(self, **flags):
        return ControllerManagedHostRuntime(
            node_id=1,
            io=self.io,
            executor_public_key=self.key.public_key(),
            flags=MaintenanceRuntimeFlags(**flags),
            clock=lambda: NOW,
        )

    async def test_mutations_are_disabled_by_default_without_transport_calls(self):
        runtime = self.runtime()
        with self.assertRaises(RuntimeMutationDisabled):
            await runtime.stage(signed_manifest(self.key))
        with self.assertRaises(RuntimeMutationDisabled):
            await runtime.invoke_reboot(node_id=1, operation_id=OPERATION_ID)
        target = ExecutorCleanupTarget(
            operation_id=OPERATION_ID,
            unit=executor_instance_unit(OPERATION_ID),
            paths=(str(executor_paths(OPERATION_ID).result),),
        )
        with self.assertRaises(RuntimeMutationDisabled):
            await runtime.cleanup_executor(target)
        self.assertEqual(self.io.playbook_requests, [])
        self.assertEqual(self.io.reboot_requests, [])
        self.assertEqual(self.io.cleanup_calls, [])

    async def test_signed_manifest_staging_uses_bounded_redacted_playbook_request(self):
        runtime = self.runtime(executor_staging_enabled=True)
        envelope = signed_manifest(self.key)
        receipt = await runtime.stage(envelope)
        self.assertTrue(receipt.acknowledged)
        self.assertEqual(receipt.manifest_hash, envelope.signature.payload_sha256)
        request = self.io.playbook_requests[0]
        self.assertEqual(request.node_id, 1)
        self.assertEqual(request.playbook, "host-maintenance-executor-stage.yml")
        self.assertEqual(request.variables["maintenance_executor_operation_id"], OPERATION_ID)
        self.assertEqual(
            json.loads(request.variables["maintenance_executor_manifest_json"])["manifest"]["plan_id"],
            PLAN_ID,
        )
        checkpoint = json.loads(request.variables["maintenance_executor_checkpoint_json"])
        self.assertEqual(checkpoint["state"], "staged")
        self.assertNotIn("password", json.dumps(request.variables).lower())

    async def test_staging_rejects_wrong_signing_key_or_node_before_io(self):
        runtime = self.runtime(executor_staging_enabled=True)
        wrong_key = Ed25519PrivateKey.from_private_bytes(b"w" * 32)
        with self.assertRaises(ValueError):
            await runtime.stage(signed_manifest(wrong_key))
        with self.assertRaises(RuntimeIdentityError):
            await runtime.stage(signed_manifest(self.key, node_id=2))
        self.assertEqual(self.io.playbook_requests, [])

    async def test_reboot_requires_identity_matched_ack_and_preserves_ambiguity(self):
        runtime = self.runtime(reboot_enabled=True)
        receipt = await runtime.invoke_reboot(node_id=1, operation_id=OPERATION_ID)
        self.assertTrue(receipt.acknowledged)
        self.io.reboot_receipt = RebootRequestReceipt(
            operation_id="c" * 32,
            invocation_id="wrong",
            outcome=ExecutionOutcome.SUCCEEDED,
            observed_at=NOW,
        )
        with self.assertRaises(RuntimeIdentityError):
            await runtime.invoke_reboot(node_id=1, operation_id=OPERATION_ID)
        self.io.reboot_receipt = RemoteOutcomeUnknown("disconnect")
        with self.assertRaises(InvocationAmbiguous):
            await runtime.invoke_reboot(node_id=1, operation_id=OPERATION_ID)

    async def test_executor_discovery_validates_secure_manifest_checkpoint_and_result(self):
        runtime = self.runtime()
        envelope = signed_manifest(self.key)
        paths = executor_paths(OPERATION_ID)
        self.io.files[str(paths.manifest)] = file_observation(
            paths.manifest,
            envelope.model_dump_json().encode(),
        )
        checkpoint = {
            "schema_version": 1,
            "operation_id": OPERATION_ID,
            "state": "boot_transition_verified",
            "manifest_hash": envelope.signature.payload_sha256,
            "observed_at": NOW.isoformat(),
        }
        self.io.files[str(paths.checkpoint)] = file_observation(
            paths.checkpoint,
            json.dumps(checkpoint).encode(),
        )
        running = await runtime.discover(operation_id=OPERATION_ID)
        self.assertEqual(running.state, ExecutorDiscoveryState.RUNNING)

        result = executor_result(envelope)
        self.io.files[str(paths.result)] = file_observation(paths.result, result.model_dump_json().encode())
        complete = await runtime.discover(operation_id=OPERATION_ID)
        self.assertEqual(complete.state, ExecutorDiscoveryState.COMPLETE)
        self.assertEqual(complete.result, result)
        self.assertEqual(await runtime.import_result(OPERATION_ID), result)

        changed = result.model_copy(update={"manifest_hash": "f" * 64})
        self.io.files[str(paths.result)] = file_observation(paths.result, changed.model_dump_json().encode())
        recovery = await runtime.discover(operation_id=OPERATION_ID)
        self.assertEqual(recovery.state, ExecutorDiscoveryState.RECOVERY_REQUIRED)

    async def test_executor_discovery_rejects_insecure_files_and_filters_host_evidence(self):
        runtime = self.runtime()
        paths = executor_paths(OPERATION_ID)
        envelope = signed_manifest(self.key)
        self.io.files[str(paths.manifest)] = file_observation(
            paths.manifest,
            envelope.model_dump_json().encode(),
            mode=0o644,
        )
        discovery = await runtime.discover(operation_id=OPERATION_ID)
        self.assertEqual(discovery.state, ExecutorDiscoveryState.RECOVERY_REQUIRED)
        self.assertEqual(await runtime.generated_units(1, (UNIT,)), frozenset({UNIT}))
        self.assertEqual(await runtime.unit_states(1, (UNIT,)), {UNIT: True})
        with self.assertRaises(RuntimeIdentityError):
            await runtime.read_boot_id(2)

    async def test_executor_result_must_cover_every_signed_manifest_check(self):
        runtime = self.runtime()
        envelope = signed_manifest(
            self.key,
            checks=(
                PathExistsCheck(
                    check_id="podman-socket",
                    path="/run/podman/podman.sock",
                ),
            ),
        )
        paths = executor_paths(OPERATION_ID)
        self.io.files[str(paths.manifest)] = file_observation(
            paths.manifest,
            envelope.model_dump_json().encode(),
        )
        incomplete = executor_result(envelope)
        self.io.files[str(paths.result)] = file_observation(
            paths.result,
            incomplete.model_dump_json().encode(),
        )
        discovery = await runtime.discover(operation_id=OPERATION_ID)
        self.assertEqual(discovery.state, ExecutorDiscoveryState.RECOVERY_REQUIRED)

    async def test_cleanup_passes_only_exact_operation_owned_targets(self):
        runtime = self.runtime(cleanup_enabled=True)
        paths = executor_paths(OPERATION_ID)
        target = ExecutorCleanupTarget(
            operation_id=OPERATION_ID,
            unit=executor_instance_unit(OPERATION_ID),
            paths=(str(paths.result), str(paths.manifest)),
        )
        proof = await runtime.cleanup_executor(target)
        self.assertEqual(proof.status, CleanupStatus.VERIFIED)
        self.assertEqual(self.io.cleanup_calls[0][0], 1)
        self.assertEqual(self.io.cleanup_calls[0][1], executor_instance_unit(OPERATION_ID))
        self.assertEqual(self.io.cleanup_calls[0][2], tuple(sorted(target.paths)))


class MaintenanceRuntimeModelTests(unittest.TestCase):
    def test_playbook_requests_reject_sensitive_variable_names(self):
        with self.assertRaises(ValidationError):
            PlaybookExecutionRequest(
                node_id=1,
                playbook="host-reboot.yml",
                variables={"api_token": "not-allowed"},
            )

    def test_managed_file_observation_requires_secure_regular_artifact(self):
        observation = file_observation("/var/lib/elastic-control/maintenance/x", b"{}", owner_uid=1)
        with self.assertRaises(RuntimeIdentityError):
            observation.require_secure_regular(maximum_bytes=100)


class FakeRuntimeElasticsearchClient:
    def __init__(self):
        self.closed = False

    async def root_info(self):
        return {"cluster_uuid": "cluster_uuid_123"}

    async def nodes_info(self, node_id=None):
        return {"nodes": {node_id: {"name": "alpha-hot-1", "version": "9.1.0"}}}

    async def recovery(self, *, active_only=False):
        return {"index-a": {"shards": []}}

    async def health(self):
        return {
            "status": "green",
            "initializing_shards": 0,
            "relocating_shards": 0,
            "unassigned_primary_shards": 0,
        }

    async def aclose(self):
        self.closed = True


class FakeConnectionResolver:
    def __init__(self, ca_path):
        self.ca_path = ca_path

    def resolve(self, cluster_id):
        return ElasticsearchRuntimeConnection(
            endpoint="https://127.0.0.1:9200",
            ca_path=self.ca_path,
            api_key=SecretStr("redacted-api-key"),
        )


class FakeAvailability:
    async def available(self, expectation):
        return 2


class ElasticsearchRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_pool_builds_validated_ca_https_clients_and_closes_them(self):
        captured = []
        client = FakeRuntimeElasticsearchClient()

        def builder(config, credential, transport, capability):
            captured.append((config, credential, transport, capability))
            return client

        with tempfile.TemporaryDirectory() as directory:
            ca_path = str(Path(directory) / "ca.crt")
            Path(ca_path).write_text("test fixture")
            pool = CaVerifiedElasticsearchClientPool(FakeConnectionResolver(ca_path), builder=builder)
            self.assertIs(pool.get(1), client)
            self.assertIs(pool.get(1), client)
            self.assertEqual(len(captured), 1)
            config, credential, _, _ = captured[0]
            self.assertEqual(config.endpoint, "https://127.0.0.1:9200")
            self.assertEqual(config.ca_path, ca_path)
            self.assertEqual(credential.authorization_header(), "ApiKey redacted-api-key")
            self.assertNotIn("redacted-api-key", repr(credential))
            await pool.aclose()
            self.assertTrue(client.closed)

    async def test_post_return_adapter_collects_identity_recovery_health_and_budget(self):
        client = FakeRuntimeElasticsearchClient()

        def builder(config, credential, transport, capability):
            return client

        with tempfile.TemporaryDirectory() as directory:
            ca_path = str(Path(directory) / "ca.crt")
            Path(ca_path).write_text("fixture")
            pool = CaVerifiedElasticsearchClientPool(FakeConnectionResolver(ca_path), builder=builder)
            adapter = ElasticsearchPostReturnAdapter(pool, FakeAvailability())
            expectation = NodeIdentityExpectation(
                cluster_id=1,
                assignment_id=10,
                persistent_node_id="node-persistent-id",
                node_name="alpha-hot-1",
                version="9.1.0",
                cluster_uuid="cluster_uuid_123",
            )
            identity = await adapter.node_identity(expectation)
            self.assertEqual(identity.persistent_node_id, "node-persistent-id")
            self.assertEqual(identity.cluster_uuid, "cluster_uuid_123")
            self.assertTrue((await adapter.shard_recovery(1)).complete)
            self.assertEqual(await adapter.cluster_health(1), "green")
            budget = await adapter.service_budget(
                ServiceBudgetExpectation(cluster_id=1, role="kibana", minimum_available=1)
            )
            self.assertEqual(budget.available, 2)

    def test_connection_material_rejects_non_https_through_client_pool(self):
        class InsecureResolver:
            def resolve(self, cluster_id):
                return ElasticsearchRuntimeConnection(
                    endpoint="http://127.0.0.1:9200",
                    ca_path="/tmp/ca.crt",
                    api_key=SecretStr("key"),
                )

        pool = CaVerifiedElasticsearchClientPool(InsecureResolver(), builder=lambda *args: None)
        with self.assertRaises(ValidationError):
            pool.get(1)


if __name__ == "__main__":
    unittest.main()
