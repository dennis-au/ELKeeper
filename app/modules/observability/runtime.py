"""Runtime telemetry provider contract.

The console registers its telemetry manager during application startup. Domain
routers consume the provider without importing the console implementation.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable


_telemetry: Any = None


def register_telemetry(manager: Any) -> None:
    global _telemetry
    _telemetry = manager


def telemetry() -> Any:
    return _telemetry


class TelemetrySupervisor:
    """Own lifecycle and publication access for a telemetry collector.

    The collector itself remains injectable so the compatibility console can
    keep its existing probing implementation while observability owns startup,
    shutdown, and stream access.  This is deliberately small: it is the
    public boundary that future providers (native, imported, or ECK) can use.
    """

    def __init__(self, collector: Any):
        self.collector = collector

    async def start(self) -> Any:
        return await self.collector.start()

    async def stop(self) -> Any:
        return await self.collector.stop()

    async def collect_once(self) -> Any:
        return await self.collector.collect_once()

    def subscribe(self) -> Any:
        return self.collector.subscribe()

    def unsubscribe(self, queue: Any) -> Any:
        return self.collector.unsubscribe(queue)

    async def publish(self, event: str, payload: dict) -> Any:
        return await self.collector.publish(event, payload)

    def __getattr__(self, name: str) -> Any:
        # Compatibility reads for existing dashboard projections.  New code
        # should use the explicit methods above or a typed provider contract.
        return getattr(self.collector, name)
