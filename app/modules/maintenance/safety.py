from __future__ import annotations

from datetime import datetime
from typing import Iterable

from app.modules.maintenance.models import (
    AvailabilityMode,
    BudgetViolation,
    ClusterImpact,
    ClusterObservation,
    EvaluationStage,
    ImpactManifest,
    MaintenancePolicy,
    ObservationSnapshot,
    OperationKind,
    PlanningTarget,
    PredicateId,
    PredicateOutcome,
    PredicateResult,
    PredicateSeverity,
    RoleAvailability,
    SourceStatus,
    TierAvailability,
    WorkloadObservation,
)


ELASTICSEARCH_ROLES = frozenset(("master", "hot", "warm", "ml", "ingest", "coordinating"))
SERVICE_POLICY_FIELDS = {
    "coordinating": "minimum_coordinating",
    "fleet-server": "minimum_fleet_server",
    "kibana": "minimum_kibana",
    "logstash": "minimum_logstash",
}

PREDICATE_ORDER = (
    PredicateId.HOST_ENABLED,
    PredicateId.HOST_REACHABLE,
    PredicateId.NO_CONFLICTING_OPERATION,
    PredicateId.MEMBERSHIP_READY,
    PredicateId.FRESH_RUNTIME_OBSERVATION,
    PredicateId.EXPECTED_CLUSTER_IDENTITY,
    PredicateId.SUPPORTED_NODE_LIFECYCLE_MODE,
    PredicateId.CLUSTER_HEALTH,
    PredicateId.NO_SHARD_MOVEMENT,
    PredicateId.NO_LAST_SHARD_COPY,
    PredicateId.PRIMARY_PROMOTION_SAFETY,
    PredicateId.ALLOCATION_SETTING_CAPTURED,
    PredicateId.MASTER_QUORUM,
    PredicateId.ROLE_AVAILABILITY_BUDGET,
    PredicateId.DISK_WATERMARKS_SAFE,
    PredicateId.TARGET_ARTIFACT_READY,
    PredicateId.VERSION_TRANSITION_SUPPORTED,
    PredicateId.SNAPSHOT_RECOVERY_READY,
    PredicateId.NO_STALE_SHUTDOWN_RECORD,
)

HARD_PREDICATES = frozenset((
    PredicateId.NO_LAST_SHARD_COPY,
    PredicateId.MASTER_QUORUM,
    PredicateId.EXPECTED_CLUSTER_IDENTITY,
    PredicateId.PRIMARY_PROMOTION_SAFETY,
    PredicateId.ALLOCATION_SETTING_CAPTURED,
    PredicateId.VERSION_TRANSITION_SUPPORTED,
))


def _selected_workloads(snapshot: ObservationSnapshot, target: PlanningTarget) -> tuple[WorkloadObservation, ...]:
    if target.assignment_ids:
        selected = [item for item in snapshot.workloads if item.assignment_id in target.assignment_ids]
    elif target.operation == OperationKind.REBOOT and target.node_id is not None:
        selected = [item for item in snapshot.workloads if item.node_id == target.node_id]
    elif target.operation == OperationKind.UPGRADE and target.cluster_id is not None:
        selected = [item for item in snapshot.workloads if item.cluster_id == target.cluster_id]
    else:
        selected = []
    return tuple(sorted(selected, key=lambda item: item.assignment_id))


def _minimum_masters(policy: MaintenancePolicy, total: int) -> int:
    if not total:
        return 0
    if policy.minimum_master_eligible == "quorum":
        return total // 2 + 1
    return policy.minimum_master_eligible


def _violation(identifier: str, summary: str, remediation: str) -> BudgetViolation:
    return BudgetViolation(identifier=identifier, summary=summary, remediation=remediation)


def calculate_impact(
    snapshot: ObservationSnapshot,
    target: PlanningTarget,
    policy: MaintenancePolicy,
) -> ImpactManifest:
    selected = _selected_workloads(snapshot, target)
    selected_ids = {item.assignment_id for item in selected}
    affected_cluster_ids = {item.cluster_id for item in selected}
    if target.cluster_id is not None:
        affected_cluster_ids.add(target.cluster_id)

    cluster_impacts = []
    simultaneous = target.operation == OperationKind.REBOOT
    for cluster_id in sorted(affected_cluster_ids):
        cluster_workloads = tuple(item for item in snapshot.workloads if item.cluster_id == cluster_id and item.expected_running)
        targeted = tuple(item for item in cluster_workloads if item.assignment_id in selected_ids)
        cluster_observation = snapshot.cluster(cluster_id)
        existing_unavailable = sum(not item.ready for item in cluster_workloads)
        targeted_ready = sum(item.ready for item in targeted)
        planned_unavailable = targeted_ready if simultaneous else min(1, targeted_ready)
        total_unavailable_after = existing_unavailable + planned_unavailable
        violations = []
        if total_unavailable_after > policy.max_unavailable:
            violations.append(_violation(
                "max_unavailable",
                f"Cluster {cluster_id} would have {total_unavailable_after} unavailable workloads; policy allows {policy.max_unavailable}.",
                "Restore unavailable workloads or increase redundancy before planning this disruption.",
            ))

        observed_master_total = cluster_observation.master_eligible_total if cluster_observation else 0
        observed_master_available = cluster_observation.master_eligible_available if cluster_observation else 0
        targeted_available_masters = sum(item.master_eligible and item.ready for item in targeted)
        if not simultaneous:
            targeted_available_masters = min(1, targeted_available_masters)
        master_available_after = max(0, observed_master_available - targeted_available_masters)
        master_required = _minimum_masters(policy, observed_master_total)
        if targeted_available_masters and master_available_after < master_required:
            violations.append(_violation(
                "master_quorum",
                f"Cluster {cluster_id} would retain {master_available_after} of {master_required} required master-eligible nodes.",
                "Add or restore master-eligible capacity before disrupting this host.",
            ))

        active_tiers = sorted({tier for item in cluster_workloads for tier in item.data_tiers})
        tier_availability = []
        for tier in active_tiers:
            before = sum(item.ready and tier in item.data_tiers for item in cluster_workloads)
            reduction = sum(item.ready and tier in item.data_tiers for item in targeted)
            after = before - (reduction if simultaneous else min(1, reduction))
            item = TierAvailability(
                tier=tier,
                available_before=before,
                available_after=max(0, after),
                required=policy.minimum_data_per_tier,
            )
            tier_availability.append(item)
            if any(tier in workload.data_tiers for workload in targeted) and not item.safe:
                violations.append(_violation(
                    f"data_tier:{tier}",
                    f"Cluster {cluster_id} would retain {item.available_after} ready {tier} data nodes; policy requires {item.required}.",
                    f"Add or restore a ready {tier} data node before disruption.",
                ))

        service_availability = []
        for role, field_name in sorted(SERVICE_POLICY_FIELDS.items()):
            role_workloads = tuple(item for item in cluster_workloads if item.role == role)
            if not role_workloads:
                continue
            before = sum(item.ready for item in role_workloads)
            reduction = sum(item.ready and item.assignment_id in selected_ids for item in role_workloads)
            after = before - (reduction if simultaneous else min(1, reduction))
            item = RoleAvailability(
                role=role,
                available_before=before,
                available_after=max(0, after),
                required=getattr(policy, field_name),
            )
            service_availability.append(item)
            if any(workload.assignment_id in selected_ids for workload in role_workloads) and not item.safe:
                violations.append(_violation(
                    f"service:{role}",
                    f"Cluster {cluster_id} would retain {item.available_after} ready {role} workloads; policy requires {item.required}.",
                    f"Add or restore a ready {role} workload before disruption, or use the separately audited outage workflow when supported.",
                ))

        impacted_agents = sum(item.role == "elastic-agent" and item.ready for item in targeted)
        if impacted_agents and policy.allow_agent_interruption == "block":
            violations.append(_violation(
                "agent_interruption",
                f"Cluster {cluster_id} would interrupt {impacted_agents} Elastic Agent workload(s).",
                "Move or stop the affected Agent workload deliberately before maintenance.",
            ))

        endpoints_lost = []
        endpoint_roles = sorted({item.role for item in cluster_workloads if item.endpoint_required})
        for role in endpoint_roles:
            before = sum(item.role == role and item.ready for item in cluster_workloads)
            impacted = sum(item.role == role and item.ready for item in targeted)
            remaining = before - (impacted if simultaneous else min(1, impacted))
            if remaining == 0 and any(item.role == role and item.ready for item in targeted):
                endpoints_lost.append(role)

        cluster_impacts.append(ClusterImpact(
            cluster_id=cluster_id,
            affected_assignment_ids=tuple(item.assignment_id for item in targeted),
            affected_roles=tuple(sorted({item.role for item in targeted})),
            existing_unavailable=existing_unavailable,
            planned_unavailable=planned_unavailable,
            total_unavailable_after=total_unavailable_after,
            max_unavailable=policy.max_unavailable,
            master_total=observed_master_total,
            master_available_before=observed_master_available,
            master_available_after=master_available_after,
            master_required=master_required,
            data_tiers=tuple(tier_availability),
            services=tuple(service_availability),
            endpoints_lost=tuple(endpoints_lost),
            agent_interruptions=impacted_agents,
            violations=tuple(violations),
        ))

    return ImpactManifest(
        target_node_id=target.node_id,
        affected_cluster_ids=tuple(sorted(affected_cluster_ids)),
        affected_assignment_ids=tuple(item.assignment_id for item in selected),
        clusters=tuple(cluster_impacts),
    )


def _result(
    identifier: PredicateId,
    passed: bool,
    evidence: str,
    remediation: str,
    observed_at: datetime,
    override_ids: set[PredicateId],
    *,
    applicable: bool = True,
) -> PredicateResult:
    forceable = identifier not in HARD_PREDICATES
    override_applied = bool(applicable and not passed and forceable and identifier in override_ids)
    if not applicable or passed:
        severity = PredicateSeverity.INFO
        outcome = PredicateOutcome.PASSED
    elif override_applied:
        severity = PredicateSeverity.WARNING
        outcome = PredicateOutcome.WARNING
    else:
        severity = PredicateSeverity.CRITICAL
        outcome = PredicateOutcome.BLOCKED
    return PredicateResult(
        identifier=identifier,
        severity=severity,
        outcome=outcome,
        applicable=applicable,
        forceable=forceable if applicable else False,
        override_applied=override_applied,
        evidence_summary=evidence,
        remediation="" if passed or not applicable else remediation,
        observed_at=observed_at,
    )


def _health_meets(observed: str, required: str) -> bool:
    rank = {"unknown": 0, "red": 1, "yellow": 2, "green": 3}
    return rank[observed] >= rank[required]


def _ages_are_fresh(items: Iterable, now: datetime, max_age_seconds: int) -> bool:
    return all(0 <= (now - item.observed_at).total_seconds() <= max_age_seconds for item in items)


def evaluate_predicates(
    snapshot: ObservationSnapshot,
    target: PlanningTarget,
    policy: MaintenancePolicy,
    impact: ImpactManifest,
    *,
    now: datetime,
    stage: EvaluationStage = EvaluationStage.PLANNING,
    override_ids: set[PredicateId] | None = None,
) -> tuple[PredicateResult, ...]:
    override_ids = set(override_ids or ())
    selected = _selected_workloads(snapshot, target)
    target_host_ids = {item.node_id for item in selected}
    if target.node_id is not None:
        target_host_ids.add(target.node_id)
    target_hosts = tuple(item for item in snapshot.hosts if item.node_id in target_host_ids)
    missing_host_ids = target_host_ids - {item.node_id for item in target_hosts}

    affected_cluster_ids = set(impact.affected_cluster_ids)
    es_cluster_ids = {
        item.cluster_id for item in selected if item.role in ELASTICSEARCH_ROLES
    }
    if target.cluster_id is not None and target.operation in {
        OperationKind.SETTINGS_CHANGE,
        OperationKind.ZONING_CHANGE,
        OperationKind.UPGRADE,
    }:
        es_cluster_ids.add(target.cluster_id)
    es_clusters = tuple(item for item in snapshot.clusters if item.cluster_id in es_cluster_ids)
    missing_es_clusters = es_cluster_ids - {item.cluster_id for item in es_clusters}
    data_cluster_ids = {item.cluster_id for item in selected if item.data_tiers}
    data_clusters = tuple(item for item in es_clusters if item.cluster_id in data_cluster_ids)

    all_enabled = bool(target_hosts) and not missing_host_ids and all(item.enabled and item.initialized for item in target_hosts)
    all_reachable = bool(target_hosts) and not missing_host_ids and all(item.reachable for item in target_hosts)
    membership_ready = bool(target_hosts) and not missing_host_ids and all(item.membership_ready for item in target_hosts)

    relevant_workloads = tuple(item for item in snapshot.workloads if item.cluster_id in affected_cluster_ids)
    relevant_clusters = tuple(item for item in snapshot.clusters if item.cluster_id in affected_cluster_ids)
    required_sources = tuple(item for item in snapshot.sources if item.required)
    source_status_ok = bool(required_sources) and all(item.status == SourceStatus.OK for item in required_sources)
    fresh_items = required_sources + target_hosts + relevant_workloads + relevant_clusters
    fresh = source_status_ok and bool(fresh_items) and _ages_are_fresh(fresh_items, now, policy.observation_max_age_seconds)

    role_violations = tuple(
        violation
        for cluster_impact in impact.clusters
        for violation in cluster_impact.violations
        if violation.identifier != "master_quorum"
    )
    quorum_impacts = tuple(
        item for item in impact.clusters
        if any(workload.master_eligible for workload in selected if workload.cluster_id == item.cluster_id)
    )
    quorum_safe = all(item.master_quorum_safe for item in quorum_impacts)

    definitions = {
        PredicateId.HOST_ENABLED: (
            bool(target_host_ids),
            all_enabled,
            f"{len(target_hosts)} of {len(target_host_ids)} targeted hosts are enabled and initialized.",
            "Enable and initialize every targeted inventory host.",
        ),
        PredicateId.HOST_REACHABLE: (
            bool(target_host_ids),
            all_reachable,
            f"{sum(item.reachable for item in target_hosts)} of {len(target_host_ids)} targeted hosts passed an authenticated reachability observation.",
            "Restore authenticated controller SSH access and refresh the plan.",
        ),
        PredicateId.NO_CONFLICTING_OPERATION: (
            True,
            not snapshot.conflicting_operations,
            f"{len(snapshot.conflicting_operations)} overlapping operations were observed.",
            "Wait for or recover the overlapping operation before creating a new executable plan.",
        ),
        PredicateId.MEMBERSHIP_READY: (
            bool(target_host_ids),
            membership_ready,
            f"{sum(item.membership_ready for item in target_hosts)} of {len(target_host_ids)} targeted hosts have ready membership bindings.",
            "Verify configured interfaces and addresses on every targeted host.",
        ),
        PredicateId.FRESH_RUNTIME_OBSERVATION: (
            True,
            fresh,
            f"{len(required_sources)} required observation sources and {len(fresh_items) - len(required_sources)} affected objects are fresh and available.",
            "Refresh every required observation source before execution.",
        ),
        PredicateId.EXPECTED_CLUSTER_IDENTITY: (
            bool(es_cluster_ids),
            not missing_es_clusters and bool(es_clusters) and all(item.identity_matches for item in es_clusters),
            f"{sum(item.identity_matches for item in es_clusters)} of {len(es_cluster_ids)} affected Elasticsearch clusters match configured identity.",
            "Restore CA-verified access to the configured cluster name and UUID.",
        ),
        PredicateId.SUPPORTED_NODE_LIFECYCLE_MODE: (
            bool(es_cluster_ids),
            not missing_es_clusters and bool(es_clusters) and all(item.lifecycle_supported for item in es_clusters),
            f"{sum(item.lifecycle_supported for item in es_clusters)} of {len(es_cluster_ids)} affected clusters support their selected maintenance backend.",
            "Select a verified provider capability and maintenance backend.",
        ),
        PredicateId.CLUSTER_HEALTH: (
            bool(es_cluster_ids),
            not missing_es_clusters and bool(es_clusters) and all(_health_meets(item.health, policy.required_cluster_health) for item in es_clusters),
            f"{sum(_health_meets(item.health, policy.required_cluster_health) for item in es_clusters)} of {len(es_cluster_ids)} affected clusters meet required {policy.required_cluster_health} health.",
            "Restore policy-compliant Elasticsearch health and refresh observations.",
        ),
        PredicateId.NO_SHARD_MOVEMENT: (
            bool(data_cluster_ids),
            bool(data_clusters) and all(not item.initializing_shards and not item.relocating_shards for item in data_clusters),
            f"{sum(item.initializing_shards + item.relocating_shards for item in data_clusters)} initializing or relocating shards were observed on affected data clusters.",
            "Wait for shard movement to finish before disrupting a data node.",
        ),
        PredicateId.NO_LAST_SHARD_COPY: (
            bool(data_cluster_ids),
            bool(data_clusters) and all(item.no_last_shard_copy for item in data_clusters),
            f"{sum(item.no_last_shard_copy for item in data_clusters)} of {len(data_cluster_ids)} affected data clusters have no target-held last usable shard copy.",
            "Restore another usable shard copy before disrupting the target data node.",
        ),
        PredicateId.PRIMARY_PROMOTION_SAFETY: (
            bool(data_cluster_ids),
            bool(data_clusters) and all(item.primary_promotion_safe for item in data_clusters),
            f"{sum(item.primary_promotion_safe for item in data_clusters)} of {len(data_cluster_ids)} affected data clusters can safely promote required primaries.",
            "Restore an available in-sync copy for every affected primary shard.",
        ),
        PredicateId.ALLOCATION_SETTING_CAPTURED: (
            bool(data_cluster_ids) and stage == EvaluationStage.PREFLIGHT,
            bool(data_clusters) and all(item.allocation_setting_captured for item in data_clusters),
            (
                f"{sum(item.allocation_setting_captured for item in data_clusters)} of {len(data_cluster_ids)} affected data clusters have captured persistent and transient allocation layers."
                if stage == EvaluationStage.PREFLIGHT
                else "Allocation-layer capture is evaluated at the execution preflight before any allocation mutation."
            ),
            "Capture both persistent and transient allocation values before applying a guard.",
        ),
        PredicateId.MASTER_QUORUM: (
            bool(quorum_impacts),
            quorum_safe,
            f"{sum(item.master_quorum_safe for item in quorum_impacts)} of {len(quorum_impacts)} affected master quorums remain available.",
            "Add or restore master-eligible capacity before disruption.",
        ),
        PredicateId.ROLE_AVAILABILITY_BUDGET: (
            bool(impact.clusters),
            not role_violations,
            f"{len(role_violations)} cluster or role availability budget violations were calculated.",
            "Restore unavailable workloads, add redundant capacity, or create an explicitly audited outage plan where supported.",
        ),
        PredicateId.DISK_WATERMARKS_SAFE: (
            bool(data_cluster_ids),
            bool(data_clusters) and all(item.disk_watermarks_safe for item in data_clusters),
            f"{sum(item.disk_watermarks_safe for item in data_clusters)} of {len(data_cluster_ids)} affected data clusters have safe remaining disk capacity.",
            "Free disk capacity or add eligible data capacity before disruption.",
        ),
        PredicateId.TARGET_ARTIFACT_READY: (
            target.operation == OperationKind.UPGRADE,
            bool(es_clusters) and all(item.target_artifact_ready for item in es_clusters),
            f"{sum(item.target_artifact_ready for item in es_clusters)} of {len(es_cluster_ids)} affected clusters have the pinned target artifact ready.",
            "Download and verify every selected image digest before upgrade execution.",
        ),
        PredicateId.VERSION_TRANSITION_SUPPORTED: (
            target.operation == OperationKind.UPGRADE,
            bool(es_clusters) and all(item.version_transition_supported for item in es_clusters),
            f"{sum(item.version_transition_supported for item in es_clusters)} of {len(es_cluster_ids)} affected clusters support the requested version transition.",
            "Choose a non-downgrade target on a supported upgrade path.",
        ),
        PredicateId.SNAPSHOT_RECOVERY_READY: (
            target.operation == OperationKind.UPGRADE and target.is_major_upgrade,
            bool(es_clusters) and all(item.snapshot_recovery_ready for item in es_clusters),
            f"{sum(item.snapshot_recovery_ready for item in es_clusters)} of {len(es_cluster_ids)} affected clusters have verified snapshot recovery evidence.",
            "Create and verify a recent registered snapshot before the major upgrade.",
        ),
        PredicateId.NO_STALE_SHUTDOWN_RECORD: (
            bool(es_cluster_ids),
            not missing_es_clusters and bool(es_clusters) and all(not item.stale_shutdown_record for item in es_clusters),
            f"{sum(item.stale_shutdown_record for item in es_clusters)} stale shutdown records were observed across affected clusters.",
            "Resolve or remove the unrelated shutdown record through an audited recovery action.",
        ),
    }

    results = []
    for identifier in PREDICATE_ORDER:
        applicable, passed, evidence, remediation = definitions[identifier]
        results.append(_result(
            identifier,
            passed,
            evidence,
            remediation,
            snapshot.captured_at,
            override_ids,
            applicable=applicable,
        ))
    return tuple(results)
