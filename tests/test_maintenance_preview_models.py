import unittest

from pydantic import TypeAdapter, ValidationError

from app.modules.maintenance.models import MaintenancePlanPreviewInput, PreviewOperation


class MaintenancePreviewModelTests(unittest.TestCase):
    def test_public_operation_union_accepts_every_phase_one_target(self):
        adapter = TypeAdapter(MaintenancePlanPreviewInput)
        requests = [
            {"operation": "reboot", "node_id": 1, "reason": "inspect"},
            {"operation": "manual_maintenance", "node_id": 1, "reason": "inspect"},
            {"operation": "host_maintenance", "node_id": 1, "reason": "inspect"},
            {"operation": "container_maintenance", "assignment_id": 2, "reason": "inspect"},
            {"operation": "resource_change", "assignment_ids": [2], "reason": "inspect"},
            {"operation": "cluster_settings", "cluster_id": 3, "reason": "inspect"},
            {"operation": "zoning", "cluster_id": 3, "reason": "inspect"},
            {"operation": "apply", "assignment_ids": [2], "reason": "inspect"},
            {"operation": "detach", "assignment_ids": [2], "reason": "inspect"},
            {"operation": "purge", "assignment_ids": [2], "reason": "inspect"},
            {"operation": "download", "cluster_id": 3, "reason": "inspect"},
            {
                "operation": "upgrade", "cluster_id": 3,
                "current_version": "8.18.0", "target_version": "8.18.1", "reason": "inspect",
            },
        ]
        parsed = [adapter.validate_python(item) for item in requests]
        self.assertEqual({item.operation for item in parsed}, set(PreviewOperation))

    def test_public_operation_union_rejects_missing_or_cross_scope_targets(self):
        adapter = TypeAdapter(MaintenancePlanPreviewInput)
        with self.assertRaises(ValidationError):
            adapter.validate_python({"operation": "resource_change", "reason": "inspect"})
        with self.assertRaises(ValidationError):
            adapter.validate_python({"operation": "cluster_settings", "node_id": 1, "reason": "inspect"})
        with self.assertRaises(ValidationError):
            adapter.validate_python({"operation": "upgrade", "cluster_id": 3, "reason": "inspect"})


if __name__ == "__main__":
    unittest.main()
