from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
import unittest

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from app.modules.maintenance.host_actions import (
    HostMaintenanceActionDisabled,
    HostMaintenanceActionService,
    HostWorkflowAction,
)
from app.modules.maintenance.planned_contracts import MaintenanceWorkflowState
from app.modules.maintenance.workflow_http import build_host_workflow_router


class _FakeRepository:
    def __init__(self):
        self.plan = SimpleNamespace(
            id="b" * 32,
            run_id=23,
            lifecycle_state=SimpleNamespace(value="executing"),
        )

    def get_plan(self, plan_id: str):
        if plan_id != self.plan.id:
            raise LookupError("missing")
        return self.plan


class _FakeWorkflow:
    def __init__(self):
        self.repository = _FakeRepository()
        self.calls: list[tuple[str, str, str]] = []

    async def prepare(self, plan_id: str, *, username: str):
        self.calls.append(("prepare", plan_id, username))
        return SimpleNamespace(workflow_state=MaintenanceWorkflowState.READY_TO_STOP)

    async def stop_workloads(self, plan_id: str, *, username: str):
        self.calls.append(("stop", plan_id, username))
        return SimpleNamespace(workflow_state=MaintenanceWorkflowState.MAINTENANCE)

    async def reboot_host(self, plan_id: str, *, username: str):
        self.calls.append(("reboot", plan_id, username))
        return SimpleNamespace(workflow_state=MaintenanceWorkflowState.MAINTENANCE)

    async def record_operator_handoff(self, plan_id: str, *, username: str):
        self.calls.append(("handoff", plan_id, username))
        return SimpleNamespace(workflow_state=MaintenanceWorkflowState.MAINTENANCE)

    async def return_to_service(self, plan_id: str, *, username: str):
        self.calls.append(("return", plan_id, username))
        self.repository.plan.lifecycle_state = SimpleNamespace(value="succeeded")
        return SimpleNamespace(workflow_state=MaintenanceWorkflowState.AVAILABLE)


@contextmanager
def _connection():
    yield object()


class HostMaintenanceActionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_capability_rejects_before_workflow_factory_is_called(self):
        calls = []
        service = HostMaintenanceActionService(
            db_factory=_connection,
            enabled=lambda: False,
            workflow_factory=lambda _connection: calls.append("factory"),
        )

        with self.assertRaises(HostMaintenanceActionDisabled):
            await service.perform("b" * 32, HostWorkflowAction.PREPARE, username="operator")

        self.assertEqual(calls, [])

    async def test_enabled_actions_dispatch_only_the_supported_workflow_methods(self):
        workflow = _FakeWorkflow()
        service = HostMaintenanceActionService(
            db_factory=_connection,
            enabled=lambda: True,
            workflow_factory=lambda _connection: workflow,
        )

        prepared = await service.perform("b" * 32, HostWorkflowAction.PREPARE, username="operator")
        stopped = await service.perform("b" * 32, HostWorkflowAction.STOP, username="operator")
        rebooted = await service.perform("b" * 32, HostWorkflowAction.REBOOT, username="operator")
        handed_off = await service.perform("b" * 32, HostWorkflowAction.HANDOFF, username="operator")
        returned = await service.perform("b" * 32, HostWorkflowAction.RETURN, username="operator")

        self.assertEqual(workflow.calls, [
            ("prepare", "b" * 32, "operator"),
            ("stop", "b" * 32, "operator"),
            ("reboot", "b" * 32, "operator"),
            ("handoff", "b" * 32, "operator"),
            ("return", "b" * 32, "operator"),
        ])
        self.assertEqual(prepared.run_id, 23)
        self.assertEqual(stopped.workflow_state, "maintenance")
        self.assertEqual(rebooted.action, "reboot")
        self.assertEqual(handed_off.action, "handoff")
        self.assertEqual(returned.lifecycle_state, "succeeded")


class _HttpOperations:
    def __init__(self):
        self.calls = []
        self.error: Exception | None = None

    async def perform(self, plan_id, action, *, username):
        if self.error is not None:
            raise self.error
        self.calls.append((plan_id, action, username))
        return SimpleNamespace(
            plan_id=plan_id,
            run_id=23,
            action=action.value,
            workflow_state="ready_to_stop",
            lifecycle_state="executing",
        )


def _user(authorization: str | None = Header(default=None)):
    if authorization != "Bearer test":
        raise HTTPException(401, "Not authenticated")
    return "operator"


class HostWorkflowHttpTests(unittest.TestCase):
    def setUp(self):
        self.operations = _HttpOperations()
        app = FastAPI()
        app.include_router(
            build_host_workflow_router(
                operations=self.operations,
                user_dependency=_user,
            )
        )
        self.client = TestClient(app)

    def test_authentication_dispatch_and_failure_redaction(self):
        path = "/api/maintenance/host-workflows/" + "b" * 32 + "/prepare"
        self.assertEqual(self.client.post(path).status_code, 401)

        success = self.client.post(path, headers={"Authorization": "Bearer test"})
        self.assertEqual(success.status_code, 200, success.text)
        self.assertEqual(success.json()["run_id"], 23)
        self.assertEqual(self.operations.calls[0][1], HostWorkflowAction.PREPARE)

        self.operations.error = HostMaintenanceActionDisabled("disabled")
        disabled = self.client.post(path, headers={"Authorization": "Bearer test"})
        self.assertEqual(disabled.status_code, 409)

        self.operations.error = RuntimeError("password=top-secret")
        protected = self.client.post(path, headers={"Authorization": "Bearer test"})
        self.assertEqual(protected.status_code, 502)
        self.assertNotIn("top-secret", protected.text)


if __name__ == "__main__":
    unittest.main()
