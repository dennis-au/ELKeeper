"""Disabled-by-default composition for the future Phase 2 reboot adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .controller_io import ControllerMaintenanceIOAdapter
from .elasticsearch import AllocationGuardController, ElasticsearchMaintenanceClient
from .post_return import PostReturnCoordinator
from .reboot import (
    PredicateEvaluatorProtocol,
    RebootControlProtocol,
    RebootOrchestrator,
)
from .repository import MaintenanceRepository
from .runtime import ControllerManagedHostRuntime


@dataclass(frozen=True)
class Phase2RebootAdapterComposition:
    """One inert, inspectable Phase 2 reboot-adapter dependency graph.

    The composition is intentionally not a maintenance action adapter and is
    never registered by this module.  It gives application assembly a single
    typed place to join the CA-verified Elasticsearch client, allocation guard,
    controller I/O, signed one-shot executor runtime, reboot orchestrator, and
    post-return cleanup coordinator once Phase 2's release gates are complete.
    """

    elasticsearch_client: ElasticsearchMaintenanceClient
    allocation_guard: AllocationGuardController
    controller_io: ControllerMaintenanceIOAdapter
    executor_runtime: ControllerManagedHostRuntime
    reboot_orchestrator: RebootOrchestrator
    post_return: PostReturnCoordinator


class Phase2RebootAdapterFactory:
    """Compose existing reboot contracts without enabling or registering them."""

    def __init__(
        self,
        *,
        elasticsearch_client_factory: Callable[[], ElasticsearchMaintenanceClient],
        allocation_guard_factory: Callable[[ElasticsearchMaintenanceClient], AllocationGuardController],
        controller_io_factory: Callable[[], ControllerMaintenanceIOAdapter],
        executor_runtime_factory: Callable[[ControllerMaintenanceIOAdapter], ControllerManagedHostRuntime],
        post_return_factory: Callable[
            [ControllerMaintenanceIOAdapter, AllocationGuardController, ControllerManagedHostRuntime],
            PostReturnCoordinator,
        ],
        reboot_orchestrator_type: type[RebootOrchestrator] = RebootOrchestrator,
    ) -> None:
        self._elasticsearch_client_factory = elasticsearch_client_factory
        self._allocation_guard_factory = allocation_guard_factory
        self._controller_io_factory = controller_io_factory
        self._executor_runtime_factory = executor_runtime_factory
        self._post_return_factory = post_return_factory
        self._reboot_orchestrator_type = reboot_orchestrator_type

    def compose(
        self,
        *,
        repository: MaintenanceRepository,
        predicates: PredicateEvaluatorProtocol,
        control: RebootControlProtocol | None = None,
    ) -> Phase2RebootAdapterComposition:
        """Build the graph without registering it or permitting reboot execution."""

        elasticsearch_client = self._elasticsearch_client_factory()
        allocation_guard = self._allocation_guard_factory(elasticsearch_client)
        controller_io = self._controller_io_factory()
        executor_runtime = self._executor_runtime_factory(controller_io)
        reboot_orchestrator = self._reboot_orchestrator_type(
            repository=repository,
            predicates=predicates,
            executor=executor_runtime,
            host=executor_runtime,
            control=control,
            execution_enabled=False,
        )
        post_return = self._post_return_factory(controller_io, allocation_guard, executor_runtime)
        return Phase2RebootAdapterComposition(
            elasticsearch_client=elasticsearch_client,
            allocation_guard=allocation_guard,
            controller_io=controller_io,
            executor_runtime=executor_runtime,
            reboot_orchestrator=reboot_orchestrator,
            post_return=post_return,
        )


__all__ = ["Phase2RebootAdapterComposition", "Phase2RebootAdapterFactory"]
