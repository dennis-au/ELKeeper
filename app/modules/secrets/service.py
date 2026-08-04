"""Secret catalog assembly behind public host and cluster projections."""

from __future__ import annotations

from typing import Callable

from app.modules.certificates import CertificateInventoryService, certificate_public_metadata
from app.modules.platform import open_config


class SecretsCatalogService:
    """Build sensitive-item metadata without exposing database access."""

    def __init__(
        self,
        *,
        cluster_provider: Callable[[int], dict],
        encrypted_credentials_provider: Callable[[int], str],
        host_provider: Callable[[int], dict | None],
        certificate_inventory: CertificateInventoryService | None = None,
    ):
        self._cluster_provider = cluster_provider
        self._encrypted_credentials_provider = encrypted_credentials_provider
        self._host_provider = host_provider
        self._certificate_inventory = certificate_inventory or CertificateInventoryService(host_provider)

    def catalog(self, cluster_id: int) -> tuple[dict, dict, list[dict]]:
        cluster = self._cluster_provider(cluster_id)
        credentials = open_config(self._encrypted_credentials_provider(cluster_id))
        items = self._credential_items(credentials)
        items.extend(self._certificate_inventory.cluster_items(cluster))
        for assignment in cluster["assignments"]:
            items.extend(self._certificate_inventory.workload_items(cluster, assignment))
        return cluster, credentials, items

    @staticmethod
    def _credential_items(credentials: dict) -> list[dict]:
        return [
            {
                "id": f"cluster.{key}",
                "label": label,
                "category": "Credentials",
                "source": "controller",
                "db_key": key,
                "available": bool(credentials.get(key)),
            }
            for key, label in (
                ("elastic_password", "Elastic superuser password"),
                ("kibana_password", "kibana_system password"),
                ("monitoring_api_key", "Dashboard monitoring API key"),
            )
        ]

class RemoteSecretMetadataService:
    """Probe allowlisted secret/certificate metadata without returning values."""

    def __init__(self, remote_command: Callable[..., object]):
        self._remote_command = remote_command

    async def inspect(self, item: dict) -> dict:
        if "node" not in item or not item.get("path"):
            return item
        result = dict(item)
        try:
            if item.get("certificate"):
                content = await self._remote_command(item["node"], "cat", item["path"])
                result.update(certificate_public_metadata(content))
            else:
                await self._remote_command(item["node"], "test", "-s", item["path"])
            result["available"] = True
        except Exception:
            result["available"] = False
        return result


__all__ = ["SecretsCatalogService", "RemoteSecretMetadataService"]
