from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
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
from app.modules.maintenance.manual import (
    ManualMaintenanceConflict,
    ManualMaintenanceRecoveryRequired,
    ManualMaintenanceService,
)
from app.modules.maintenance.models import MaintenancePlanPreviewInput, MaintenancePolicy, PreviewOperation
from app.modules.maintenance.evacuation_contracts import (
    build_inventory_evacuation_preview,
)
from app.modules.maintenance.observation import (
    collect_generic_preview_data,
    collect_host_reboot_planning_data,
)
from app.modules.maintenance.planning import canonical_hash
from app.modules.maintenance.service import (
    HostRebootPlanRequest,
    MaintenancePlanningService,
    generic_preview_idempotency_key,
    same_generic_preview_request,
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
from app.modules.maintenance.workflow_recovery import MaintenanceWorkflowRecoveryService


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


class ManualMaintenanceEnterInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=512)
    duration_seconds: int = Field(default=3600, ge=60, le=604800)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class ManualMaintenanceExitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="Manual maintenance complete", min_length=1, max_length=512)


class EvacuationPreviewInput(BaseModel):
    """Read-only placement preview; it never authorizes an evacuation."""

    model_config = ConfigDict(extra="forbid")

    cluster_id: int = Field(ge=1)
    source_node_id: int = Field(ge=1)
    replacement_node_id: int | None = Field(default=None, ge=1)


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
        recover=MAINTENANCE_CAPABILITIES["recovery"],
    )
    host_state = repository.find_host_state(record.target_node_id) if record.target_node_id else None
    workflow_state = None
    if host_state is not None and host_state.active_plan_id == record.id:
        workflow_state = host_state.workflow_state
    elif record.target_assignment_id is not None:
        assignment_state = repository.find_assignment_state(record.target_assignment_id)
        if assignment_state is not None and assignment_state.active_plan_id == record.id:
            workflow_state = assignment_state.workflow_state
    workflow_scope = record.target_manifest.get("public_operation")
    if workflow_scope not in {"host_maintenance", "container_maintenance"}:
        workflow_scope = None
    return {
        **serialize_plan_preview(record),
        "operation": serialize_maintenance_operation(
            record,
            steps=repository.list_steps(record.id),
            checkpoints=repository.list_checkpoints(record.id),
            host_state=host_state,
            workflow_state=workflow_state,
            workflow_scope=workflow_scope,
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
                include_post_return_expectations=True,
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


@router.get("/api/nodes/{node_id}/maintenance-mode")
async def get_manual_maintenance_mode(node_id: int, _: Annotated[str, Depends(require_user)]):
    with control_db() as connection:
        read_repository = MaintenanceReadRepository.from_connection(connection)
        if read_repository.host(node_id) is None:
            raise HTTPException(404, "Host not found")
        repository = MaintenanceRepository(connection)
        state = repository.get_host_state(node_id)
        plan = repository.get_plan(state.active_plan_id) if state.active_plan_id else None
        return {
            "node_id": node_id,
            "state": state.state.value,
            "state_revision": state.state_revision,
            "workflow_state": state.workflow_state.value,
            "workflow_state_revision": state.workflow_state_revision,
            "plan_id": plan.id if plan else None,
            "run_id": plan.run_id if plan else None,
            "expires_at": plan.expires_at if plan else None,
            "lifecycle_state": plan.lifecycle_state.value if plan else None,
        }


@router.post("/api/nodes/{node_id}/maintenance-mode/enter", status_code=201)
async def enter_manual_maintenance_mode(
    node_id: int,
    input: ManualMaintenanceEnterInput,
    username: Annotated[str, Depends(require_user)],
):
    if not MAINTENANCE_CAPABILITIES["manual_maintenance_entry"]:
        raise HTTPException(409, "Manual maintenance entry is disabled until the Phase 2 safety gate passes")
    key = input.idempotency_key or canonical_hash({
        "operation": "manual_maintenance",
        "node_id": node_id,
        "reason": input.reason.strip(),
        "duration_seconds": input.duration_seconds,
        "requested_by": username,
    })
    try:
        with control_db() as connection:
            return ManualMaintenanceService(
                MaintenanceRepository(connection), telemetry=telemetry(),
            ).enter(
                node_id,
                requested_by=username,
                reason=input.reason,
                idempotency_key=key,
                duration_seconds=input.duration_seconds,
            )
    except RecordNotFound as error:
        raise HTTPException(404, str(error)) from error
    except (ManualMaintenanceConflict, IdempotencyConflict, OverlappingPlanError) as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post("/api/nodes/{node_id}/maintenance-mode/exit")
async def exit_manual_maintenance_mode(
    node_id: int,
    input: ManualMaintenanceExitInput,
    username: Annotated[str, Depends(require_user)],
):
    try:
        with control_db() as connection:
            read_repository = MaintenanceReadRepository.from_connection(connection)
            if read_repository.host(node_id) is None:
                raise HTTPException(404, "Host not found")
            result = ManualMaintenanceService(
                MaintenanceRepository(connection), telemetry=telemetry(),
            ).exit(node_id, requested_by=username, reason=input.reason)
        if result.get("recovery_required"):
            raise HTTPException(409, result["recovery_reason"])
        return result
    except ManualMaintenanceRecoveryRequired as error:
        raise HTTPException(409, str(error)) from error
    except RecordNotFound as error:
        raise HTTPException(404, str(error)) from error


@router.post("/api/maintenance/plans/preview", status_code=201)
async def create_maintenance_plan_preview(
    input: MaintenancePlanPreviewInput,
    username: Annotated[str, Depends(require_user)],
):
    """Create a generic, non-mutating maintenance preview.

    The planning capability is deliberately independent from every execution
    capability. This route never creates a run or lock and never dispatches a
    remote adapter.
    """
    if not MAINTENANCE_CAPABILITIES["planning"]:
        raise HTTPException(409, "Maintenance planning is disabled until the Phase 1 safety gate passes")
    try:
        with control_db() as connection:
            repository = MaintenanceRepository(connection)
            idempotency_key = generic_preview_idempotency_key(input, requested_by=username)
            existing = repository.get_plan_by_idempotency_key(idempotency_key)
            if existing:
                if not same_generic_preview_request(existing, input):
                    raise IdempotencyConflict(
                        "Idempotency key was already used for a different maintenance preview"
                    )
                return maintenance_plan_response(repository, existing)
            assignment_ids = tuple(getattr(input, "assignment_ids", ()))
            if getattr(input, "assignment_id", None) is not None:
                assignment_ids = (input.assignment_id,)
            data = collect_generic_preview_data(
                connection,
                telemetry(),
                node_ids=((input.node_id,) if hasattr(input, "node_id") else ()),
                additional_cluster_ids=((input.cluster_id,) if hasattr(input, "cluster_id") else ()),
                additional_assignment_ids=assignment_ids,
                capability_revision=capability_revision(),
                node_shutdown_backend_enabled=MAINTENANCE_CAPABILITIES["node_shutdown_backend"],
                include_post_return_expectations=(
                    input.operation == PreviewOperation.HOST_MAINTENANCE
                ),
            )
            preview = MaintenancePlanningService(repository).create_generic_preview(
                data,
                input,
                requested_by=username,
            )
            return maintenance_plan_response(repository, repository.get_plan(preview["plan_id"]))
    except (KeyError, RecordNotFound) as error:
        raise HTTPException(404, "Maintenance preview target was not found") from error
    except (IdempotencyConflict, OverlappingPlanError) as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post("/api/maintenance/evacuation/preview")
async def preview_evacuation(
    input: EvacuationPreviewInput,
    _: Annotated[str, Depends(require_user)],
):
    """Return provider/capacity placement evidence without any side effect.

    This deliberately does not create a plan, run, lock, or remote operation.
    The response advertises ``execution_enabled=False`` until the Phase 5 gate
    is accepted, even when the provider-level predicates are otherwise ready.
    """
    with control_db() as connection:
        read_repository = MaintenanceReadRepository.from_connection(connection)
        if not read_repository.cluster_exists(input.cluster_id):
            raise HTTPException(404, "Cluster not found")
        policy_record = MaintenanceRepository(connection).get_policy(input.cluster_id)
        policy = MaintenancePolicy.model_validate(policy_record.policy if policy_record else {})
        preview = build_inventory_evacuation_preview(read_repository.evacuation_inventory(
            cluster_id=input.cluster_id,
            source_node_id=input.source_node_id,
            replacement_node_id=input.replacement_node_id,
            max_surge=policy.max_surge,
        ))
    payload = preview.model_dump(mode="json")
    payload["execution_enabled"] = False
    payload["capability_enabled"] = bool(MAINTENANCE_CAPABILITIES["evacuation"])
    return payload


@router.get("/api/maintenance/plans")
async def list_maintenance_plans(
    _: Annotated[str, Depends(require_user)],
    node_id: int | None = Query(default=None, ge=1),
    host_id: int | None = Query(default=None, ge=1),
    cluster_id: int | None = Query(default=None, ge=1),
    state: str | None = Query(default=None, min_length=1, max_length=32),
    limit: int = Query(default=100, ge=1, le=500),
):
    """List redacted plans with optional host, cluster, and state filters."""
    if node_id is not None and host_id is not None and node_id != host_id:
        raise HTTPException(422, "node_id and host_id must identify the same host")
    selected_node_id = node_id if node_id is not None else host_id
    try:
        with control_db() as connection:
            repository = MaintenanceRepository(connection)
            MaintenanceWorkflowRecoveryService(repository).expire_due_workflows()
            records = repository.list_plans(
                node_id=selected_node_id,
                cluster_id=cluster_id,
                state=state,
                limit=limit,
            )
            return {
                "items": [maintenance_plan_response(repository, record) for record in records],
                "count": len(records),
                "filters": {
                    "node_id": selected_node_id,
                    "cluster_id": cluster_id,
                    "state": state,
                },
            }
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/api/maintenance/plans/{plan_id}")
async def get_maintenance_plan(plan_id: str, _: Annotated[str, Depends(require_user)]):
    try:
        with control_db() as connection:
            repository = MaintenanceRepository(connection)
            MaintenanceWorkflowRecoveryService(repository).expire_due_workflows()
            return maintenance_plan_response(repository, repository.get_plan(plan_id))
    except RecordNotFound as error:
        raise HTTPException(404, str(error)) from error


@router.post("/api/maintenance/plans/{plan_id}/{action}")
async def maintenance_action(
    plan_id: str,
    action: Literal["execute", "pause", "resume", "cancel", "recover"],
    username: Annotated[str, Depends(require_user)],
):
    if action == "recover":
        if not MAINTENANCE_CAPABILITIES["recovery"]:
            raise HTTPException(409, "Maintenance recovery is disabled")
        try:
            with control_db() as connection:
                repository = MaintenanceRepository(connection)
                plan = repository.get_plan(plan_id)
                MaintenanceWorkflowRecoveryService(repository).reconcile_plan(
                    plan.id,
                    reason="operator-recovery-request",
                    username=username,
                )
                return {
                    **maintenance_plan_response(repository, repository.get_plan(plan_id)),
                    "run_id": plan.run_id,
                }
        except RecordNotFound as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

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
