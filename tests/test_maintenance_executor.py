from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from app.maintenance_executor import (
    EXECUTOR_SCRIPT_PATH,
    EXECUTOR_UNIT_TEMPLATE_PATH,
    ExecutorCheckResult,
    ExecutorSignature,
    ExecutorUnitResult,
    HostExecutorManifest,
    HostExecutorResult,
    LocalHttpsCheck,
    PathExistsCheck,
    SignatureVerificationError,
    SignedHostExecutorManifest,
    UnitActiveCheck,
    canonical_executor_json,
    executor_instance_unit,
    executor_key_id,
    executor_paths,
    executor_public_key_pem,
    require_operation_owned_path,
    sign_executor_manifest,
    sign_executor_result,
    validate_cleanup_paths,
    validate_managed_reference_path,
    validate_managed_unit,
    validate_operation_id,
    verify_executor_manifest,
    verify_executor_result,
)


NOW = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)
OPERATION_ID = "0123456789abcdef0123456789abcdef"
PLAN_ID = "fedcba9876543210fedcba9876543210"
BOOT_ID = "01234567-89ab-cdef-0123-456789abcdef"
ROOT = Path(__file__).resolve().parents[1]


def fixed_private_key(seed_byte: int = 7) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes([seed_byte]) * 32)


def manifest(**overrides) -> HostExecutorManifest:
    paths = executor_paths(OPERATION_ID)
    values = {
        "operation_id": OPERATION_ID,
        "plan_id": PLAN_ID,
        "node_id": 3,
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=2),
        "pre_reboot_boot_id": BOOT_ID,
        "required_units": ("ecp-alpha-hot-1.service",),
        "checks": (
            UnitActiveCheck(check_id="hot-active", unit="ecp-alpha-hot-1.service"),
            PathExistsCheck(check_id="podman-socket", path="/run/podman/podman.sock"),
            LocalHttpsCheck(
                check_id="elastic-https",
                url="https://127.0.0.1:9200/_cluster/health",
                ca_path="/etc/elastic-control/alpha/ca.crt",
                curl_config_path="/etc/elastic-control/alpha/monitoring.curlrc",
            ),
        ),
        "checkpoint_path": str(paths.checkpoint),
        "result_path": str(paths.result),
    }
    values.update(overrides)
    return HostExecutorManifest(**values)


def result(**overrides) -> HostExecutorResult:
    values = {
        "operation_id": OPERATION_ID,
        "plan_id": PLAN_ID,
        "manifest_hash": "a" * 64,
        "state": "complete",
        "reason_code": "completed",
        "pre_reboot_boot_id": BOOT_ID,
        "observed_boot_id": "11111111-2222-3333-4444-555555555555",
        "units": (ExecutorUnitResult(unit="ecp-alpha-hot-1.service", active=True),),
        "checks": (ExecutorCheckResult(check_id="hot-active", passed=True),),
        "started_at": NOW,
        "completed_at": NOW + timedelta(minutes=1),
    }
    values.update(overrides)
    return HostExecutorResult(**values)


class MaintenanceExecutorSigningTests(unittest.TestCase):
    def test_fixed_seed_signature_is_deterministic(self):
        key = fixed_private_key()
        first = sign_executor_manifest(manifest(), key)
        second = sign_executor_manifest(manifest(), key)
        self.assertEqual(first, second)
        payload = canonical_executor_json(first.manifest).encode("utf-8")
        self.assertEqual(first.signature.payload_sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(len(base64.urlsafe_b64decode(first.signature.value + "==")), 64)
        self.assertEqual(first.signature.key_id, executor_key_id(key.public_key()))

    def test_serialized_round_trip_preserves_canonical_payload(self):
        key = fixed_private_key()
        signed = sign_executor_manifest(manifest(), key)
        encoded = signed.model_dump_json()
        decoded = SignedHostExecutorManifest.model_validate_json(encoded)
        self.assertEqual(
            canonical_executor_json(signed.manifest),
            canonical_executor_json(decoded.manifest),
        )
        self.assertEqual(verify_executor_manifest(decoded, key.public_key(), now=NOW), signed.manifest)

    def test_tampering_wrong_key_expiry_and_malformed_signature_fail_closed(self):
        key = fixed_private_key()
        signed = sign_executor_manifest(manifest(), key)
        tampered = SignedHostExecutorManifest(
            manifest=signed.manifest.model_copy(update={"node_id": 4}),
            signature=signed.signature,
        )
        with self.assertRaises(SignatureVerificationError):
            verify_executor_manifest(tampered, key.public_key(), now=NOW)
        with self.assertRaises(SignatureVerificationError):
            verify_executor_manifest(signed, fixed_private_key(8).public_key(), now=NOW)
        with self.assertRaises(SignatureVerificationError):
            verify_executor_manifest(signed, key.public_key(), now=signed.manifest.expires_at)
        malformed = signed.model_copy(update={
            "signature": signed.signature.model_copy(update={"value": "_" * 86}),
        })
        with self.assertRaises(SignatureVerificationError):
            verify_executor_manifest(malformed, key.public_key(), now=NOW)

    def test_public_key_export_never_interprets_raw_public_bytes_as_private(self):
        key = fixed_private_key()
        raw_public = key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        pem = executor_public_key_pem(raw_public)
        loaded = serialization.load_pem_public_key(pem)
        self.assertEqual(executor_key_id(loaded), executor_key_id(key.public_key()))

    def test_result_signature_round_trip_and_tamper_detection(self):
        key = fixed_private_key()
        signed = sign_executor_result(result(), key)
        decoded = type(signed).model_validate_json(signed.model_dump_json())
        self.assertEqual(verify_executor_result(decoded, key.public_key()), signed.result)
        tampered = decoded.model_copy(update={
            "result": decoded.result.model_copy(update={
                "observed_boot_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            }),
        })
        with self.assertRaises(SignatureVerificationError):
            verify_executor_result(tampered, key.public_key())


class MaintenanceExecutorValidationTests(unittest.TestCase):
    def test_operation_unit_and_derived_paths_are_strict(self):
        self.assertEqual(validate_operation_id(OPERATION_ID), OPERATION_ID)
        self.assertEqual(validate_managed_unit("ecp-alpha-hot-1.service"), "ecp-alpha-hot-1.service")
        self.assertEqual(executor_instance_unit(OPERATION_ID), f"ecp-maintenance-resume@{OPERATION_ID}.service")
        self.assertEqual(str(EXECUTOR_SCRIPT_PATH), "/usr/libexec/elkeeper/ecp-maintenance-resume")
        self.assertEqual(str(EXECUTOR_UNIT_TEMPLATE_PATH), "/etc/systemd/system/ecp-maintenance-resume@.service")
        for invalid in ("", "../bad", "A" * 32, "0" * 31, "0" * 33):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_operation_id(invalid)
        for invalid in ("sshd.service", "ecp-maintenance-resume@x.service", "ecp-bad/one.service"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_managed_unit(invalid)

    def test_paths_are_controller_owned_and_cleanup_is_operation_scoped(self):
        paths = executor_paths(OPERATION_ID)
        for path in (paths.manifest, paths.public_key, paths.checkpoint, paths.result):
            self.assertEqual(require_operation_owned_path(path, OPERATION_ID), path)
        self.assertEqual(
            validate_cleanup_paths((str(paths.result), str(paths.manifest)), OPERATION_ID),
            tuple(sorted((str(paths.result), str(paths.manifest)))),
        )
        for invalid in ("relative", "/tmp/result.json", str(paths.operation_dir / ".." / "other")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                require_operation_owned_path(invalid, OPERATION_ID)
        with self.assertRaises(ValueError):
            validate_cleanup_paths((str(paths.result), str(paths.result)), OPERATION_ID)

    def test_reference_paths_and_local_urls_use_allowlisted_boundaries(self):
        for allowed in (
            "/etc/elastic-control/alpha/ca.crt",
            "/var/lib/elastic-control/maintenance/operations/" + OPERATION_ID + "/checkpoint.json",
            "/run/podman/podman.sock",
        ):
            self.assertEqual(validate_managed_reference_path(allowed), allowed)
        for denied in ("/etc/shadow", "/tmp/check", "/etc/elastic-control/../shadow"):
            with self.subTest(denied=denied), self.assertRaises(ValueError):
                validate_managed_reference_path(denied)
        LocalHttpsCheck(
            check_id="ok",
            url="https://[::1]:9200/",
            ca_path="/etc/elastic-control/alpha/ca.crt",
        )
        for denied in (
            "http://127.0.0.1:9200/",
            "https://elastic.example:9200/",
            "https://user:pass@127.0.0.1:9200/",
            "https://127.0.0.1:9200/?token=secret",
            "https://127.0.0.1/",
        ):
            with self.subTest(denied=denied), self.assertRaises(ValidationError):
                LocalHttpsCheck(
                    check_id="denied",
                    url=denied,
                    ca_path="/etc/elastic-control/alpha/ca.crt",
                )

    def test_manifest_bounds_checks_and_rejects_secret_fields(self):
        with self.assertRaises(ValidationError):
            manifest(password="secret")
        with self.assertRaises(ValidationError):
            manifest(required_units=("ecp-z.service", "ecp-a.service"))
        with self.assertRaises(ValidationError):
            manifest(checks=(UnitActiveCheck(check_id="wrong", unit="ecp-other.service"),))
        with self.assertRaises(ValidationError):
            manifest(expires_at=NOW + timedelta(days=2))
        with self.assertRaises(ValidationError):
            manifest(unit_wait_timeout_seconds=3600)
        paths = executor_paths(OPERATION_ID)
        with self.assertRaises(ValidationError):
            manifest(checkpoint_path=str(paths.result))

    def test_result_is_redacted_and_complete_requires_all_checks_to_pass(self):
        with self.assertRaises(ValidationError):
            result(secret="value")
        with self.assertRaises(ValidationError):
            result(units=(ExecutorUnitResult(unit="ecp-alpha-hot-1.service", active=False),))
        with self.assertRaises(ValidationError):
            result(checks=(ExecutorCheckResult(check_id="hot-active", passed=False),))
        with self.assertRaises(ValidationError):
            result(reason_code="not-completed")
        recovery = result(
            state="recovery_required",
            reason_code="boot_id_unchanged",
            units=(ExecutorUnitResult(unit="ecp-alpha-hot-1.service", active=False),),
            checks=(ExecutorCheckResult(check_id="hot-active", passed=False, error_category="unit_timeout"),),
        )
        self.assertEqual(recovery.state, "recovery_required")
        with self.assertRaises(ValidationError):
            ExecutorCheckResult(check_id="hot-active", passed=True, error_category="unit_timeout")
        with self.assertRaises(ValidationError):
            ExecutorCheckResult(check_id="hot-active", passed=False)
        with self.assertRaises(ValidationError):
            ExecutorSignature(
                key_id="SHA256:" + "a" * 43,
                payload_sha256="b" * 64,
                value="not-base64".ljust(86, "!"),
            )


class MaintenanceExecutorArtifactTests(unittest.TestCase):
    def setUp(self):
        self.playbook = (ROOT / "ansible/playbooks/host-maintenance-executor-stage.yml").read_text()
        self.executor = (ROOT / "ansible/templates/ecp-maintenance-resume.py.j2").read_text()
        self.unit = (ROOT / "ansible/templates/ecp-maintenance-resume@.service.j2").read_text()

    def test_staging_is_disabled_by_default_and_never_reboots_or_starts_executor(self):
        self.assertIn("maintenance_executor_stage_enabled | default(false) | bool", self.playbook)
        self.assertIn("/etc/elastic-control-host-init", self.playbook)
        self.assertIn('mode: "0600"', self.playbook)
        self.assertIn("no_log: true", self.playbook)
        self.assertIn("enabled: true", self.playbook)
        self.assertNotIn("ansible.builtin.reboot", self.playbook)
        self.assertNotIn("state: started", self.playbook)

    def test_unit_is_one_shot_hardened_and_self_disables(self):
        self.assertIn("Type=oneshot", self.unit)
        self.assertIn("NoNewPrivileges=true", self.unit)
        self.assertIn("ProtectSystem=strict", self.unit)
        self.assertIn("ecp-maintenance-resume %i", self.unit)
        self.assertIn('["systemctl", "disable", unit]', self.executor)
        self.assertNotIn("systemctl reboot", self.executor)

    def test_executor_has_bounded_redacted_recovery_and_no_purge_or_rollback_authority(self):
        for required in (
            "recovery_required",
            "boot_id_unchanged",
            "manifest_signature_invalid",
            "os.replace",
            "openssl",
            "payload_sha256",
        ):
            self.assertIn(required, self.executor)
        lowered = self.executor.lower()
        for forbidden in (
            "podman rm", "podman rmi", "quadlet", "rollback", "purge",
            '"systemctl", "reboot"', "shutdown -r",
        ):
            self.assertNotIn(forbidden, lowered)


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL is required for executor compatibility tests")
class MaintenanceExecutorHostStubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / "ansible/templates/ecp-maintenance-resume.py.j2"
        loader = SourceFileLoader("ecp_maintenance_resume_template", str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        assert spec is not None
        cls.executor = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.executor)

    def run_executor(self, observed_boot_id: str, *, corrupt_signature: bool = False):
        executor = self.executor
        key = fixed_private_key()
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            maintenance_root = state_root / "maintenance"
            operations_root = maintenance_root / "operations"
            operation_dir = operations_root / OPERATION_ID
            state_root.mkdir(mode=0o750)
            maintenance_root.mkdir(mode=0o700)
            operations_root.mkdir(mode=0o700)
            operation_dir.mkdir(mode=0o700)

            executor.STATE_ROOT = state_root
            executor.MAINTENANCE_ROOT = maintenance_root
            executor.OPERATIONS_ROOT = operations_root
            executor.EXPECTED_OWNER_UID = os.getuid()
            _, manifest_path, key_path, checkpoint_path, result_path = executor.operation_paths(OPERATION_ID)
            now = datetime.now(timezone.utc)
            manifest_value = {
                "schema_version": 1,
                "signing_context": "elkeeper-host-executor-v1",
                "operation_id": OPERATION_ID,
                "plan_id": PLAN_ID,
                "node_id": 3,
                "created_at": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                "expected_boot_transition": "must_change",
                "pre_reboot_boot_id": BOOT_ID,
                "required_units": [],
                "checks": [],
                "checkpoint_path": str(checkpoint_path),
                "result_path": str(result_path),
                "unit_wait_timeout_seconds": 30,
                "poll_interval_seconds": 1,
            }
            payload = executor.canonical_json(manifest_value)
            signature_value = base64.urlsafe_b64encode(key.sign(payload)).decode("ascii").rstrip("=")
            if corrupt_signature:
                signature_value = "_" * 86
            envelope = {
                "manifest": manifest_value,
                "signature": {
                    "algorithm": "ed25519",
                    "key_id": executor_key_id(key.public_key()),
                    "payload_sha256": hashlib.sha256(payload).hexdigest(),
                    "value": signature_value,
                },
            }
            manifest_path.write_bytes(executor.canonical_json(envelope) + b"\n")
            key_path.write_bytes(executor_public_key_pem(key.public_key()))
            checkpoint_path.write_text('{"state":"staged"}\n')
            for path in (manifest_path, key_path, checkpoint_path):
                path.chmod(0o600)

            with (
                mock.patch.object(executor, "read_boot_id", return_value=observed_boot_id),
                mock.patch.object(executor, "disable_self") as disable_self,
                mock.patch.object(sys, "argv", ["ecp-maintenance-resume", OPERATION_ID]),
            ):
                return_code = executor.main()
            disable_self.assert_called_once_with(OPERATION_ID)
            loaded_result = HostExecutorResult.model_validate_json(result_path.read_text())
            return return_code, loaded_result

    def test_signed_controller_manifest_completes_and_writes_redacted_result(self):
        return_code, loaded_result = self.run_executor("11111111-2222-3333-4444-555555555555")
        self.assertEqual(return_code, 0)
        self.assertEqual(loaded_result.state, "complete")
        self.assertEqual(loaded_result.reason_code, "completed")
        self.assertNotIn("secret", loaded_result.model_dump_json().lower())

    def test_unchanged_boot_id_stops_with_recovery_required(self):
        return_code, loaded_result = self.run_executor(BOOT_ID)
        self.assertEqual(return_code, 1)
        self.assertEqual(loaded_result.state, "recovery_required")
        self.assertEqual(loaded_result.reason_code, "boot_id_unchanged")

    def test_invalid_manifest_signature_stops_with_stable_reason(self):
        return_code, loaded_result = self.run_executor(
            "11111111-2222-3333-4444-555555555555",
            corrupt_signature=True,
        )
        self.assertEqual(return_code, 1)
        self.assertEqual(loaded_result.state, "recovery_required")
        self.assertEqual(loaded_result.reason_code, "manifest_signature_invalid")


if __name__ == "__main__":
    unittest.main()
