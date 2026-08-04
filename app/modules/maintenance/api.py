from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.modules.platform import MAINTENANCE_CAPABILITIES, control_db, require_user
from app.modules.observability import telemetry
from app.modules.maintenance.execution import (
    AdapterResult,
    MAINTENANCE_ADAPTERS,
    MaintenanceAction,
    MaintenanceExecutionService,
    MaintenanceValidationError,
)
from app.modules.maintenance.lifecycle import TransitionError
from app.modules.maintenance.models import MaintenancePolicy
from app.modules.maintenance.observation import collect_host_reboot_planning_data
from app.modules.maintenance.planning import canonical_hash
from app.modules.maintenance.service import (
    HostRebootPlanRequest,
    MaintenancePlanningService,
    serialize_plan_preview,
)
from app.modules.maintenance.status import MaintenanceActionCapabilities, serialize_maintenance_operation
from app.modules.maintenance.store import (
    IdempotencyConflict,
    MaintenanceRepository,
    LockConflict,
    LockOwnershipError,
    OverlappingPlanError,
    RecordNotFound,
    RevisionConflict,
    StaleLockRequiresRecovery,
)
from app.modules.maintenance.repository import MaintenanceRepository as MaintenanceReadRepository


router = APIRouter()


class MaintenancePolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    policy: dict[str, Any]


class HostMaintenancePlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["reboot"] = "reboot"
    reason: str = Field(min_length=1, max_length=512)
    availability_mode: Literal["zero-impact"] = "zero-impact"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


def maintenance_policy_response(record=None):
    if record is None:
        return {
            "policy": MaintenancePolicy().model_dump(mode="json"),
            "revision": 0,
            "customized": False,
            "updated_by": None,
            "updated_at": None,
        }
    policy = MaintenancePolicy.model_validate(record.policy)
    return {
        "policy": policy.model_dump(mode="json"),
        "revision": record.revision,
        "customized": True,
        "updated_by": record.updated_by,
        "updated_at": record.updated_at,
    }


def require_cluster(repository: MaintenanceReadRepository, cluster_id: int):
    if not repository.cluster_exists(cluster_id):
        raise HTTPException(404, "Cluster not found")


def capability_revision() -> str:
    return canonical_hash({"schema": 1, "capabilities": MAINTENANCE_CAPABILITIES})


def same_host_plan_request(record, *, node_id: int, request: HostRebootPlanRequest) -> bool:
    target = record.plan.get("target") if isinstance(record.plan, dict) else None
    return bool(
        record.operation_kind == "reboot"
        and record.target_node_id == node_id
        and isinstance(target, dict)
        and target.get("reason") == request.reason
        and target.get("availability_mode") == request.availability_mode.value
    )


def maintenance_plan_response(repository: MaintenanceRepository, record):
    capabilities = MaintenanceActionCapabilities(
        pause=MAINTENANCE_CAPABILITIES["host_reboot"],
        resume=MAINTENANCE_CAPABILITIES["host_reboot"],
        cancel=MAINTENANCE_CAPABILITIES["host_reboot"],
        recover=MAINTENANCE_CAPABILITIES["host_reboot"],
    )
    return {
        **serialize_plan_preview(record),
        "operation": serialize_maintenance_operation(
            record,
            steps=repository.list_steps(record.id),
            checkpoints=repository.list_checkpoints(record.id),
            host_state=(repository.find_host_state(record.target_node_id) if record.target_node_id else None),
            capabilities=capabilities,
        ),
    }


@router.get("/api/clusters/{cluster_id}/maintenance-policy")
async def get_maintenance_policy(cluster_id: int, _: Annotated[str, Depends(require_user)]):
    with control_db() as connection:
        read_repository = MaintenanceReadRepository.from_connection(connection)
        require_cluster(read_repository, cluster_id)
        return maintenance_policy_response(MaintenanceRepository(connection).get_policy(cluster_id))


@router.put("/api/clusters/{cluster_id}/maintenance-policy")
async def put_maintenance_policy(
    cluster_id: int,
    input: MaintenancePolicyUpdate,
    username: Annotated[str, Depends(require_user)],
):
    policy = MaintenancePolicy.model_validate(input.policy)
    try:
        with control_db() as connection:
            read_repository = MaintenanceReadRepository.from_connection(connection)
            require_cluster(read_repository, cluster_id)
            repository = MaintenanceRepository(connection)
            record = repository.put_policy(
                cluster_id,
                policy.model_dump(mode="json"),
                username,
                expected_revision=input.expected_revision,
            )
            repository.record_audit(
                username=username,
                action="maintenance-policy-updated",
                cluster_id=cluster_id,
                item_id=str(cluster_id),
                detail={"revision": record.revision},
            )
            return maintenance_policy_response(record)
    except RevisionConflict as error:
        raise HTTPException(409, str(error)) from error


@router.post("/api/nodes/{node_id}/maintenance/plans", status_code=201)
async def create_host_maintenance_plan(
    node_id: int,
    input: HostMaintenancePlanInput,
    username: Annotated[str, Depends(require_user)],
):
    if not MAINTENANCE_CAPABILITIES["planning"]:
        raise HTTPException(409, "Maintenance planning is disabled until the Phase 1 safety gate passes")
    key = input.idempotency_key or canonical_hash({
        "operation": input.operation,
        "node_id": node_id,
        "reason": input.reason.strip(),
        "availability_mode": input.availability_mode,
        "requested_by": username,
    })
    try:
        request = HostRebootPlanRequest(
            operation=input.operation,
            reason=input.reason,
            availability_mode=input.availability_mode,
            idempotency_key=key,
        )
        with control_db() as connection:
            repository = MaintenanceRepository(connection)
            existing = repository.get_plan_by_idempotency_key(key)
            if existing:
                if not same_host_plan_request(existing, node_id=node_id, request=request):
                    raise IdempotencyConflict(
                        "Idempotency key was already used for a different maintenance preview"
                    )
                return maintenance_plan_response(repository, existing)
            conflicts = repository.observe_conflicts(node_id)
            data = collect_host_reboot_planning_data(
                connection,
                telemetry(),
                node_id=node_id,
                capability_revision=capability_revision(),
                conflicting_operations=conflicts.conflict_identifiers,
                node_shutdown_backend_enabled=MAINTENANCE_CAPABILITIES["node_shutdown_backend"],
            )
            preview = MaintenancePlanningService(repository).create_host_reboot_preview(
                data,
                request,
                requested_by=username,
            )
            return maintenance_plan_response(repository, repository.get_plan(preview["plan_id"]))
    except (KeyError, RecordNotFound) as error:
        raise HTTPException(404, "Host not found") from error
    except (IdempotencyConflict, OverlappingPlanError) as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/api/maintenance/plans/{plan_id}")
async def get_maintenance_plan(plan_id: str, _: Annotated[str, Depends(require_user)]):
    try:
        with control_db() as connection:
            repository = MaintenanceRepository(connection)
            return maintenance_plan_response(repository, repository.get_plan(plan_id))
    except RecordNotFound as error:
        raise HTTPException(404, str(error)) from error


@router.post("/api/maintenance/plans/{plan_id}/{action}")
async def maintenance_action(
    plan_id: str,
    action: Literal["execute", "pause", "resume", "cancel", "recover"],
    username: Annotated[str, Depends(require_user)],
):
    if not MAINTENANCE_CAPABILITIES["host_reboot"]:
        raise HTTPException(409, f"Maintenance {action} is disabled until its execution safety gate passes")

    try:
        with control_db() as connection:
            plan = MaintenanceRepository(connection).get_plan(plan_id)
    except RecordNotFound as error:
        raise HTTPException(404, str(error)) from error
    if plan.operation_kind != "reboot":
        raise HTTPException(409, f"Maintenance {plan.operation_kind} execution is not enabled")
    adapter = MAINTENANCE_ADAPTERS.get(plan.operation_kind)
    if adapter is None:
        raise HTTPException(409, "Maintenance reboot adapter is not configured; execution remains unavailable")

    ticket = None
    try:
        with control_db() as connection:
            service = MaintenanceExecutionService(
                MaintenanceRepository(connection),
                capability_revision=capability_revision,
            )
            ticket = service.prepare(plan_id, MaintenanceAction(action), username=username)
    except RecordNotFound as error:
        raise HTTPException(404, str(error)) from error
    except (
        LockConflict,
        LockOwnershipError,
        MaintenanceValidationError,
        OverlappingPlanError,
        RevisionConflict,
        StaleLockRequiresRecovery,
        TransitionError,
    ) as error:
        raise HTTPException(409, str(error)) from error

    try:
        adapter_result = await adapter.perform(ticket.request)
        if adapter_result is None:
            adapter_result = AdapterResult()
        if not isinstance(adapter_result, AdapterResult):
            raise TypeError("maintenance adapter returned an unsupported result")
    except Exception as error:
        with control_db() as connection:
            MaintenanceExecutionService(
                MaintenanceRepository(connection),
                capability_revision=capability_revision,
            ).fail(ticket, error_category="adapter-dispatch-failed")
        raise HTTPException(
            502,
            "Maintenance adapter failed at a protected boundary; recovery is required",
        ) from error

    try:
        with control_db() as connection:
            repository = MaintenanceRepository(connection)
            service = MaintenanceExecutionService(repository, capability_revision=capability_revision)
            service.finalize(ticket, adapter_result)
            response = maintenance_plan_response(repository, repository.get_plan(plan_id))
    except (
        LockConflict,
        LockOwnershipError,
        MaintenanceValidationError,
        OverlappingPlanError,
        RevisionConflict,
        StaleLockRequiresRecovery,
        TransitionError,
    ) as error:
        with control_db() as connection:
            MaintenanceExecutionService(
                MaintenanceRepository(connection),
                capability_revision=capability_revision,
            ).fail(ticket, error_category="adapter-result-requires-recovery")
        raise HTTPException(409, str(error)) from error
    return {**response, "run_id": ticket.run_id}
