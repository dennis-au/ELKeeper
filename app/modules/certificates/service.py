"""Public lifecycle service for certificate discovery, policy, and previews."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .contracts import (
    TRUST_DOMAIN_KINDS,
    CertificateCapabilityBlocked,
    CertificateNotFound,
    CertificateValidationError,
    elastic_tls_capability,
    validate_certificate_policy,
)
from .metadata import inspect_certificate_chain
from .repository import CertificateRepository


_ELASTICSEARCH_ROLES = frozenset({"master", "hot", "warm", "ml", "ingest", "coordinating"})


def _canonical_hash(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class CertificateLifecycleService:
    """Certificate domain facade with no direct host, Podman, or TLS mutation.

    The service first adopts the existing shared certificate layout into an
    immutable, metadata-only projection. Lifecycle previews remain blocked
    until the maintenance module exposes a proven rolling-restart executor.
    """

    def __init__(
        self,
        *,
        db_factory: Callable,
        cluster_provider: Callable[[int], dict[str, Any] | None],
        audit_event: Callable[[str, str, int, str, dict[str, object]], None] | None = None,
        rolling_restart_capability: Callable[[], bool] | None = None,
        completed_run: Callable[[str, str, str, Mapping[str, object] | None], int] | None = None,
        node_provider: Callable[[int], Mapping[str, Any] | None] | None = None,
        remote_file_reader: Callable[[Mapping[str, Any], str], Awaitable[bytes]] | None = None,
    ) -> None:
        self._db = db_factory
        self._cluster_provider = cluster_provider
        self._audit_event = audit_event or (lambda *_args, **_kwargs: None)
        self._rolling_restart_capability = rolling_restart_capability or (lambda: False)
        self._completed_run = completed_run
        self._node_provider = node_provider
        self._remote_file_reader = remote_file_reader

    def list_assets(self, cluster_id: int) -> dict[str, object]:
        cluster = self._cluster(cluster_id)
        capability = elastic_tls_capability(str(cluster.get("desired_version", "")))
        with self._db() as connection:
            repository = CertificateRepository(connection)
            self._adopt_legacy(repository, cluster, capability)
            return {
                "items": repository.list_assets(cluster_id),
                "trust_domains": repository.list_domains(cluster_id),
                "compatibility": self._compatibility_payload(capability),
            }

    def asset_detail(self, certificate_id: str) -> dict[str, object]:
        with self._db() as connection:
            repository = CertificateRepository(connection)
            asset = repository.get_asset(certificate_id)
            cluster = self._cluster(int(asset["cluster_id"]))
            self._adopt_legacy(repository, cluster, elastic_tls_capability(str(cluster.get("desired_version", ""))))
            return {
                "asset": repository.get_asset(certificate_id),
                "generations": repository.list_generations(certificate_id),
                "observations": repository.list_observations(certificate_id),
                "deployments": [],
            }

    def record_inspection(
        self,
        certificate_id: str,
        *,
        certificate_pem: bytes,
        chain_pems: tuple[bytes, ...] = (),
        source: str,
    ) -> dict[str, object]:
        """Record controller-collected public evidence without storing PEM data."""

        with self._db() as connection:
            repository = CertificateRepository(connection)
            asset = repository.get_asset(certificate_id)
            identity = dict(asset["desired_identity"])
            expected_dns = tuple(str(item) for item in identity.get("san_dns", ()) if str(item).strip())
            if not expected_dns and identity.get("node_name"):
                expected_dns = (str(identity["node_name"]),)
            expected_ips = tuple(str(item) for item in identity.get("san_ips", ()) if str(item).strip())
            inspection = inspect_certificate_chain(
                certificate_pem,
                chain_pems,
                purpose=str(asset["purpose"]),
                expected_dns=expected_dns,
                expected_ips=expected_ips,
            )
            return repository.record_observation(
                certificate_id,
                metadata=inspection["metadata"],
                validation=inspection["validation"],
                chain_fingerprints=tuple(inspection["chain_fingerprints"]),
                source=source,
            )

    def policy(self, cluster_id: int) -> dict[str, object]:
        self._cluster(cluster_id)
        with self._db() as connection:
            return CertificateRepository(connection).ensure_default_policy(cluster_id)

    def update_policy(self, cluster_id: int, value: Mapping[str, object], *, username: str) -> dict[str, object]:
        self._cluster(cluster_id)
        try:
            expected_revision = int(value.get("expected_revision", 0))
        except (TypeError, ValueError) as error:
            raise CertificateValidationError("expected_revision is required") from error
        if expected_revision < 1:
            raise CertificateValidationError("expected_revision is required")
        with self._db() as connection:
            repository = CertificateRepository(connection)
            current = repository.ensure_default_policy(cluster_id)
            policy = validate_certificate_policy({**current, **dict(value)})
            updated = repository.update_default_policy(
                cluster_id,
                policy=policy,
                expected_revision=expected_revision,
                username=username,
            )
        self._audit_event(
            username,
            "certificate-policy-updated",
            cluster_id,
            updated["id"],
            {"revision": updated["revision"], **policy},
        )
        return updated

    def compatibility(self, cluster_id: int) -> dict[str, object]:
        cluster = self._cluster(cluster_id)
        return self._compatibility_payload(elastic_tls_capability(str(cluster.get("desired_version", ""))))

    def trust_consumers(self, cluster_id: int) -> dict[str, object]:
        cluster = self._cluster(cluster_id)
        capability = elastic_tls_capability(str(cluster.get("desired_version", "")))
        with self._db() as connection:
            repository = CertificateRepository(connection)
            self._adopt_legacy(repository, cluster, capability)
            consumers = repository.list_consumers(cluster_id)
        external_blockers = [item for item in consumers if item["consumer_type"] == "external" and item["trust_state"] != "verified"]
        return {"items": consumers, "retirement_blocked": bool(external_blockers), "blockers": [item["id"] for item in external_blockers]}

    def declare_external_consumer(
        self,
        cluster_id: int,
        value: Mapping[str, object],
        *,
        username: str,
    ) -> dict[str, object]:
        self._cluster(cluster_id)
        allowed_fields = {"trust_domain_id", "consumer_kind", "description", "verification_method"}
        unexpected = sorted(set(value) - allowed_fields)
        if unexpected:
            raise CertificateValidationError("Unexpected trust consumer fields: " + ", ".join(unexpected))
        try:
            trust_domain_id = str(value["trust_domain_id"])
            consumer_kind = str(value["consumer_kind"])
            description = str(value["description"]).strip()
            verification_method = str(value["verification_method"])
        except KeyError as error:
            raise CertificateValidationError(f"{error.args[0]} is required") from error
        if consumer_kind not in {"external_application", "proxy", "remote_cluster", "beats", "logstash", "automation"}:
            raise CertificateValidationError("consumer_kind is not supported")
        if verification_method != "external_attestation":
            raise CertificateValidationError("External consumers require external_attestation")
        if not 3 <= len(description) <= 200:
            raise CertificateValidationError("description must be between 3 and 200 characters")
        with self._db() as connection:
            consumer = CertificateRepository(connection).declare_external_consumer(
                cluster_id=cluster_id,
                trust_domain_id=trust_domain_id,
                consumer_kind=consumer_kind,
                description=description,
                verification_method=verification_method,
            )
        self._audit_event(
            username,
            "certificate-external-consumer-declared",
            cluster_id,
            consumer["id"],
            {"trust_domain_id": trust_domain_id, "consumer_kind": consumer_kind},
        )
        return consumer

    def operations(self, cluster_id: int) -> dict[str, object]:
        self._cluster(cluster_id)
        with self._db() as connection:
            return {"items": CertificateRepository(connection).list_operations(cluster_id)}

    def operation_detail(self, operation_id: str) -> dict[str, object]:
        with self._db() as connection:
            return CertificateRepository(connection).get_operation(operation_id)

    async def refresh(self, cluster_id: int, *, username: str) -> dict[str, object]:
        inventory = self.list_assets(cluster_id)
        summary = {"collected": 0, "failed": 0}
        mode = "metadata_only"
        if self._node_provider is not None and self._remote_file_reader is not None:
            mode = "remote_metadata"
            cluster = self._cluster(cluster_id)
            assets = list(inventory["items"])
            ca_paths = {
                str(item["trust_domain_id"]): str(item["storage_locator"].get("path", ""))
                for item in assets
                if item["purpose"] == "legacy_shared_ca"
            }
            bootstrap_node_id = next(
                (
                    int(item["node_id"])
                    for item in cluster.get("assignments", [])
                    if item.get("role") == "master" and item.get("node_id") is not None
                ),
                None,
            )
            bootstrap_node = self._node_provider(bootstrap_node_id) if bootstrap_node_id is not None else None
            cached_files: dict[tuple[int, str], bytes] = {}

            async def read_certificate(node_id: int, node: Mapping[str, Any], path: str) -> bytes:
                cache_key = (node_id, path)
                if cache_key not in cached_files:
                    content = await self._remote_file_reader(node, path)
                    if not isinstance(content, bytes):
                        raise TypeError("certificate collector returned a non-bytes result")
                    cached_files[cache_key] = content
                return cached_files[cache_key]

            for asset in assets:
                locator = dict(asset["storage_locator"])
                path = str(locator.get("path", ""))
                try:
                    if bootstrap_node_id is None or not bootstrap_node:
                        raise ValueError("certificate collection bootstrap is unavailable")
                    if asset["purpose"] == "legacy_shared_ca":
                        node_id = bootstrap_node_id
                        node = bootstrap_node
                    else:
                        node_id = int(locator["node_id"])
                        node = self._node_provider(node_id)
                    if not node or not path.startswith("/"):
                        raise ValueError("certificate collection target is unavailable")
                    certificate_pem = await read_certificate(node_id, node, path)
                    chain_pems: tuple[bytes, ...] = ()
                    ca_path = ca_paths.get(str(asset["trust_domain_id"]), "")
                    if asset["purpose"] != "legacy_shared_ca" and ca_path and ca_path != path:
                        chain_pems = (
                            await read_certificate(bootstrap_node_id, bootstrap_node, ca_path),
                        )
                    self.record_inspection(
                        str(asset["id"]),
                        certificate_pem=certificate_pem,
                        chain_pems=chain_pems,
                        source="remote_file",
                    )
                    summary["collected"] += 1
                except Exception:
                    with self._db() as connection:
                        CertificateRepository(connection).record_collection_failure(
                            str(asset["id"]),
                            source="remote_file",
                            error_code="certificate_collection_failed",
                        )
                    summary["failed"] += 1
            inventory = self.list_assets(cluster_id)
        run_id = None
        if self._completed_run is not None:
            run_id = self._completed_run(
                "certificate-inventory-refresh",
                f"cluster:{cluster_id}",
                "Certificate inventory refresh completed without certificate mutation.",
                {"cluster_id": cluster_id, "mode": mode, **summary},
            )
        self._audit_event(
            username,
            "certificate-inventory-refreshed",
            cluster_id,
            "",
            {"mode": mode, "run_id": run_id, **summary},
        )
        return {"run_id": run_id, "inventory": inventory, "mode": mode, "summary": summary}

    def renewal_preview(self, certificate_id: str, *, username: str) -> dict[str, object]:
        with self._db() as connection:
            repository = CertificateRepository(connection)
            asset = repository.get_asset(certificate_id)
            cluster = self._cluster(int(asset["cluster_id"]))
            capability = elastic_tls_capability(str(cluster.get("desired_version", "")))
            self._adopt_legacy(repository, cluster, capability)
            policy = repository.ensure_default_policy(int(asset["cluster_id"]))
            blockers = self._preview_blockers(asset, capability)
            summary = {
                "certificate_id": asset["id"],
                "purpose": asset["purpose"],
                "legacy_shared": asset["legacy_shared"],
                "requires_rolling_restart": True,
                "mutation_performed": False,
            }
            operation = repository.create_preview_operation(
                cluster_id=int(asset["cluster_id"]),
                operation_type="leaf_renewal",
                trust_domain_ids=(str(asset["trust_domain_id"]),),
                request_hash=_canonical_hash({"asset": asset["id"], "policy_revision": policy["revision"], "operation": "leaf_renewal"}),
                policy_revision=int(policy["revision"]),
                requested_by=username,
                blockers=tuple(blockers),
                summary=summary,
            )
        self._audit_event(
            username,
            "certificate-renewal-preview-created",
            int(asset["cluster_id"]),
            operation["id"],
            {"certificate_id": asset["id"], "blockers": blockers},
        )
        return self._preview_projection(operation, capability)

    def ca_rotation_preview(self, cluster_id: int, *, username: str) -> dict[str, object]:
        cluster = self._cluster(cluster_id)
        capability = elastic_tls_capability(str(cluster.get("desired_version", "")))
        with self._db() as connection:
            repository = CertificateRepository(connection)
            self._adopt_legacy(repository, cluster, capability)
            policy = repository.ensure_default_policy(cluster_id)
            domains = repository.list_domains(cluster_id)
            consumers = repository.list_consumers(cluster_id)
            blockers = []
            if not capability["supported"]:
                blockers.append(str(capability["reason_code"]))
            if not self._rolling_restart_capability():
                blockers.append("rolling_restart_capability_disabled")
            if any(domain["legacy_shared"] for domain in domains):
                blockers.append("legacy_shared_split_migration_required")
            if any(item["consumer_type"] == "external" and item["trust_state"] != "verified" for item in consumers):
                blockers.append("external_trust_consumer_unverified")
            operation = repository.create_preview_operation(
                cluster_id=cluster_id,
                operation_type="ca_rotation",
                trust_domain_ids=tuple(str(domain["id"]) for domain in domains),
                request_hash=_canonical_hash({"cluster": cluster_id, "policy_revision": policy["revision"], "operation": "ca_rotation"}),
                policy_revision=int(policy["revision"]),
                requested_by=username,
                blockers=tuple(dict.fromkeys(blockers)),
                summary={
                    "trust_domains": [domain["kind"] for domain in domains],
                    "requires_dual_trust": True,
                    "requires_rolling_restart": True,
                    "mutation_performed": False,
                },
            )
        self._audit_event(username, "certificate-ca-rotation-preview-created", cluster_id, operation["id"], {"blockers": blockers})
        return self._preview_projection(operation, capability)

    def require_execution_capability(self, operation_id: str) -> None:
        operation = self.operation_detail(operation_id)
        blockers = list(operation["blockers"])
        if not self._rolling_restart_capability():
            blockers.append("rolling_restart_capability_disabled")
        if blockers:
            raise CertificateCapabilityBlocked(
                "Certificate mutation is blocked: " + ", ".join(sorted(set(str(item) for item in blockers)))
            )

    def _adopt_legacy(
        self,
        repository: CertificateRepository,
        cluster: Mapping[str, Any],
        capability: Mapping[str, object],
    ) -> None:
        cluster_id = int(cluster["id"])
        slug = str(cluster["slug"])
        domains = {
            kind: repository.ensure_domain(
                cluster_id=cluster_id,
                kind=kind,
                compatibility_profile=str(capability["profile"]),
            )
            for kind in TRUST_DOMAIN_KINDS
        }
        repository.ensure_default_policy(cluster_id)
        base = f"/etc/elastic-control/clusters/{slug}"
        for kind, domain in domains.items():
            repository.ensure_asset(
                cluster_id=cluster_id,
                trust_domain_id=str(domain["id"]),
                owner_type="cluster",
                owner_id=str(cluster_id),
                purpose="legacy_shared_ca",
                storage_locator={"path": f"{base}/ca/ca.crt", "legacy_shared": True},
                desired_identity={"trust_domain": kind, "issuer_role": "legacy_shared"},
            )
        for assignment in cluster.get("assignments", []):
            self._adopt_assignment(repository, cluster, domains, assignment)
        repository.ensure_managed_consumer(
            cluster_id=cluster_id,
            trust_domain_id=str(domains["elasticsearch_http"]["id"]),
            consumer_kind="controller_elasticsearch_client",
            owner_id="controller",
            description="ELKeeper controller Elasticsearch client",
        )

    @staticmethod
    def _adopt_assignment(
        repository: CertificateRepository,
        cluster: Mapping[str, Any],
        domains: Mapping[str, Mapping[str, object]],
        assignment: Mapping[str, Any],
    ) -> None:
        cluster_id = int(cluster["id"])
        assignment_id = str(assignment["id"])
        role = str(assignment["role"])
        workload = f"ecp-{cluster['slug']}-{role}-{assignment['node_id']}"
        locator = {
            "node_id": int(assignment["node_id"]),
            "node_name": str(assignment.get("node_name", "")),
            "path": f"/etc/elastic-control/clusters/{cluster['slug']}/workloads/{workload}/certs/node.crt",
            "legacy_shared": True,
        }
        identity = {"role": role, "node_name": locator["node_name"], "legacy_shared": True}
        if role in _ELASTICSEARCH_ROLES:
            for domain_kind, purpose in (
                ("elasticsearch_transport", "elasticsearch_transport"),
                ("elasticsearch_http", "elasticsearch_http"),
            ):
                repository.ensure_asset(
                    cluster_id=cluster_id,
                    trust_domain_id=str(domains[domain_kind]["id"]),
                    owner_type="assignment",
                    owner_id=assignment_id,
                    purpose=purpose,
                    storage_locator=locator,
                    desired_identity=identity,
                )
                repository.ensure_managed_consumer(
                    cluster_id=cluster_id,
                    trust_domain_id=str(domains[domain_kind]["id"]),
                    consumer_kind="workload",
                    owner_id=assignment_id,
                    description=f"{workload} {purpose}",
                )
            return
        domain_kind, purpose = {
            "kibana": ("kibana_http", "kibana_server"),
            "fleet-server": ("fleet_http", "fleet_server"),
            "elastic-agent": ("fleet_http", "elastic_agent_client"),
            "logstash": ("elasticsearch_http", "logstash_elasticsearch_client"),
        }.get(role, ("elasticsearch_http", f"{role}_client"))
        repository.ensure_asset(
            cluster_id=cluster_id,
            trust_domain_id=str(domains[domain_kind]["id"]),
            owner_type="assignment",
            owner_id=assignment_id,
            purpose=purpose,
            storage_locator=locator,
            desired_identity=identity,
        )
        repository.ensure_managed_consumer(
            cluster_id=cluster_id,
            trust_domain_id=str(domains[domain_kind]["id"]),
            consumer_kind="workload",
            owner_id=assignment_id,
            description=f"{workload} {purpose}",
        )

    def _cluster(self, cluster_id: int) -> dict[str, Any]:
        cluster = self._cluster_provider(cluster_id)
        if not cluster:
            raise CertificateNotFound("Cluster not found")
        return cluster

    def _preview_blockers(self, asset: Mapping[str, object], capability: Mapping[str, object]) -> list[str]:
        blockers: list[str] = []
        if not capability["supported"]:
            blockers.append(str(capability["reason_code"]))
        if bool(asset["legacy_shared"]):
            blockers.append("legacy_shared_split_migration_required")
        if not self._rolling_restart_capability():
            blockers.append("rolling_restart_capability_disabled")
        return blockers

    @staticmethod
    def _compatibility_payload(capability: Mapping[str, object]) -> dict[str, object]:
        return {
            **dict(capability),
            "mutation_enabled": bool(capability["supported"]) and False,
            "mutation_blocker": "rolling_restart_capability_disabled",
        }

    @staticmethod
    def _preview_projection(operation: Mapping[str, object], capability: Mapping[str, object]) -> dict[str, object]:
        return {
            "operation_id": operation["id"],
            "operation_type": operation["operation_type"],
            "state": operation["state"],
            "preview_hash": operation["request_hash"],
            "run_id": operation["run_id"],
            "blockers": operation["blockers"],
            "summary": operation["summary"],
            "execution_enabled": False,
            "compatibility": CertificateLifecycleService._compatibility_payload(capability),
        }


__all__ = ["CertificateLifecycleService"]
