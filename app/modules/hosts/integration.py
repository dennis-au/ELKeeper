"""Public composition facades for host enrollment and lifecycle operations."""

from __future__ import annotations

from typing import Any, Callable

from .http import build_batch_router, build_lifecycle_router
from .repository import HostRepository


class HostOperations:
    """Own host-orchestration construction without importing application assembly."""

    def __init__(
        self,
        *,
        orchestrator_type: type,
        db_factory: Callable,
        inventories: Any,
        runtime: Any,
        playbooks: Any,
        active_key_path: Callable,
        enrollment_key: Callable,
        managed_key_path: Callable,
        known_hosts_path: Callable,
        launch: Callable,
        audit: Callable,
    ) -> None:
        self._orchestrator_type = orchestrator_type
        self._kwargs = {
            "db_factory": db_factory,
            "inventories": inventories,
            "runtime": runtime,
            "playbooks": playbooks,
            "active_key_path": active_key_path,
            "enrollment_key": enrollment_key,
            "managed_key_path": managed_key_path,
            "known_hosts_path": known_hosts_path,
            "launch": launch,
            "audit": audit,
        }

    def orchestrator(self):
        return self._orchestrator_type(**self._kwargs)

    def inventory(self, *args, **kwargs):
        return self.orchestrator().inventory(*args, **kwargs)

    def password_test_known_hosts(self, *args, **kwargs):
        return self.orchestrator().password_test_known_hosts(*args, **kwargs)

    def password_test_command(self, *args, **kwargs):
        return self.orchestrator().password_test_command(*args, **kwargs)

    def verify_ssh_password(self, *args, **kwargs):
        return self.orchestrator().verify_ssh_password(*args, **kwargs)

    def enrollment_variables(self, *args, **kwargs):
        return self.orchestrator().enrollment_variables(*args, **kwargs)

    def enrollment_context(self, *args, **kwargs):
        return self.orchestrator().enrollment_context(*args, **kwargs)

    def launch_password_enrollment(self, *args, **kwargs):
        return self.orchestrator().launch_password_enrollment(*args, **kwargs)

    def launch_key_enrollment_probe(self, *args, **kwargs):
        return self.orchestrator().launch_key_enrollment_probe(*args, **kwargs)


class HostLifecycleOperations:
    """Compose host lifecycle routes without leaking launcher details to assembly."""

    def __init__(
        self,
        *,
        db_factory: Callable,
        require_no_maintenance_conflict: Callable,
        workload_repository_type: type,
        playbooks: Any,
        active_key_path: Callable[[], str],
        launch: Callable,
        playbook_command: Callable,
        host_repository_type: type = HostRepository,
    ) -> None:
        self._db_factory = db_factory
        self._require_no_maintenance_conflict = require_no_maintenance_conflict
        self._workload_repository_type = workload_repository_type
        self._playbooks = playbooks
        self._active_key_path = active_key_path
        self._launch = launch
        self._playbook_command = playbook_command
        self._host_repository_type = host_repository_type

    def require_no_conflict(self, node_id: int) -> None:
        with self._db_factory() as connection:
            self._require_no_maintenance_conflict(connection, node_id=node_id)

    def has_assignments(self, node_id: int) -> bool:
        return self._workload_repository_type(self._db_factory).has_assignments_for_node(node_id)

    def launch_action(self, node: dict, action: str) -> int:
        playbook = self._playbooks / f"host-{action}.yml"
        return self._launch(
            f"host-{action}",
            node["name"],
            lambda inventory, _variables: self._playbook_command(
                inventory,
                playbook,
                node["name"],
                self._active_key_path(),
            ),
        )

    def launch_initialize(self, name: str) -> int:
        return self._launch(
            "host-init",
            name,
            lambda inventory, _variables: self._playbook_command(
                inventory,
                self._playbooks / "host-init.yml",
                name,
                self._active_key_path(),
            ),
        )

    def lifecycle_router(self, *, enabled_host_provider: Callable[[int], dict], user_dependency: Callable):
        return build_lifecycle_router(
            enabled_host_provider=enabled_host_provider,
            require_no_conflict=self.require_no_conflict,
            has_assignments=self.has_assignments,
            launch_action=self.launch_action,
            user_dependency=user_dependency,
        )

    def batch_router(self, *, batch_model: type, user_dependency: Callable):
        return build_batch_router(
            batch_model=batch_model,
            db_factory=self._db_factory,
            require_no_conflict=self.require_no_conflict,
            enabled_host_names=lambda connection, node_ids: self._host_repository_type.from_connection(
                connection
            ).enabled_names_in_connection(connection, node_ids),
            launch_action=self.launch_initialize,
            user_dependency=user_dependency,
        )


__all__ = ["HostOperations", "HostLifecycleOperations"]
