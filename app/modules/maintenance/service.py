from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, field_validator

from .lifecycle import MaintenanceState, canonical_hash, redact_structure
from .models import (
    AvailabilityMode,
    ClusterObservation,
    FrozenModel,
    HostObservation,
    ImpactManifest,
    MaintenanceBackend,
    MaintenancePolicy,
    ObservationSnapshot,
    OperationKind,
    PlanStep,
    PlanningTarget,
    PolicyObservation,
    PredicateOutcome,
    RevisionObservation,
    RollbackBoundary,
    SourceObservation,
    WorkloadObservation,
    MaintenancePlanPreviewInput,
    PreviewOperation,
)
from .planning import canonical_hash, compile_plan
from .safety import calculate_impact, evaluate_predicates
from .store import IdempotencyConflict, MaintenanceRepository, PlanRecord, iso_timestamp, utc_now


class HostRebootPlanningData(FrozenModel):
    target_node_id: int = Field(ge=1)
    captured_at: datetime
    capability_revision: str = Field(min_length=1, max_length=128)
    sources: tuple[SourceObservation, ...]
    hosts: tuple[HostObservation, ...]
    clusters: tuple[ClusterObservation, ...]
    workloads: tuple[WorkloadObservation, ...]
    assignment_revisions: tuple[RevisionObservation, ...]
    conflicting_operations: tuple[str, ...] = ()

    @field_validator("captured_at")
    @classmethod
    def captured_at_is_aware(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured_at must include a timezone")
        return value


class HostRebootPlanRequest(FrozenModel):
    operation: Literal["reboot"] = "reboot"
    reason: str = Field(min_length=1, max_length=512)
    availability_mode: AvailabilityMode = AvailabilityMode.ZERO_IMPACT
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value):
        reason = value.strip()
        if not reason:
            raise ValueError("reason must not be blank")
        return reason


def generic_preview_idempotency_key(
    request: MaintenancePlanPreviewInput,
    *,
    requested_by: str,
) -> str:
    """Return the stable idempotency key for a public preview request."""
    return request.idempotency_key or canonical_hash({
        "operation": request.operation.value,
        "node_id": getattr(request, "node_id", None),
        "cluster_id": getattr(request, "cluster_id", None),
        "assignment_ids": list(getattr(request, "assignment_ids", ())),
        "current_version": getattr(request, "current_version", None),
        "target_version": getattr(request, "target_version", None),
        "reason": request.reason,
        "availability_mode": request.availability_mode.value,
        "requested_by": requested_by,
    })


def same_generic_preview_request(record: PlanRecord, request: MaintenancePlanPreviewInput) -> bool:
    """Check a repeat request without recollecting live observations."""
    target = record.plan.get("target", {}) if isinstance(record.plan, dict) else {}
    return bool(
        target.get("operation") == MaintenancePlanningService._internal_operation(request.operation.value)
        and target.get("reason") == request.reason
        and target.get("availability_mode") == request.availability_mode.value
        and target.get("cluster_id") == getattr(request, "cluster_id", None)
        and tuple(target.get("assignment_ids", ())) == tuple(getattr(request, "assignment_ids", ()))
        and target.get("current_version") == getattr(request, "current_version", None)
        and target.get("target_version") == getattr(request, "target_version", None)
    )


def build_host_reboot_snapshot(
    data: HostRebootPlanningData,
    policies: tuple[PolicyObservation, ...] = (),
) -> ObservationSnapshot:
    if not any(item.node_id == data.target_node_id for item in data.hosts):
        raise ValueError("A target host observation is required to plan a reboot")
    return ObservationSnapshot(
        captured_at=data.captured_at,
        capability_revision=data.capability_revision,
        sources=data.sources,
        hosts=data.hosts,
        clusters=data.clusters,
        workloads=data.workloads,
        assignment_revisions=data.assignment_revisions,
        policies=policies,
        conflicting_operations=data.conflicting_operations,
    )


def _affected_cluster_ids(data: HostRebootPlanningData) -> tuple[int, ...]:
    return tuple(sorted({
        item.cluster_id
        for item in data.workloads
        if item.node_id == data.target_node_id and item.expected_running
    }))


def _effective_policy(policies: tuple[PolicyObservation, ...]) -> MaintenancePolicy:
    if not policies:
        return MaintenancePolicy()
    values = [item.policy for item in policies]
    allocation_delays = [item.restart_allocation_delay_seconds for item in values if item.restart_allocation_delay_seconds is not None]
    return MaintenancePolicy(
        max_unavailable=min(item.max_unavailable for item in values),
        minimum_master_eligible="quorum",
        minimum_data_per_tier=max(item.minimum_data_per_tier for item in values),
        minimum_kibana=max(item.minimum_kibana for item in values),
        minimum_fleet_server=max(item.minimum_fleet_server for item in values),
        minimum_logstash=max(item.minimum_logstash for item in values),
        minimum_coordinating=max(item.minimum_coordinating for item in values),
        allow_agent_interruption="block" if any(item.allow_agent_interruption == "block" for item in values) else "true-with-warning",
        required_cluster_health="green" if any(item.required_cluster_health == "green" for item in values) else "yellow",
        allocation_guard="primaries-for-data" if any(item.allocation_guard == "primaries-for-data" for item in values) else "none",
        observation_max_age_seconds=min(item.observation_max_age_seconds for item in values),
        restart_allocation_delay_seconds=max(allocation_delays) if allocation_delays else None,
        host_return_timeout_seconds=max(item.host_return_timeout_seconds for item in values),
        workload_ready_timeout_seconds=max(item.workload_ready_timeout_seconds for item in values),
        plan_validity_seconds=min(item.plan_validity_seconds for item in values),
    )


def _impact_for_policies(
    snapshot: ObservationSnapshot,
    target: PlanningTarget,
    policies: tuple[PolicyObservation, ...],
    effective_policy: MaintenancePolicy,
) -> ImpactManifest:
    effective = calculate_impact(snapshot, target, effective_policy)
    if not policies:
        return effective
    policy_by_cluster = {item.cluster_id: item.policy for item in policies}
    clusters = []
    for cluster_id in effective.affected_cluster_ids:
        policy = policy_by_cluster.get(cluster_id, MaintenancePolicy())
        clusters.append(calculate_impact(snapshot, target, policy).cluster(cluster_id))
    return ImpactManifest(
        target_node_id=effective.target_node_id,
        affected_cluster_ids=effective.affected_cluster_ids,
        affected_assignment_ids=effective.affected_assignment_ids,
        clusters=tuple(clusters),
    )


def _backend(snapshot: ObservationSnapshot, cluster_ids: tuple[int, ...]) -> MaintenanceBackend:
    backends = {
        item.backend for item in snapshot.clusters
        if item.cluster_id in cluster_ids
    }
    return next(iter(backends)) if len(backends) == 1 else MaintenanceBackend.NONE


def _steps(snapshot: ObservationSnapshot, target: PlanningTarget, impact: ImpactManifest) -> tuple[PlanStep, ...]:
    steps = []

    def add(kind: str, summary: str, **scope):
        steps.append(PlanStep(sequence=len(steps) + 1, kind=kind, summary=summary, **scope))

    add("acquire-maintenance-locks", "Acquire host, cluster, and assignment maintenance locks.", node_id=target.node_id)
    add("refresh-observations", "Refresh host, runtime, membership, and Elasticsearch observations.", node_id=target.node_id)
    add("evaluate-safety-predicates", "Re-evaluate every safety predicate against fresh observations.", node_id=target.node_id)
    for cluster_id in impact.affected_cluster_ids:
        cluster = snapshot.cluster(cluster_id)
        if cluster:
            add(
                "prepare-elasticsearch",
                "Prepare the affected Elasticsearch cluster for a one-host disruption.",
                cluster_id=cluster_id,
                node_id=target.node_id,
            )
    add("stage-host-executor", "Stage the signed one-shot host maintenance executor.", node_id=target.node_id)
    add("reboot-host", "Reboot the selected host through the staged executor.", node_id=target.node_id)
    add("verify-host-return", "Verify host identity, SSH, Podman, Quadlets, and systemd after boot.", node_id=target.node_id)
    for assignment_id in impact.affected_assignment_ids:
        workload = next(item for item in snapshot.workloads if item.assignment_id == assignment_id)
        add(
            "verify-workload",
            "Verify the workload returned with its expected identity and readiness.",
            cluster_id=workload.cluster_id,
            assignment_id=assignment_id,
            node_id=workload.node_id,
        )
    for cluster_id in impact.affected_cluster_ids:
        add(
            "restore-cluster-maintenance-state",
            "Restore captured cluster settings and clear temporary lifecycle markers.",
            cluster_id=cluster_id,
            node_id=target.node_id,
        )
    add("release-maintenance-locks", "Release all maintenance locks after verified cleanup.", node_id=target.node_id)
    return tuple(steps)


def _same_request(record: PlanRecord, data: HostRebootPlanningData, request: HostRebootPlanRequest) -> bool:
    target = record.plan.get("target") if isinstance(record.plan, dict) else None
    return bool(
        record.operation_kind == OperationKind.REBOOT.value
        and record.target_node_id == data.target_node_id
        and isinstance(target, dict)
        and target.get("reason") == request.reason
        and target.get("availability_mode") == request.availability_mode.value
    )


def _display_title(value: str) -> str:
    output = []
    for index, character in enumerate(value):
        if index and character.isupper() and value[index - 1].islower():
            output.append(" ")
        output.append(character)
    return "".join(output)


def serialize_plan_preview(record: PlanRecord, *, now: datetime | None = None) -> dict:
    compiled = record.plan
    observation = compiled.get("observation", {}) if isinstance(compiled, dict) else {}
    sources = observation.get("sources", []) if isinstance(observation, dict) else []
    policies = observation.get("policies", []) if isinstance(observation, dict) else []
    hosts = observation.get("hosts", []) if isinstance(observation, dict) else []
    clusters = observation.get("clusters", []) if isinstance(observation, dict) else []
    workloads = observation.get("workloads", []) if isinstance(observation, dict) else []
    impact = compiled.get("impact", {}) if isinstance(compiled, dict) else {}
    cluster_impacts = impact.get("clusters", []) if isinstance(impact, dict) else []
    target = compiled.get("target", {}) if isinstance(compiled, dict) else {}
    target_node_id = target.get("node_id") if isinstance(target, dict) else record.target_node_id
    target_host = next(
        (item for item in hosts if isinstance(item, dict) and item.get("node_id") == target_node_id),
        {},
    )
    host_names = {
        item.get("node_id"): item.get("name") or f"node-{item.get('node_id')}"
        for item in hosts if isinstance(item, dict)
    }
    cluster_names = {
        item.get("cluster_id"): item.get("configured_name") or f"cluster-{item.get('cluster_id')}"
        for item in clusters if isinstance(item, dict)
    }
    affected_cluster_ids = impact.get("affected_cluster_ids", []) if isinstance(impact, dict) else []
    affected_assignment_ids = set(impact.get("affected_assignment_ids", []) if isinstance(impact, dict) else [])
    cluster_impact_by_id = {
        item.get("cluster_id"): item for item in cluster_impacts if isinstance(item, dict)
    }
    current_time = (now or utc_now()).astimezone(timezone.utc)
    # The durable column is authoritative: tests, recovery, and retention may
    # update it after the immutable compiled payload was created.
    expires_at = record.expires_at
    expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    fresh_predicate = next(
        (
            item for item in compiled.get("predicates", [])
            if isinstance(item, dict) and item.get("identifier") == "FreshRuntimeObservation"
        ),
        None,
    )
    if current_time >= expiry:
        freshness_state = "expired"
        freshness_detail = "The plan validity window has expired; create a fresh preview."
    elif fresh_predicate and fresh_predicate.get("outcome") == "blocked":
        freshness_state = "stale"
        freshness_detail = fresh_predicate.get("remediation") or "Refresh required observations."
    else:
        freshness_state = "fresh"
        freshness_detail = "All required planning observations are within policy age."

    workload_view = []
    endpoint_view = []
    for item in workloads:
        if not isinstance(item, dict) or item.get("assignment_id") not in affected_assignment_ids:
            continue
        cluster_impact = cluster_impact_by_id.get(item.get("cluster_id"), {})
        lost_roles = set(cluster_impact.get("endpoints_lost", [])) if isinstance(cluster_impact, dict) else set()
        availability = "unavailable" if item.get("role") in lost_roles else "degraded"
        workload_view.append({
            "id": item.get("assignment_id"),
            "name": item.get("name") or f"{item.get('role')}-{item.get('assignment_id')}",
            "role": item.get("role"),
            "host": host_names.get(item.get("node_id"), f"node-{item.get('node_id')}"),
            "availability": availability,
        })
        if item.get("endpoint_required"):
            endpoint_view.append({
                "id": item.get("assignment_id"),
                "name": _display_title(str(item.get("role", "endpoint"))).title(),
                "availability": availability,
                "detail": (
                    "The endpoint would be unavailable during host maintenance."
                    if availability == "unavailable"
                    else "Other ready instances preserve the endpoint."
                ),
            })

    master_impacts = [
        item for item in cluster_impacts
        if isinstance(item, dict) and item.get("master_total", 0) > 0 and item.get("affected_assignment_ids")
    ]
    master_impact = min(
        master_impacts,
        key=lambda item: item.get("master_available_after", 0) - item.get("master_required", 0),
        default=None,
    )
    data_tiers = []
    for cluster_impact in cluster_impacts:
        if not isinstance(cluster_impact, dict):
            continue
        for tier in cluster_impact.get("data_tiers", []):
            if not isinstance(tier, dict):
                continue
            label = str(tier.get("tier"))
            if len(affected_cluster_ids) > 1:
                label = f"{cluster_names.get(cluster_impact.get('cluster_id'), cluster_impact.get('cluster_id'))}/{label}"
            data_tiers.append({
                "tier": label,
                "availableAfter": tier.get("available_after", 0),
                "total": tier.get("available_before", 0),
                "minimumRequired": tier.get("required", 0),
                "safe": tier.get("available_after", 0) >= tier.get("required", 0),
            })
    agent_count = sum(
        item.get("agent_interruptions", 0) for item in cluster_impacts if isinstance(item, dict)
    )
    singleton_services = [
        {
            "name": _display_title(role).title(),
            "estimatedOutage": f"up to {compiled.get('policy', {}).get('host_return_timeout_seconds', 900)} seconds",
        }
        for item in cluster_impacts if isinstance(item, dict)
        for role in item.get("endpoints_lost", [])
    ]
    policy_revision = max(
        (item.get("revision", 0) for item in policies if isinstance(item, dict)),
        default=0,
    )
    predicate_view = []
    for item in compiled.get("predicates", []):
        if not isinstance(item, dict):
            continue
        predicate_view.append({
            "id": item.get("identifier"),
            "title": _display_title(str(item.get("identifier", "Predicate"))),
            "outcome": "blocking" if item.get("outcome") == "blocked" else item.get("outcome"),
            "evidence": item.get("evidence_summary", ""),
            "remediation": item.get("remediation") or None,
            "observedAt": item.get("observed_at"),
            "forceable": item.get("forceable", False),
        })
    step_view = []
    for item in compiled.get("steps", []):
        if not isinstance(item, dict):
            continue
        scope = item.get("assignment_id") or item.get("cluster_id") or item.get("node_id")
        step_view.append({
            "id": f"preview:{item.get('sequence')}:{item.get('kind')}",
            "sequence": item.get("sequence"),
            "title": _display_title(str(item.get("kind", "step")).replace("-", " ")).title(),
            "description": item.get("summary", ""),
            "state": "pending",
            "target": str(scope) if scope is not None else None,
        })
    operation_kind = str(target.get("operation", record.operation_kind)) if isinstance(target, dict) else record.operation_kind
    target_cluster_id = target.get("cluster_id") if isinstance(target, dict) else record.target_cluster_id
    target_assignment_ids = target.get("assignment_ids", ()) if isinstance(target, dict) else ()
    if target_cluster_id is not None:
        target_view = {
            "kind": "cluster",
            "name": cluster_names.get(target_cluster_id, f"cluster-{target_cluster_id}"),
        }
    elif target_assignment_ids:
        assignment = next((item for item in workloads if isinstance(item, dict) and item.get("assignment_id") == target_assignment_ids[0]), {})
        target_view = {
            "kind": "workload",
            "name": assignment.get("name") or f"assignment-{target_assignment_ids[0]}",
        }
    else:
        target_view = {
            "kind": "host",
            "name": target_host.get("name") or f"node-{target_node_id}",
        }
    operation_label = {
        "reboot": "Reboot host",
        "manual_maintenance": "Manual maintenance",
        "resource_change": "Resource change",
        "settings_change": "Cluster settings",
        "zoning_change": "Zoning change",
        "workload_restart": "Workload restart",
        "apply": "Apply workload",
        "detach": "Detach workload",
        "purge": "Purge workload",
        "download": "Download images",
        "upgrade": "Upgrade cluster",
    }.get(operation_kind, _display_title(operation_kind.replace("_", " ")).title())
    view = {
        "header": {
            "planId": record.id,
            "state": record.lifecycle_state.value,
            "target": target_view,
            "operation": operation_label,
            "reason": target.get("reason", "") if isinstance(target, dict) else "",
            "requester": record.requested_by,
            "createdAt": compiled.get("created_at", record.created_at),
            "freshness": {
                "state": freshness_state,
                "observedAt": observation.get("captured_at") if isinstance(observation, dict) else None,
                "expiresAt": expires_at,
                "detail": freshness_detail,
            },
            "policy": {
                "name": "Effective maintenance policy",
                "revision": policy_revision,
                "availabilityMode": target.get("availability_mode", "zero-impact") if isinstance(target, dict) else "zero-impact",
            },
        },
        "impact": {
            "clusters": [
                {"id": cluster_id, "name": cluster_names.get(cluster_id, f"cluster-{cluster_id}")}
                for cluster_id in affected_cluster_ids
            ],
            "workloads": workload_view,
            "endpoints": endpoint_view,
            "masterQuorum": (
                {
                    "availableAfter": master_impact.get("master_available_after", 0),
                    "total": master_impact.get("master_total", 0),
                    "required": master_impact.get("master_required", 0),
                    "preserved": master_impact.get("master_available_after", 0) >= master_impact.get("master_required", 0),
                }
                if master_impact else None
            ),
            "dataTiers": data_tiers,
            "agents": {
                "affected": agent_count,
                "interruptionExpected": agent_count > 0,
            },
            "singletonServices": singleton_services,
        },
        "predicates": predicate_view,
        "steps": step_view,
        "statusDetail": (
            "Planning is complete and no remote changes have been made."
            if record.lifecycle_state == MaintenanceState.READY
            else "Planning is blocked and no remote changes have been made."
        ),
        "execution_enabled": False,
    }
    return redact_structure({
        "plan_id": record.id,
        "plan_hash": record.plan_hash,
        "lifecycle_state": record.lifecycle_state.value,
        "view": view,
    })


class MaintenancePlanningService:
    def __init__(
        self,
        repository: MaintenanceRepository,
        *,
        clock: Callable[[], datetime] = utc_now,
    ):
        self.repository = repository
        self.clock = clock

    def create_host_reboot_preview(
        self,
        data: HostRebootPlanningData,
        request: HostRebootPlanRequest,
        *,
        requested_by: str,
    ) -> dict:
        existing = self.repository.get_plan_by_idempotency_key(request.idempotency_key)
        if existing:
            if not _same_request(existing, data, request):
                raise IdempotencyConflict("Idempotency key was already used for a different maintenance preview")
            return serialize_plan_preview(existing, now=self.clock())

        affected_cluster_ids = _affected_cluster_ids(data)
        policy_observations = []
        for cluster_id in affected_cluster_ids:
            record = self.repository.get_policy(cluster_id)
            policy_observations.append(PolicyObservation(
                cluster_id=cluster_id,
                revision=record.revision if record else 0,
                policy=MaintenancePolicy.model_validate(record.policy if record else {}),
            ))
        policy_observations = tuple(policy_observations)
        snapshot = build_host_reboot_snapshot(data, policy_observations)
        effective_policy = _effective_policy(policy_observations)
        target = PlanningTarget(
            operation=OperationKind.REBOOT,
            node_id=data.target_node_id,
            reason=request.reason,
            availability_mode=request.availability_mode,
        )
        impact = _impact_for_policies(snapshot, target, policy_observations, effective_policy)
        now = self.clock().astimezone(timezone.utc)
        predicates = evaluate_predicates(snapshot, target, effective_policy, impact, now=now)
        steps = _steps(snapshot, target, impact)
        reboot_step = next(item for item in steps if item.kind == "reboot-host")
        compiled = compile_plan(
            target=target,
            policy=effective_policy,
            policy_revision=max((item.revision for item in policy_observations), default=0),
            backend=_backend(snapshot, impact.affected_cluster_ids),
            observation=snapshot,
            predicates=predicates,
            impact=impact,
            steps=steps,
            rollback_boundaries=(RollbackBoundary(
                before_step=reboot_step.sequence,
                behavior="Before reboot, abort safely and remove only staged maintenance artifacts. After reboot begins, operating-system rollback is unavailable.",
            ),),
            created_at=now,
            idempotency_key=request.idempotency_key,
        )
        initial_state = (
            MaintenanceState.BLOCKED
            if any(item.outcome == PredicateOutcome.BLOCKED for item in predicates)
            else MaintenanceState.READY
        )
        revision_map = {item.assignment_id: item.revision for item in snapshot.assignment_revisions}
        target_revisions = [
            {"assignment_id": assignment_id, "revision": revision_map.get(assignment_id)}
            for assignment_id in impact.affected_assignment_ids
        ]
        compiled_payload = compiled.model_dump(mode="json")

        connection = self.repository.connection
        connection.execute("SAVEPOINT maintenance_preview_create")
        try:
            persisted = self.repository.create_plan(
                operation_kind=OperationKind.REBOOT.value,
                plan=compiled_payload,
                idempotency_key=request.idempotency_key,
                requested_by=requested_by,
                expires_at=compiled.expires_at,
                observation=compiled.observation.model_dump(mode="json"),
                target_node_id=data.target_node_id,
                expected_policy_revision=compiled.policy_revision if len(policy_observations) == 1 else None,
                expected_assignment_revision=(
                    target_revisions[0]["revision"] if len(target_revisions) == 1 else None
                ),
                observed_at=iso_timestamp(snapshot.captured_at),
                target_manifest={
                    "authoritative_plan_hash": compiled.plan_hash,
                    "affected_cluster_ids": list(impact.affected_cluster_ids),
                    "assignment_revisions": target_revisions,
                    "policy_revisions": [
                        {"cluster_id": item.cluster_id, "revision": item.revision}
                        for item in policy_observations
                    ],
                    "cluster_backends": [
                        {
                            "cluster_id": item.cluster_id,
                            "provider_type": item.provider_type.value,
                            "backend": item.backend.value,
                        }
                        for item in snapshot.clusters if item.cluster_id in impact.affected_cluster_ids
                    ],
                },
                initial_state=initial_state,
                authoritative_plan_hash=compiled.plan_hash,
            )
            for step in compiled.steps:
                self.repository.create_step(
                    plan_id=persisted.id,
                    step_key=f"preview:{step.sequence}:{step.kind}",
                    sequence=step.sequence,
                    step_kind=step.kind,
                    affected_cluster_id=step.cluster_id,
                    affected_assignment_id=step.assignment_id,
                    affected_node_id=step.node_id,
                )
            self.repository.record_audit(
                username=requested_by,
                action="maintenance-plan-preview-created",
                item_id=persisted.id,
                detail={
                    "operation": OperationKind.REBOOT.value,
                    "target_node_id": data.target_node_id,
                    "result": initial_state.value,
                    "plan_hash": compiled.plan_hash,
                },
            )
            connection.execute("RELEASE SAVEPOINT maintenance_preview_create")
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT maintenance_preview_create")
            connection.execute("RELEASE SAVEPOINT maintenance_preview_create")
            raise
        return serialize_plan_preview(persisted, now=self.clock())

    def create_generic_preview(
        self,
        data: HostRebootPlanningData,
        request: MaintenancePlanPreviewInput,
        *,
        requested_by: str,
    ) -> dict:
        """Compile and persist a non-mutating preview for any public target.

        This intentionally shares the reboot observation and persistence
        contracts. It only writes the maintenance plan, step, and audit rows;
        no run, lock, host, workload, cluster, or remote state is changed.
        """
        public_operation = request.operation.value
        idempotency_key = generic_preview_idempotency_key(request, requested_by=requested_by)
        existing = self.repository.get_plan_by_idempotency_key(idempotency_key)
        if existing:
            if not same_generic_preview_request(existing, request):
                raise IdempotencyConflict("Idempotency key was already used for a different maintenance preview")
            return serialize_plan_preview(existing, now=self.clock())

        operation = self._internal_operation(public_operation)
        target = PlanningTarget(
            operation=operation,
            # The anchor host is an observation scope, not a requested
            # mutation target. Keeping it in the compiled target preserves
            # existing predicate and response contracts.
            node_id=data.target_node_id,
            cluster_id=getattr(request, "cluster_id", None),
            assignment_ids=tuple(getattr(request, "assignment_ids", ())),
            reason=request.reason,
            availability_mode=request.availability_mode,
            current_version=getattr(request, "current_version", None),
            target_version=getattr(request, "target_version", None),
        )
        affected_cluster_ids = set(item.cluster_id for item in data.workloads if item.node_id == data.target_node_id)
        affected_cluster_ids.update(item.cluster_id for item in data.workloads if item.assignment_id in target.assignment_ids)
        if target.cluster_id is not None:
            affected_cluster_ids.add(target.cluster_id)
        policies = tuple(
            PolicyObservation(
                cluster_id=cluster_id,
                revision=(record.revision if (record := self.repository.get_policy(cluster_id)) else 0),
                policy=MaintenancePolicy.model_validate(record.policy if record else {}),
            )
            for cluster_id in sorted(affected_cluster_ids)
        )
        snapshot = build_host_reboot_snapshot(data, policies)
        effective_policy = _effective_policy(policies)
        impact = _impact_for_policies(snapshot, target, policies, effective_policy)
        now = self.clock().astimezone(timezone.utc)
        predicates = evaluate_predicates(snapshot, target, effective_policy, impact, now=now)
        steps = self._generic_steps(target, impact)
        compiled = compile_plan(
            target=target,
            policy=effective_policy,
            policy_revision=max((item.revision for item in policies), default=0),
            backend=_backend(snapshot, impact.affected_cluster_ids),
            observation=snapshot,
            predicates=predicates,
            impact=impact,
            steps=steps,
            rollback_boundaries=(RollbackBoundary(
                before_step=max(1, next((item.sequence for item in steps if item.kind == "apply-preview-boundary"), 1)),
                behavior="No remote side effect has started; discard this preview and create a fresh plan.",
            ),),
            created_at=now,
            idempotency_key=idempotency_key,
        )
        state = MaintenanceState.BLOCKED if any(item.outcome == PredicateOutcome.BLOCKED for item in predicates) else MaintenanceState.READY
        revision_map = {item.assignment_id: item.revision for item in snapshot.assignment_revisions}
        target_revisions = [
            {"assignment_id": assignment_id, "revision": revision_map.get(assignment_id)}
            for assignment_id in target.assignment_ids
        ]
        assignment_cluster_ids = {
            item.cluster_id for item in snapshot.workloads if item.assignment_id in target.assignment_ids
        }
        persisted_cluster_id = target.cluster_id
        if persisted_cluster_id is None and len(assignment_cluster_ids) == 1:
            persisted_cluster_id = next(iter(assignment_cluster_ids))
        payload = compiled.model_dump(mode="json")
        connection = self.repository.connection
        connection.execute("SAVEPOINT maintenance_generic_preview_create")
        try:
            persisted = self.repository.create_plan(
                operation_kind=operation.value,
                plan=payload,
                idempotency_key=idempotency_key,
                requested_by=requested_by,
                expires_at=compiled.expires_at,
                observation=compiled.observation.model_dump(mode="json"),
                target_node_id=data.target_node_id,
                target_cluster_id=persisted_cluster_id,
                target_assignment_id=(target.assignment_ids[0] if len(target.assignment_ids) == 1 else None),
                expected_policy_revision=(compiled.policy_revision if len(policies) == 1 else None),
                expected_assignment_revision=(target_revisions[0]["revision"] if len(target_revisions) == 1 else None),
                observed_at=iso_timestamp(snapshot.captured_at),
                target_manifest={
                    "authoritative_plan_hash": compiled.plan_hash,
                    "affected_cluster_ids": list(impact.affected_cluster_ids),
                    "assignment_revisions": target_revisions,
                    "public_operation": public_operation,
                },
                initial_state=state,
                authoritative_plan_hash=compiled.plan_hash,
            )
            for step in compiled.steps:
                self.repository.create_step(
                    plan_id=persisted.id,
                    step_key=f"preview:{step.sequence}:{step.kind}",
                    sequence=step.sequence,
                    step_kind=step.kind,
                    affected_cluster_id=step.cluster_id,
                    affected_assignment_id=step.assignment_id,
                    affected_node_id=step.node_id,
                )
            self.repository.record_audit(
                username=requested_by,
                action="maintenance-plan-preview-created",
                item_id=persisted.id,
                detail={"operation": public_operation, "result": state.value, "plan_hash": compiled.plan_hash},
            )
            connection.execute("RELEASE SAVEPOINT maintenance_generic_preview_create")
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT maintenance_generic_preview_create")
            connection.execute("RELEASE SAVEPOINT maintenance_generic_preview_create")
            raise
        return serialize_plan_preview(persisted, now=self.clock())

    @staticmethod
    def _internal_operation(value: str) -> OperationKind:
        return {
            PreviewOperation.REBOOT.value: OperationKind.REBOOT,
            PreviewOperation.MANUAL_MAINTENANCE.value: OperationKind.MANUAL_MAINTENANCE,
            PreviewOperation.RESOURCE_CHANGE.value: OperationKind.RESOURCE_CHANGE,
            PreviewOperation.CLUSTER_SETTINGS.value: OperationKind.SETTINGS_CHANGE,
            PreviewOperation.ZONING.value: OperationKind.ZONING_CHANGE,
            PreviewOperation.APPLY.value: OperationKind.APPLY,
            PreviewOperation.DETACH.value: OperationKind.DETACH,
            PreviewOperation.PURGE.value: OperationKind.PURGE,
            PreviewOperation.DOWNLOAD.value: OperationKind.DOWNLOAD,
            PreviewOperation.UPGRADE.value: OperationKind.UPGRADE,
        }[value]

    @staticmethod
    def _generic_steps(target: PlanningTarget, impact: ImpactManifest) -> tuple[PlanStep, ...]:
        steps = [
            PlanStep(sequence=1, kind="acquire-maintenance-locks", summary="Acquire locks after this preview is explicitly approved.", node_id=target.node_id),
            PlanStep(sequence=2, kind="refresh-observations", summary="Refresh host, workload, and cluster observations.", node_id=target.node_id),
            PlanStep(sequence=3, kind="evaluate-safety-predicates", summary="Re-evaluate safety predicates against fresh observations.", node_id=target.node_id),
        ]
        scope = target.cluster_id or (target.assignment_ids[0] if target.assignment_ids else target.node_id)
        steps.append(PlanStep(sequence=4, kind="apply-preview-boundary", summary="Validate the selected operation without performing it.", cluster_id=target.cluster_id, assignment_id=(target.assignment_ids[0] if target.assignment_ids else None), node_id=target.node_id))
        steps.append(PlanStep(sequence=5, kind="release-maintenance-locks", summary="Release any locks if execution is later approved.", node_id=target.node_id))
        return tuple(steps)
