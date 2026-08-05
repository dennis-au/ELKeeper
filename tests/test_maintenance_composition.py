from __future__ import annotations

import unittest
from unittest.mock import patch

from app.modules.maintenance import Phase2RebootAdapterFactory
from app.modules.maintenance.execution import MAINTENANCE_ADAPTERS


class Phase2RebootCompositionTests(unittest.TestCase):
    def test_factory_composes_existing_boundaries_without_registering_or_enabling_execution(self):
        events = []
        client = object()
        guard = object()
        controller_io = object()
        executor_runtime = object()
        post_return = object()
        repository = object()
        predicates = object()

        factory = Phase2RebootAdapterFactory(
            elasticsearch_client_factory=lambda: events.append("elasticsearch") or client,
            allocation_guard_factory=lambda value: events.append(("allocation", value)) or guard,
            controller_io_factory=lambda: events.append("controller-io") or controller_io,
            executor_runtime_factory=lambda value: events.append(("executor", value)) or executor_runtime,
            post_return_factory=lambda io, allocation, executor: events.append(
                ("post-return", io, allocation, executor)
            )
            or post_return,
        )

        with patch.dict(MAINTENANCE_ADAPTERS, {}, clear=True):
            composition = factory.compose(repository=repository, predicates=predicates)
            self.assertFalse(MAINTENANCE_ADAPTERS)

        self.assertIs(composition.elasticsearch_client, client)
        self.assertIs(composition.allocation_guard, guard)
        self.assertIs(composition.controller_io, controller_io)
        self.assertIs(composition.executor_runtime, executor_runtime)
        self.assertIs(composition.post_return, post_return)
        self.assertIs(composition.reboot_orchestrator.repository, repository)
        self.assertIs(composition.reboot_orchestrator.predicates, predicates)
        self.assertIs(composition.reboot_orchestrator.executor, executor_runtime)
        self.assertIs(composition.reboot_orchestrator.host, executor_runtime)
        self.assertFalse(composition.reboot_orchestrator.execution_enabled)
        self.assertEqual(
            events,
            [
                "elasticsearch",
                ("allocation", client),
                "controller-io",
                ("executor", controller_io),
                ("post-return", controller_io, guard, executor_runtime),
            ],
        )
