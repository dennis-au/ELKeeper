"""Planning-only bridge for maintenance-backed version upgrades.

The versions module discovers releases and downloads images.  This module owns
the immutable identity captured by a maintenance plan and the safety decision
that must happen before an upgrade could be approved.  No method in this file
starts a run, rewrites a Quadlet, restarts a workload, or contacts a host.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.modules.platform import RunDescriptor, create_run_in_connection, finish_run_in_connection

from .lifecycle import MaintenanceState, SideEffectState
from .models import OperationKind
from .store import MaintenanceRepository, iso_timestamp, utc_now
from .upgrade_contracts import (
    UpgradeArtifact,
    UpgradeManifest,
    UpgradePreflight,
    build_upgrade_manifest,
    validate_upgrade_transition,
)
from .workload_contracts import legacy_role_to_workload_role, rollback_allowed


class UpgradeManifestProjection(BaseModel):
    """Redacted JSON-safe representation stored in ``target_manifest``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifacts: tuple[UpgradeArtifact, ...]

    @classmethod
    def from_manifest(cls, manifest: UpgradeManifest) -> "UpgradeManifestProjection":
        return cls(
            schema_version=1,
            manifest_hash=manifest.manifest_hash,
            artifacts=manifest.artifacts,
        )

    def to_manifest(self) -> UpgradeManifest:
        manifest = build_upgrade_manifest(self.artifacts)
        if manifest.manifest_hash != self.manifest_hash:
            raise ValueError("upgrade manifest hash does not match its artifacts")
        return manifest


class UpgradePlanPreview(BaseModel):
    """Stable result for a non-mutating upgrade plan preview."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cluster_id: int = Field(ge=1)
    current_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    target_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    manifest: UpgradeManifestProjection
    blockers: tuple[str, ...] = ()
    execution_enabled: bool = False

    @property
    def ready(self) -> bool:
        return not self.blockers and not self.execution_enabled


class UpgradePlanDispatch(BaseModel):
    """Legacy-route-compatible result of a maintenance-owned upgrade dispatch.

    The run is a controller-side planning record only while the Phase 4
    capability is disabled.  It deliberately has no orchestration command or
    remote side effect attached to it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: int = Field(ge=1)
    plan_id: str = Field(min_length=1)
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_enabled: bool = False
    blockers: tuple[str, ...] = ()


class MaintenanceUpgradePlanningService:
    """Persist immutable, ordered upgrade plans before any execution seam.

    Version discovery and registry access stay injected from ``versions``.  A
    future approved executor can consume these plan records, but this service
    never launches a playbook, restarts a container, or changes workload
    configuration.  The current release creates a closed planning run so the
    legacy endpoint still returns a ``run_id`` for the action console.
    """

    def __init__(
        self,
        repository: MaintenanceRepository,
        *,
        image_for_role: Callable[[str, str], str],
        resolve_target_digests: Callable[[Sequence[Mapping[str, Any]], str], Mapping[int, str]],
        preflight: Callable[[Mapping[str, Any], str, list[str] | None], bool],
        upgrade_order: Sequence[str],
        execution_enabled: bool,
        clock: Callable[[], Any] = utc_now,
    ):
        self.repository = repository
        self._image_for_role = image_for_role
        self._resolve_target_digests = resolve_target_digests
        self._preflight = preflight
        self._upgrade_order = tuple(upgrade_order)
        self._execution_enabled = bool(execution_enabled)
        self._clock = clock

    def create_legacy_upgrade_plan(
        self,
        cluster: Mapping[str, Any],
        *,
        target_version: str,
        candidates: list[str] | None,
        requested_by: str,
    ) -> UpgradePlanDispatch:
        """Create the durable Phase 4 plan used by the legacy upgrade route.

        Existing version preflight remains the compatibility authority for
        target validation, fresh observations, quorum, and snapshot checks.
        It executes before manifest resolution so an already-rejected request
        cannot make an unnecessary registry request or create a run.
        """

        major_upgrade = bool(self._preflight(dict(cluster), target_version, candidates))
        assignments = tuple(cluster.get("assignments") or ())
        if not assignments:
            raise ValueError("an upgrade plan requires at least one active assignment")
        digests = self._resolve_target_digests(assignments, target_version)
        manifest = build_manifest_for_assignments(
            assignments,
            target_version=target_version,
            image_for_role=self._image_for_role,
            target_digests=digests,
        )
        order = {role: index for index, role in enumerate(self._upgrade_order)}
        ordered = tuple(sorted(
            assignments,
            key=lambda item: (order.get(str(item["role"]), len(order)), str(item.get("node_name") or ""), int(item["id"])),
        ))
        artifact_by_assignment = {artifact.assignment_id: artifact for artifact in manifest.artifacts}
        current_versions = tuple(sorted({str((item.get("observation") or {}).get("version") or "") for item in ordered if (item.get("observation") or {}).get("version")}))
        current_version = current_versions[0] if current_versions else target_version
        now = self._clock()
        blockers = () if self._execution_enabled else ("maintenance_upgrade_execution_disabled",)
        plan_payload = {
            "operation": OperationKind.UPGRADE.value,
            "target": {
                "cluster_id": int(cluster["id"]),
                "current_versions": list(current_versions),
                "target_version": target_version,
                "major_upgrade": major_upgrade,
            },
            "execution_enabled": self._execution_enabled,
            "ordered_assignment_ids": [int(item["id"]) for item in ordered],
            "rollback_policy": {
                "stateless": "restore_prior_artifact_when_compatible",
                "elasticsearch": "recovery_required_after_new_process_starts_no_auto_downgrade",
            },
        }
        target_manifest = attach_manifest({
            "cluster_id": int(cluster["id"]),
            "target_version": target_version,
            "major_upgrade": major_upgrade,
            "execution_enabled": self._execution_enabled,
            "blockers": list(blockers),
            "ordered_assignment_ids": [int(item["id"]) for item in ordered],
        }, manifest)
        idempotency_key = manifest.manifest_hash
        existing = self.repository.get_plan_by_idempotency_key(idempotency_key)
        if existing:
            existing_manifest = manifest_from_target_manifest(existing.target_manifest)
            if (
                existing.operation_kind != OperationKind.UPGRADE.value
                or existing.target_cluster_id != int(cluster["id"])
                or existing_manifest.manifest_hash != manifest.manifest_hash
                or existing.run_id is None
            ):
                raise ValueError("immutable upgrade manifest identity conflicts with an existing maintenance plan")
            return UpgradePlanDispatch(
                run_id=existing.run_id,
                plan_id=existing.id,
                manifest_hash=manifest.manifest_hash,
                execution_enabled=bool(existing.target_manifest.get("execution_enabled")),
                blockers=tuple(existing.target_manifest.get("blockers") or ()),
            )
        connection = self.repository.connection
        connection.execute("SAVEPOINT maintenance_upgrade_plan")
        try:
            plan = self.repository.create_plan(
                operation_kind=OperationKind.UPGRADE.value,
                plan=plan_payload,
                idempotency_key=idempotency_key,
                requested_by=requested_by,
                expires_at=now + timedelta(minutes=15),
                observation={
                    "captured_at": iso_timestamp(now),
                    "current_versions": list(current_versions),
                    "preflight_accepted": True,
                },
                target_cluster_id=int(cluster["id"]),
                observed_at=iso_timestamp(now),
                target_manifest=target_manifest,
                initial_state=MaintenanceState.READY if self._execution_enabled else MaintenanceState.BLOCKED,
            )
            for sequence, assignment in enumerate(ordered, start=1):
                observation = dict(assignment.get("observation") or {})
                artifact = artifact_by_assignment[int(assignment["id"])]
                role = legacy_role_to_workload_role(str(assignment["role"]))
                process_started = bool(observation.get("running"))
                rollback = "restore_prior_artifact_when_compatible" if rollback_allowed(
                    role, process_started=process_started,
                ) else "recovery_required_no_auto_downgrade"
                step = self.repository.create_step(
                    plan_id=plan.id,
                    step_key=f"upgrade:{sequence}:{assignment['id']}",
                    sequence=sequence,
                    step_kind="upgrade-workload",
                    affected_cluster_id=int(cluster["id"]),
                    affected_assignment_id=int(assignment["id"]),
                    affected_node_id=int(assignment["node_id"]),
                    elasticsearch_node_id=str(assignment.get("node_name") or "") if role.value == "elasticsearch" else None,
                    before_observation={
                        "image": observation.get("image", ""),
                        "digest": observation.get("digest", ""),
                        "version": observation.get("version", ""),
                        "rollback": rollback,
                    },
                )
                self.repository.record_checkpoint(
                    plan_id=plan.id,
                    step_id=step.id,
                    checkpoint_key=f"upgrade:{sequence}:{assignment['id']}:before-start",
                    sequence=sequence,
                    side_effect_state=SideEffectState.NOT_STARTED,
                    payload={
                        "assignment_id": int(assignment["id"]),
                        "role": role.value,
                        "target_image": artifact.image,
                        "target_digest": artifact.digest,
                        "rollback": rollback,
                    },
                    observation={"running": process_started},
                )
            run = create_run_in_connection(
                connection,
                RunDescriptor(
                    "maintenance-upgrade-plan",
                    str(cluster.get("name") or cluster["id"]) + ":upgrade:" + target_version,
                    {
                        "maintenance_plan_id": plan.id,
                        "target_version": target_version,
                        "upgrade_manifest_hash": manifest.manifest_hash,
                        "execution_enabled": self._execution_enabled,
                    },
                ),
            )
            self.repository.attach_run_id(plan.id, run.run_id)
            finish_run_in_connection(
                connection,
                run.run_id,
                "succeeded",
                log_suffix=(
                    "Maintenance upgrade plan persisted; execution is disabled pending the Phase 4 approval gate.\n"
                    if not self._execution_enabled
                    else "Maintenance upgrade plan persisted and is awaiting the approved executor.\n"
                ),
            )
            self.repository.record_audit(
                username=requested_by,
                action="maintenance-upgrade-plan-created",
                item_id=plan.id,
                detail={
                    "cluster_id": int(cluster["id"]),
                    "target_version": target_version,
                    "manifest_hash": manifest.manifest_hash,
                    "execution_enabled": self._execution_enabled,
                    "blockers": list(blockers),
                },
            )
            connection.execute("RELEASE SAVEPOINT maintenance_upgrade_plan")
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT maintenance_upgrade_plan")
            connection.execute("RELEASE SAVEPOINT maintenance_upgrade_plan")
            raise
        return UpgradePlanDispatch(
            run_id=run.run_id,
            plan_id=plan.id,
            manifest_hash=manifest.manifest_hash,
            execution_enabled=self._execution_enabled,
            blockers=blockers,
        )


def build_manifest_for_assignments(
    assignments: Iterable[Mapping[str, Any]],
    *,
    target_version: str,
    image_for_role: Callable[[str, str], str],
    target_digests: Mapping[int, str],
) -> UpgradeManifest:
    """Resolve one immutable target artifact for every active assignment.

    Digest resolution is deliberately an input to this pure adapter.  The
    registry/download implementation remains owned by ``versions``; callers
    must not silently substitute a tag or the currently running digest.
    """

    artifacts: list[UpgradeArtifact] = []
    for assignment in assignments:
        assignment_id = int(assignment["id"])
        digest = target_digests.get(assignment_id)
        if not digest:
            raise ValueError(f"missing immutable target digest for assignment {assignment_id}")
        artifacts.append(
            UpgradeArtifact(
                assignment_id=assignment_id,
                node_id=int(assignment["node_id"]),
                role=str(assignment["role"]),
                image=image_for_role(str(assignment["role"]), target_version),
                version=target_version,
                digest=digest,
            )
        )
    if not artifacts:
        raise ValueError("an upgrade plan requires at least one active assignment")
    return build_upgrade_manifest(artifacts)


def build_upgrade_plan_preview(
    *,
    cluster_id: int,
    current_version: str,
    target_version: str,
    manifest: UpgradeManifest,
    preflight: UpgradePreflight,
) -> UpgradePlanPreview:
    """Build a persisted, execution-disabled upgrade projection."""

    if any(artifact.version != target_version for artifact in manifest.artifacts):
        raise ValueError("upgrade manifest artifacts must match the selected target version")
    blockers = validate_upgrade_transition(
        current_version=current_version,
        target_version=target_version,
        preflight=preflight,
    )
    return UpgradePlanPreview(
        cluster_id=cluster_id,
        current_version=current_version,
        target_version=target_version,
        manifest=UpgradeManifestProjection.from_manifest(manifest),
        blockers=blockers,
        # This adapter is planning-only.  A future gate may consume the same
        # projection, but must explicitly replace this field during approval.
        execution_enabled=False,
    )


def attach_manifest(target_manifest: Mapping[str, Any], manifest: UpgradeManifest) -> dict[str, Any]:
    """Return a redaction-safe maintenance ``target_manifest`` payload."""

    projection = UpgradeManifestProjection.from_manifest(manifest)
    payload = dict(target_manifest)
    payload["upgrade_manifest"] = projection.model_dump(mode="json")
    payload["upgrade_manifest_hash"] = manifest.manifest_hash
    return payload


def manifest_from_target_manifest(target_manifest: Mapping[str, Any]) -> UpgradeManifest:
    """Load and verify the immutable manifest captured in a plan record."""

    payload = target_manifest.get("upgrade_manifest")
    if not isinstance(payload, Mapping):
        raise ValueError("maintenance plan has no immutable upgrade manifest")
    projection = UpgradeManifestProjection.model_validate(payload)
    expected_hash = target_manifest.get("upgrade_manifest_hash")
    if expected_hash is not None and expected_hash != projection.manifest_hash:
        raise ValueError("maintenance plan upgrade manifest hash mismatch")
    return projection.to_manifest()


__all__ = [
    "UpgradeManifestProjection",
    "UpgradePlanDispatch",
    "UpgradePlanPreview",
    "MaintenanceUpgradePlanningService",
    "attach_manifest",
    "build_manifest_for_assignments",
    "build_upgrade_plan_preview",
    "manifest_from_target_manifest",
]
