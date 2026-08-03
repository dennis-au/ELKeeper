from __future__ import annotations

import json
import ssl
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
from pydantic import SecretStr, ValidationError

from app.maintenance_elasticsearch import (
    AllocationCleanupTrigger,
    AllocationGuardController,
    AllocationGuardPhase,
    ApiKeyCredential,
    DocumentedRollingBackend,
    ElasticsearchClientConfig,
    ElasticsearchMaintenanceClient,
    ElasticsearchRequestError,
    NodeShutdownApiBackend,
    NodeShutdownCapability,
    NodeShutdownStatus,
    SettingLayerValue,
    ShutdownBackendDisabled,
    TransientAllocationPrecedence,
    capture_allocation_setting,
    select_maintenance_backend,
)
from app.maintenance_models import MaintenanceBackend


NOW = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)
SECRET = "encoded-api-key-material"
SETTING = "cluster.routing.allocation.enable"


def make_client(handler, *, capability=None):
    transport = httpx.MockTransport(handler)
    config = ElasticsearchClientConfig(
        endpoint="https://192.0.2.10:9200",
        ca_path="/etc/elastic-control/example/ca.crt",
        timeout_seconds=4,
    )
    with patch(
        "app.maintenance_elasticsearch.ssl.create_default_context",
        return_value=ssl.create_default_context(),
    ) as create_context:
        client = ElasticsearchMaintenanceClient(
            config,
            ApiKeyCredential(value=SecretStr(SECRET)),
            transport=transport,
            shutdown_capability=capability,
        )
    create_context.assert_called_once_with(cafile=config.ca_path)
    return client


class ElasticsearchMaintenanceClientSecurityTests(unittest.IsolatedAsyncioTestCase):
    def test_config_requires_ca_verified_https_without_embedded_credentials(self):
        ElasticsearchClientConfig(
            endpoint="https://[2001:db8::10]:9200",
            ca_path="/etc/elastic-control/example/ca.crt",
        )
        for endpoint in (
            "http://192.0.2.10:9200",
            "https://user:password@192.0.2.10:9200",
            "https://192.0.2.10:9200/?token=secret",
            "https://192.0.2.10:9200/path",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValidationError):
                ElasticsearchClientConfig(
                    endpoint=endpoint,
                    ca_path="/etc/elastic-control/example/ca.crt",
                )
        with self.assertRaises(ValidationError):
            ElasticsearchClientConfig(
                endpoint="https://192.0.2.10:9200",
                ca_path="relative-ca.crt",
            )

    async def test_credential_is_header_only_and_errors_are_redacted(self):
        requests = []

        def handler(request: httpx.Request):
            requests.append(request)
            return httpx.Response(401, json={"error": SECRET})

        client = make_client(handler)
        with self.assertRaises(ElasticsearchRequestError) as raised:
            await client.health()

        request = requests[0]
        self.assertEqual(request.headers["authorization"], f"ApiKey {SECRET}")
        self.assertNotIn(SECRET, str(request.url))
        self.assertNotIn(SECRET, repr(client))
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertNotIn(SECRET, repr(raised.exception))


class ElasticsearchMaintenanceClientContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_read_contracts_use_json_apis_only(self):
        seen = []

        def handler(request: httpx.Request):
            seen.append((request.method, request.url.path, dict(request.url.params)))
            if request.url.path == "/_cluster/health":
                return httpx.Response(200, json={"status": "green"})
            if request.url.path == "/_cluster/settings":
                return httpx.Response(200, json={"persistent": {}, "transient": {}})
            if request.url.path == "/_nodes/node-1":
                return httpx.Response(200, json={"nodes": {"node-1": {"name": "es-1"}}})
            if request.url.path == "/_recovery":
                return httpx.Response(200, json={"index-a": {"shards": []}})
            if request.url.path == "/_cluster/allocation/explain":
                self.assertEqual(json.loads(request.content), {"index": "index-a", "shard": 0, "primary": True})
                return httpx.Response(200, json={"can_allocate": "yes"})
            if request.url.path == "/_cluster/pending_tasks":
                return httpx.Response(200, json={"tasks": []})
            return httpx.Response(404)

        client = make_client(handler)
        self.assertEqual((await client.health())["status"], "green")
        self.assertEqual((await client.settings())["persistent"], {})
        self.assertEqual((await client.nodes_info("node-1"))["nodes"]["node-1"]["name"], "es-1")
        self.assertIn("index-a", await client.recovery(active_only=True))
        self.assertEqual(
            (await client.allocation_explain(index="index-a", shard=0, primary=True))["can_allocate"],
            "yes",
        )
        self.assertEqual((await client.pending_tasks())["tasks"], [])

        self.assertEqual(
            seen,
            [
                ("GET", "/_cluster/health", {"level": "cluster"}),
                ("GET", "/_cluster/settings", {"flat_settings": "true", "include_defaults": "false"}),
                ("GET", "/_nodes/node-1", {}),
                ("GET", "/_recovery", {"active_only": "true", "detailed": "true"}),
                ("POST", "/_cluster/allocation/explain", {}),
                ("GET", "/_cluster/pending_tasks", {}),
            ],
        )

    async def test_non_object_and_malformed_json_responses_fail_closed(self):
        responses = iter(
            (
                httpx.Response(200, json=[{"status": "green"}]),
                httpx.Response(200, content=b"not-json"),
            )
        )

        client = make_client(lambda request: next(responses))
        for category in ("invalid-response-shape", "invalid-json"):
            with self.subTest(category=category), self.assertRaises(ElasticsearchRequestError) as raised:
                await client.health()
            self.assertEqual(raised.exception.category, category)


class AllocationSettingCaptureTests(unittest.TestCase):
    def test_capture_preserves_layers_absence_and_transient_precedence(self):
        capture = capture_allocation_setting(
            {
                "persistent": {SETTING: "none"},
                "transient": {SETTING: "all"},
            },
            captured_at=NOW,
        )
        self.assertEqual(capture.persistent, SettingLayerValue(present=True, value="none"))
        self.assertEqual(capture.transient, SettingLayerValue(present=True, value="all"))
        self.assertEqual(capture.effective_value, "all")
        self.assertEqual(
            capture.restoration_payload(),
            {
                "persistent": {SETTING: "none"},
                "transient": {SETTING: "all"},
            },
        )

        absent = capture_allocation_setting(
            {"persistent": {}, "transient": {}},
            captured_at=NOW,
        )
        self.assertFalse(absent.persistent.present)
        self.assertFalse(absent.transient.present)
        self.assertEqual(absent.effective_value, "all")
        self.assertEqual(
            absent.restoration_payload(),
            {
                "persistent": {SETTING: None},
                "transient": {SETTING: None},
            },
        )

    def test_capture_accepts_nested_settings_without_text_parsing(self):
        capture = capture_allocation_setting(
            {
                "persistent": {"cluster": {"routing": {"allocation": {"enable": "primaries"}}}},
                "transient": {},
            },
            captured_at=NOW,
        )
        self.assertEqual(capture.persistent.value, "primaries")
        self.assertEqual(capture.effective_value, "primaries")


class AllocationGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsafe_transient_precedence_blocks_before_mutation(self):
        methods = []

        def handler(request: httpx.Request):
            methods.append(request.method)
            return httpx.Response(
                200,
                json={"persistent": {SETTING: "all"}, "transient": {SETTING: "none"}},
            )

        guard = AllocationGuardController(make_client(handler), clock=lambda: NOW)
        checkpoint = await guard.capture(plan_id="a" * 32, cluster_id=7)
        with self.assertRaises(TransientAllocationPrecedence):
            await guard.activate(checkpoint)
        self.assertEqual(methods, ["GET"])

    async def test_guard_sets_persistent_primaries_and_verifies_effective_value(self):
        state = {"persistent": {}, "transient": {}}
        bodies = []

        def handler(request: httpx.Request):
            if request.method == "PUT":
                body = json.loads(request.content)
                bodies.append(body)
                state["persistent"].update(body.get("persistent", {}))
                state["transient"].update(body.get("transient", {}))
                return httpx.Response(200, json={"acknowledged": True})
            return httpx.Response(200, json=state)

        guard = AllocationGuardController(make_client(handler), clock=lambda: NOW)
        checkpoint = await guard.capture(plan_id="b" * 32, cluster_id=8)
        result = await guard.activate(checkpoint)

        self.assertEqual(result.status, "active")
        self.assertEqual(result.checkpoint.phase, AllocationGuardPhase.ACTIVE)
        self.assertEqual(result.checkpoint.observed.effective_value, "primaries")
        self.assertIsNone(result.cleanup)
        self.assertEqual(bodies, [{"persistent": {SETTING: "primaries"}}])

    async def test_failed_activation_restores_exact_layers_before_returning(self):
        original = {"persistent": {SETTING: "none"}, "transient": {}}
        state = {"persistent": dict(original["persistent"]), "transient": {}}
        put_bodies = []
        settings_reads = 0

        def handler(request: httpx.Request):
            nonlocal settings_reads
            if request.method == "PUT":
                body = json.loads(request.content)
                put_bodies.append(body)
                for layer in ("persistent", "transient"):
                    for key, value in body.get(layer, {}).items():
                        if value is None:
                            state[layer].pop(key, None)
                        else:
                            state[layer][key] = value
                return httpx.Response(200, json={"acknowledged": True})
            settings_reads += 1
            if settings_reads == 2:
                return httpx.Response(200, json={"persistent": {SETTING: "all"}, "transient": {}})
            return httpx.Response(200, json=state)

        guard = AllocationGuardController(make_client(handler), clock=lambda: NOW)
        checkpoint = await guard.capture(plan_id="c" * 32, cluster_id=9)
        result = await guard.activate(checkpoint)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_category, "allocation-guard-verification-failed")
        self.assertIsNotNone(result.cleanup)
        self.assertEqual(result.cleanup.status, "restored")
        self.assertTrue(result.cleanup.verified)
        self.assertEqual(result.cleanup.checkpoint.phase, AllocationGuardPhase.RESTORED)
        self.assertEqual(
            put_bodies,
            [
                {"persistent": {SETTING: "primaries"}},
                {
                    "persistent": {SETTING: "none"},
                    "transient": {SETTING: None},
                },
            ],
        )
        self.assertEqual(state, original)

    async def test_cleanup_is_identical_for_success_failure_cancel_and_recovery(self):
        for trigger in AllocationCleanupTrigger:
            with self.subTest(trigger=trigger):
                state = {"persistent": {}, "transient": {}}
                put_bodies = []

                def handler(request: httpx.Request):
                    if request.method == "PUT":
                        body = json.loads(request.content)
                        put_bodies.append(body)
                        for layer in ("persistent", "transient"):
                            for key, value in body.get(layer, {}).items():
                                if value is None:
                                    state[layer].pop(key, None)
                                else:
                                    state[layer][key] = value
                        return httpx.Response(200, json={"acknowledged": True})
                    return httpx.Response(200, json=state)

                guard = AllocationGuardController(make_client(handler), clock=lambda: NOW)
                captured = await guard.capture(plan_id="d" * 32, cluster_id=10)
                active = await guard.activate(captured)
                cleanup = await guard.restore(active.checkpoint, trigger=trigger)

                self.assertEqual(cleanup.status, "restored")
                self.assertEqual(cleanup.trigger, trigger)
                self.assertTrue(cleanup.verified)
                self.assertEqual(
                    put_bodies[-1],
                    {
                        "persistent": {SETTING: None},
                        "transient": {SETTING: None},
                    },
                )

    async def test_ambiguous_restore_write_is_accepted_only_after_exact_readback(self):
        reads = 0

        def handler(request: httpx.Request):
            nonlocal reads
            if request.method == "PUT":
                raise httpx.ReadTimeout("response lost after write", request=request)
            reads += 1
            return httpx.Response(
                200,
                json={"persistent": {SETTING: "all"}, "transient": {}},
            )

        guard = AllocationGuardController(make_client(handler), clock=lambda: NOW)
        captured = await guard.capture(plan_id="f" * 32, cluster_id=12)
        cleanup = await guard.restore(captured, trigger=AllocationCleanupTrigger.RECOVERY)

        self.assertEqual(reads, 2)
        self.assertEqual(cleanup.status, "restored")
        self.assertTrue(cleanup.verified)
        self.assertEqual(cleanup.error_category, "allocation-restoration-timeout")

    async def test_unverified_restoration_requires_recovery_and_preserves_capture(self):
        calls = 0

        def handler(request: httpx.Request):
            nonlocal calls
            calls += 1
            if request.method == "PUT":
                return httpx.Response(200, json={"acknowledged": True})
            if calls == 1:
                return httpx.Response(200, json={"persistent": {SETTING: "all"}, "transient": {}})
            return httpx.Response(200, json={"persistent": {SETTING: "primaries"}, "transient": {}})

        guard = AllocationGuardController(make_client(handler), clock=lambda: NOW)
        captured = await guard.capture(plan_id="e" * 32, cluster_id=11)
        cleanup = await guard.restore(captured, trigger=AllocationCleanupTrigger.RECOVERY)

        self.assertEqual(cleanup.status, "recovery_required")
        self.assertFalse(cleanup.verified)
        self.assertEqual(cleanup.error_category, "allocation-restoration-verification-failed")
        self.assertEqual(cleanup.checkpoint.phase, AllocationGuardPhase.RECOVERY_REQUIRED)
        self.assertEqual(cleanup.checkpoint.captured, captured.captured)


class MaintenanceBackendTests(unittest.IsolatedAsyncioTestCase):
    def test_documented_rolling_is_the_default_backend(self):
        client = make_client(lambda request: httpx.Response(200, json={}))
        selected = select_maintenance_backend(client)
        self.assertIsInstance(selected, DocumentedRollingBackend)
        self.assertEqual(selected.kind, MaintenanceBackend.DOCUMENTED_ROLLING)
        self.assertFalse(selected.uses_shutdown_api)

    async def test_shutdown_backend_is_disabled_and_exact_version_gated_by_default(self):
        client = make_client(lambda request: httpx.Response(200, json={}))
        backend = NodeShutdownApiBackend(client)
        self.assertEqual(backend.kind, MaintenanceBackend.NODE_SHUTDOWN_API)
        self.assertFalse(backend.capability.enabled)
        with self.assertRaises(ShutdownBackendDisabled):
            await backend.prepare_restart(
                node_id="node-1",
                node_version="8.19.0",
                reason="maintenance-plan-abc",
                allocation_delay_seconds=120,
            )

    async def test_explicit_shutdown_capability_registers_reads_and_cleans_record(self):
        seen = []
        deleted = False
        capability = NodeShutdownCapability(enabled=True, tested_versions=("8.19.0",))

        def handler(request: httpx.Request):
            nonlocal deleted
            seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
            if request.method == "PUT":
                return httpx.Response(200, json={"acknowledged": True})
            if request.method == "GET":
                if deleted:
                    return httpx.Response(200, json={"nodes": []})
                return httpx.Response(
                    200,
                    json={"nodes": [{"node_id": "node-1", "status": "IN_PROGRESS"}]},
                )
            deleted = True
            return httpx.Response(200, json={"acknowledged": True})

        client = make_client(handler, capability=capability)
        backend = NodeShutdownApiBackend(client, capability=capability)
        prepared = await backend.prepare_restart(
            node_id="node-1",
            node_version="8.19.0",
            reason="maintenance-plan-abc",
            allocation_delay_seconds=120,
        )
        status = await backend.status(node_id="node-1", node_version="8.19.0")
        cleaned = await backend.cleanup_restart(node_id="node-1", node_version="8.19.0")

        self.assertTrue(prepared)
        self.assertEqual(status.status, NodeShutdownStatus.IN_PROGRESS)
        self.assertTrue(cleaned)
        self.assertEqual(
            seen,
            [
                (
                    "PUT",
                    "/_nodes/node-1/shutdown",
                    {"type": "restart", "reason": "maintenance-plan-abc", "allocation_delay": "120s"},
                ),
                ("GET", "/_nodes/node-1/shutdown", None),
                ("DELETE", "/_nodes/node-1/shutdown", None),
                ("GET", "/_nodes/node-1/shutdown", None),
            ],
        )

    async def test_shutdown_cleanup_fails_closed_when_record_remains(self):
        capability = NodeShutdownCapability(enabled=True, tested_versions=("8.19.0",))

        def handler(request: httpx.Request):
            if request.method == "DELETE":
                return httpx.Response(200, json={"acknowledged": True})
            return httpx.Response(200, json={"nodes": [{"node_id": "node-1", "status": "COMPLETE"}]})

        client = make_client(handler, capability=capability)
        backend = NodeShutdownApiBackend(client, capability=capability)
        self.assertFalse(await backend.cleanup_restart(node_id="node-1", node_version="8.19.0"))

    def test_shutdown_backend_rejects_capability_drift_from_client(self):
        client = make_client(lambda request: httpx.Response(200, json={}))
        with self.assertRaises(ValueError):
            NodeShutdownApiBackend(
                client,
                capability=NodeShutdownCapability(enabled=True, tested_versions=("8.19.0",)),
            )


if __name__ == "__main__":
    unittest.main()
