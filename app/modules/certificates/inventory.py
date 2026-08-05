"""Allowlisted certificate and workload-key inventory projections."""

from __future__ import annotations

from collections.abc import Callable


class CertificateInventoryService:
    """Build certificate/key metadata without reading secret contents."""

    def __init__(self, host_provider: Callable[[int], dict | None]):
        self._host_provider = host_provider

    def cluster_items(self, cluster: dict) -> list[dict]:
        master = next((item for item in cluster["assignments"] if item["role"] == "master"), None)
        if not master:
            return []
        node = self._host(master["node_id"])
        base = self._cluster_base(cluster)
        return [
            {
                "id": "cluster.ca_certificate",
                "label": "Cluster CA certificate",
                "category": "Certificates",
                "source": master["node_name"],
                "node": node,
                "path": f"{base}/ca/ca.crt",
                "certificate": True,
            },
            {
                "id": "cluster.ca_private_key",
                "label": "Cluster CA private key",
                "category": "Private keys",
                "source": master["node_name"],
                "node": node,
                "path": f"{base}/ca/ca.key",
                "reveal_deprecated": True,
                "value_access": "metadata_only",
            },
        ]

    def workload_items(self, cluster: dict, assignment: dict) -> list[dict]:
        node = self._host(assignment["node_id"])
        workload = f"ecp-{cluster['slug']}-{assignment['role']}-{assignment['node_id']}"
        base = f"{self._cluster_base(cluster)}/workloads/{workload}"
        prefix = f"assignment.{assignment['id']}"
        items = [
            {
                "id": f"{prefix}.certificate",
                "label": f"{workload} certificate",
                "category": "Certificates",
                "source": assignment["node_name"],
                "node": node,
                "path": f"{base}/certs/node.crt",
                "certificate": True,
            },
            {
                "id": f"{prefix}.private_key",
                "label": f"{workload} private key",
                "category": "Private keys",
                "source": assignment["node_name"],
                "node": node,
                "path": f"{base}/certs/node.key",
                "reveal_deprecated": True,
                "value_access": "metadata_only",
            },
        ]
        token_paths = {
            "fleet-server": ("fleet_service_token", "Fleet", "fleet service token", "fleet-service-token"),
            "elastic-agent": ("enrollment_token", "Fleet", "enrollment token", "agent-enrollment-token"),
        }
        if assignment["role"] in token_paths:
            suffix, category, label, filename = token_paths[assignment["role"]]
            items.append(
                {
                    "id": f"{prefix}.{suffix}",
                    "label": f"{workload} {label}",
                    "category": category,
                    "source": assignment["node_name"],
                    "node": node,
                    "path": f"{base}/config/{filename}",
                }
            )
        return items

    @staticmethod
    def _cluster_base(cluster: dict) -> str:
        return f"/etc/elastic-control/clusters/{cluster['slug']}"

    def _host(self, node_id: int) -> dict:
        node = self._host_provider(node_id)
        if not node:
            raise KeyError(node_id)
        return node


__all__ = ["CertificateInventoryService"]
