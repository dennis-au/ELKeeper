# Planned Maintenance Status

Last updated: 2026-08-05

## Milestones

- [x] M0 Contract And Baseline
- [x] M1 Capability And State Foundations
- [x] M2 Planning And Preflight Slice
- [x] M3 Elasticsearch Allocation Guard
- [x] M4a Managed Workload Runtime Port
- [x] M4b Container Execution Integration
- [x] M5 Host Maintenance Vertical Slice
- [x] M6 Expiry, Failure, And Recovery
- [ ] M7 Operator Experience And Compatibility
- [ ] M8 Full Verification And Release Gate

## M0 Evidence

### Public Contract

- Owner: `app.modules.maintenance`.
- Public export: `app.modules.maintenance.contracts`.
- Added immutable DTOs for host and managed-container targets, workflow states,
  affected workloads and clusters, preflight evidence, allocation-guard status,
  workflow checkpoints, action availability, and the composed workflow summary.
- No route, database schema, capability, orchestration adapter, remote mutation,
  or release-artifact setting changed in this milestone.

### Existing Ownership And Compatibility Baseline

- Maintenance-owned persistence and state are implemented through
  `app.modules.maintenance.store` and its public maintenance repositories.
- Host, cluster, workload, runtime observation, platform run, and audit data are
  consumed through named maintenance read projections or injected platform
  contracts.
- Existing compatibility surfaces include maintenance plan history, generic
  previews, host reboot planning, and the legacy host manual-maintenance routes.
- Existing allocation capture and restoration remain owned by
  `app.modules.maintenance.elasticsearch`; the new summary contract exposes
  redacted guard ownership without replacing that implementation.
- Remote side effects remain behind the maintenance and orchestration adapter
  boundaries. No adapter is enabled by the M0 work.

### Validation

```text
python -m pytest -q tests/test_maintenance_planned_contracts.py \
  tests/test_maintenance_preview_models.py tests/test_maintenance_status.py
12 passed

python tools/check_refactor_boundaries.py --root . --strict
Passed: no private cross-module imports, no private cross-feature imports,
and no cross-owner SQL or route-level SQL.
```

## Next Milestone

M7 must connect the Maintenance workspace to the explicit host and container
workflow action APIs, derive action availability from the persisted scope and
workflow state, preserve generic pause/resume/cancel/recover behavior, and
retain responsive and run-console behavior.

## M1 Evidence

### Capability And State Separation

- `planning` remains read-only and no longer authorizes manual maintenance
  entry or exit.
- New operations are independently represented as manual entry, container stop,
  host shutdown, and host reboot capabilities; recovery and active-window exit
  remain available independently of new-entry policy.
- `host_maintenance_state` now holds additive `workflow_state` and
  `workflow_state_revision` columns, preserving the legacy state and revision
  contract for existing callers.
- SQLite enum and transition triggers validate workflow changes separately from
  legacy host state. The migration is additive and tested for existing and
  partially applied schemas.

### Validation

```text
python -m pytest -q tests/test_maintenance_manual.py \\
  tests/test_maintenance_store.py tests/test_maintenance_api.py \\
  tests/test_platform_contracts.py
61 passed, 1 warning

cd frontend && npm test -- --run src/features/maintenance/MaintenanceWorkspace.test.tsx
2 passed

cd frontend && npm run typecheck
passed

python tools/check_refactor_boundaries.py --root . --strict
Passed: no private cross-module imports, no private cross-feature imports,
and no cross-owner SQL or route-level SQL.

git diff --check
passed
```

## M2 Evidence

### Read-Only Host And Container Planning

- Added `host_maintenance` and `container_maintenance` request variants to the
  authenticated `/api/maintenance/plans/preview` contract.
- Host scope uses the current host projection to aggregate managed workloads
  and affected clusters across every cluster sharing the selected host.
- Container scope resolves one active assignment and calculates impact only for
  that assignment; peer workloads on its host remain outside the preview's
  affected-workload list.
- The existing planning route remains capability-gated and writes only the
  durable preview, its ordered steps, and audit evidence. Tests prove it does
  not create platform runs or maintenance locks.
- The Maintenance workspace now uses a Host/Container segmented target control
  and opens the persisted plan detail after preview creation. No stop or
  preparation action is added by this slice.

### Validation

```text
python -m pytest -q tests/test_maintenance_preview_models.py \\
  tests/test_maintenance_api.py tests/test_maintenance_service.py \\
  tests/test_maintenance_safety.py tests/test_maintenance_status.py
42 passed, 1 warning

cd frontend && npm test -- --run src/features/maintenance/MaintenanceWorkspace.test.tsx
3 passed

cd frontend && npm run typecheck
passed

python tools/check_refactor_boundaries.py --root . --strict
Passed: no private cross-module imports, no private cross-feature imports,
and no cross-owner SQL or route-level SQL.

git diff --check
passed
```

## M3 Evidence

### Durable Allocation Guard Ownership

- Added the maintenance-owned `maintenance_allocation_guards` ledger through
  additive schema migration v5. It has a unique active owner per cluster,
  immutable owner identity, optimistic revisions, legal phase transitions, and
  retained restored history.
- `AllocationGuardService` persists the checkpoint returned by the existing
  CA-verified Elasticsearch allocation controller, blocks competing owners,
  and permits only the owning plan to activate or restore a guard.
- The service rehydrates a persisted active checkpoint after controller restart
  before delegating restoration. Failed activation is persisted as
  `recovery_required`; successful restoration releases the cluster for a new
  plan.
- Existing controller coverage verifies persistent/transient capture, exact
  setting restoration after activation failure, success/failure/cancel/recovery
  cleanup, timeout read-back, and recovery-required behavior. Credentials stay
  in the HTTP authorization header and are excluded from errors and client
  representation.

### Validation

```text
python -m pytest -q tests/test_maintenance_allocation_guards.py \\
  tests/test_maintenance_elasticsearch.py tests/test_maintenance_store.py \\
  tests/test_maintenance_post_return.py
54 passed

python tools/check_refactor_boundaries.py --root . --strict
Passed: no private cross-module imports, no private cross-feature imports,
and no cross-owner SQL or route-level SQL.

git diff --check
passed
```

## M4a Evidence

### Assignment-Scoped Container Workflow

- Added `app.modules.maintenance.container_maintenance` as the maintenance
  owner for one-workload prepare, stop, return, verification, and cleanup.
- The target resolver derives the exact `ecp-<cluster>-<role>-<node>.service`
  from public cluster and workload owner contracts. It rejects a missing,
  inactive, mismatched, or non-managed target before any operation begins.
- `WorkloadRepository` now exposes exact assignment claim and release methods;
  maintenance never writes `cluster_assignments` directly. A successful
  preparation claims only the selected assignment and takes only its host,
  cluster, and assignment locks.
- Data-bearing targets capture and activate the existing durable allocation
  guard before `ready_to_stop`; stateless targets never change allocation.
  Return verifies the same unit, reconciles only that assignment's companions,
  restores the guard, releases the selected claim, and completes the attached
  platform run.
- `ControllerManagedWorkloadRuntime` maps only the selected target to
  `ControllerMaintenanceIO`. The controller I/O adapter rejects all lifecycle
  calls unless `workload_lifecycle_enabled` is explicitly enabled, validates
  only controller-managed `ecp-*.service` units, and dispatches exact
  `systemctl stop -- <unit>` and `systemctl start -- <unit>` argv through
  controller-managed SSH.
- The runtime transport is not registered with FastAPI or application
  composition and does not append workflow action progress to the platform run
  or SSE stream. It is therefore a verified runtime port, not an end-to-end
  container action.
- A failed or unconfirmed stop transitions the selected assignment and plan to
  `recovery_required` while retaining its lock and claim. Peer assignments
  remain untouched.

### Validation

```text
python -m unittest tests.test_container_maintenance \\
  tests.test_host_maintenance tests.test_maintenance_controller_io \\
  tests.test_maintenance_runtime -q
33 passed

python tools/check_refactor_boundaries.py --root . --strict
Passed: no private cross-module imports, no private cross-feature imports,
and no cross-owner SQL or route-level SQL.

git diff --check
passed
```

## M4b Evidence

### Container Execution Integration

- Added the authenticated, workflow-specific
  `/api/maintenance/workflows/{plan_id}/{prepare|stop|return}` contract without
  changing the legacy reboot action route.
- The action service rejects disabled execution before opening a database
  connection, constructing a workflow, acquiring a remote transport, or
  creating a run. The released artifact continues to have
  `container_stop=false`.
- Application assembly composes the assignment-scoped workflow with
  controller-owned pooled SSH, exact managed systemd units, assignment claims,
  fixed redacted platform-run progress, selected Filebeat reconciliation, and
  an action-scoped CA-verified allocation guard. Guard clients close after each
  action.
- Integration coverage proves one attached run survives prepare, stop, and
  return; only the selected unit and companion are affected; data-node guards
  apply `primaries` then restore the prior settings; and failures remain
  recovery-required without exposing remote exception content.

### Validation

```text
python -m unittest tests.test_container_maintenance \
  tests.test_container_maintenance_actions \
  tests.test_maintenance_controller_io \
  tests.test_maintenance_allocation_guards \
  tests.test_maintenance_elasticsearch tests.test_filebeat_reconcile \
  tests.test_maintenance_api -q
Ran 68 tests
OK

git diff --check
passed

python tools/check_refactor_boundaries.py --root . --strict
Passed: no private cross-module imports, no private cross-feature imports,
and no cross-owner SQL or route-level SQL.

python tools/check_table_ownership.py --root . --strict
Passed: no cross-owner SQL access or route-level SQL.

python tools/check_source_safety.py --root .
Passed: no lab addresses or Podman TCP endpoints found.
```

## M5 Evidence

### Host-Scoped Workflow And Reboot Composition

- Added `app.modules.maintenance.host_maintenance` as the maintenance owner for
  a host-wide workflow that resolves only the assignment IDs and revisions
  captured in the approved host preview.
- Host preparation claims every affected assignment through the public workload
  repository contract, activates exactly one allocation guard for each affected
  data-bearing cluster, and reaches `ready_to_stop` before stopping a workload.
- Stop order takes dependent services down before Elasticsearch roles; return
  requires host readiness, starts Elasticsearch before its dependents, verifies
  each workload, and reconciles only its corresponding companions.
- A prepared host workflow creates a signed reboot coordinator bound to the
  attached run, selected node, immutable assignment IDs, and exact Ansible
  playbook allowlist. It persists executor staging, reboot intent,
  acknowledgement, disconnect, reconnect, and verified return boundaries.
- Executor cleanup is unavailable until `reboot.return-discovered` is durable;
  legacy operator handoff records never claim that a reboot occurred.
- The authenticated host action route now dispatches `prepare`, `stop`,
  `handoff`, and `return` through one attached platform run. It creates no
  workflow, run, or transport when the default-disabled `host_reboot`
  capability is off.
- Application assembly uses controller-owned pooled SSH, fixed redacted
  run/SSE progress, selected Filebeat companion reconciliation, and host
  rediscovery checks for SSH, boot ID, Podman, Quadlet, and managed systemd
  units. The action-scoped allocation guard client is closed after each action.
- Cross-cluster Happy Flow coverage proves exact host targeting and peer-host
  isolation. The API test proves a host preview persists every affected
  assignment revision in its immutable manifest. A failed stop preserves every
  affected workload claim, active lock, allocation guard, and
  `recovery_required` state for explicit recovery. A role-ordering regression
  covers data, master, Kibana, Fleet Server, Logstash, and Elastic Agent.
- A host-maintenance preview now captures immutable runtime node identities,
  observed image versions, cluster UUIDs, and managed endpoint references.
  Generic host previews retain this evidence through their merged projection.
- Application assembly composes the host workflow with a CA-verified
  Elasticsearch client pool using the encrypted per-cluster monitoring API key
  and controller-cached CA. Credentials are redacted from the connection
  representation and failures.
- Controller-generated endpoint probes are restricted to active managed
  assignment endpoints, literal IP addresses, fixed paths, allowlisted status
  codes, and managed CA paths. Missing or malformed runtime endpoint data is
  omitted, so the immutable expectation fails the return verification instead
  of permitting an unverified close.
- `PostReturnCoordinator` verifies node identity, version, cluster UUID, shard
  recovery, cluster health, and service budgets after all workload and
  allocation-guard return steps complete. Any failed check keeps the workflow,
  locks, claims, and evidence in `recovery_required`.

### Validation

```text
python -m unittest tests.test_host_maintenance \\
  tests.test_host_maintenance_actions tests.test_container_maintenance \\
  tests.test_container_maintenance_actions tests.test_maintenance_controller_io \\
  tests.test_maintenance_allocation_guards tests.test_maintenance_elasticsearch \\
  tests.test_filebeat_reconcile tests.test_maintenance_safety \\
  tests.test_maintenance_service tests.test_maintenance_api \\
  tests.test_refactor_contracts -q
Ran 137 tests
OK

python -m unittest tests.test_maintenance_post_return \\
  tests.test_host_maintenance tests.test_host_maintenance_actions \\
  tests.test_maintenance_controller_io tests.test_maintenance_observation \\
  tests.test_maintenance_api -q
Ran 66 tests
OK

python tools/check_refactor_boundaries.py --root . --strict
Passed: no private cross-module imports, no private cross-feature imports,
and no cross-owner SQL or route-level SQL.

git diff --check
passed

python tools/check_table_ownership.py --root . --strict
Passed: no cross-owner SQL access or route-level SQL.

python tools/check_source_safety.py --root .
Passed: no lab addresses or Podman TCP endpoints found.
```

## M6 Evidence

### Recovery And Expiry

- Added `app.modules.maintenance.workflow_recovery` as the maintenance-owned
  recovery reconciler for the planned host and container workflows.
- Controller startup invokes the reconciler through an injected platform
  bootstrap contract after checkpoint classification. It marks only the exact
  previewed host and assignment states as `recovery_required`; it never starts,
  stops, returns, or cleans up a remote workload.
- Expired active workflows are converted to a visible recovery state when
  maintenance plans are read. The attached platform run is marked through its
  public contract while locks, workload claims, allocation guards, checkpoints,
  and missing-target evidence are retained for explicit recovery.
- The authenticated `recover` action is independent of the disabled
  host-execution capability. It reconciles durable local state and preserves
  ownership; it cannot release a stale lock or claim a successful return.
- Startup classifies reboot boundaries from the durable checkpoint journal:
  checkpoints before `reboot.intent` are resumable, intent through reconnect is
  ambiguous and requires rediscovery, and `reboot.return-discovered` permits
  only post-return verification. The recovery reconciler does not replay a
  reboot or trigger executor cleanup.
- Existing lock recovery still requires rediscovery evidence for every expired
  lock. Container identity mismatch fails before a run or lock is created; a
  failed return leaves the exact workload, guard, claim, plan, and lock in
  `recovery_required`.

### Validation

```text
python -m unittest tests.test_container_maintenance \\
  tests.test_container_maintenance_actions tests.test_host_maintenance \\
  tests.test_host_maintenance_actions tests.test_host_reboot \\
  tests.test_maintenance_controller_io tests.test_maintenance_reboot \\
  tests.test_maintenance_api tests.test_maintenance_post_return \\
  tests.test_maintenance_allocation_guards \\
  tests.test_maintenance_workflow_recovery tests.test_platform_bootstrap -q
Ran 115 tests
OK

python tools/check_refactor_boundaries.py --root . --strict
python tools/check_table_ownership.py --root . --strict
python tools/check_source_safety.py --root .
Passed: no private cross-module imports, no private cross-feature imports,
cross-owner SQL, route-level SQL, lab addresses, or Podman TCP endpoints.

git diff --check
passed
```

## M7 Evidence

### Current Operator Experience And Compatibility Status

- Plan-history selection retrieves current plan detail and presents the
  persisted preview, lifecycle/checkpoint progress, generic maintenance
  controls, and the recovery action in the existing in-page dialog model.
- A recovery-required plan keeps its generic recovery action available without
  the separately disabled host-execution capability. Non-terminal plan details
  refresh every three seconds.
- The Hosts workspace projects active host-target maintenance by durable node
  ID, avoiding name-based ambiguity and leaving container-target plans out of
  the host status column.
- This milestone is reopened: the workspace still needs explicit host/container
  workflow action dispatch and state-aware action controls. Existing frontend
  evidence is retained as regression coverage, not completion evidence.

### Validation

```text
cd frontend && npm test -- --run \\
  src/features/maintenance/MaintenanceWorkspace.test.tsx \\
  src/features/maintenance/components/MaintenanceOperationRecovery.test.tsx \\
  src/features/hosts/HostsWorkspace.test.ts
13 passed

cd frontend && npm run typecheck
passed

cd frontend && npm run build
passed (existing large-chunk warning)

python tools/check_refactor_boundaries.py --root . --strict
Passed: no private cross-module imports, no private cross-feature imports,
and no cross-owner SQL or route-level SQL.

git diff --check
passed
```

## M8 Evidence (In Progress)

### Candidate Verification

- The local five-minute profile passed: 535 backend tests, 104 frontend tests,
  type checking, the frontend production build, strict refactor and
  table-ownership checks, source safety, and all bundled Ansible syntax checks.
- The configured build host completed the fifteen-minute profile against an
  isolated candidate source directory. It rebuilt the single image, reran the
  535-test backend suite inside that image, and passed the isolated smoke.
- The isolated smoke ran without a source mount and verified health, login,
  authenticated APIs, every primary SPA route, static asset delivery, and SSE.
- An independent in-image test run exited successfully, and a second isolated
  smoke run passed. Image inspection verified the application, compiled static
  assets, Ansible runtime and playbooks, tests, and smoke tooling are present
  in the candidate image.
- Browser acceptance used the isolated candidate through a loopback SSH tunnel.
  Login and all six primary routes loaded at a desktop viewport without
  horizontal overflow. Dashboard and Maintenance also loaded at `390x844`
  without overflow; the browser console had no warnings or errors. The
  candidate database was intentionally empty, so this validates the login,
  routing, empty/degraded, and responsive shell states rather than live
  workload disruption behavior.

### Remaining Release Gate

- M8 is deliberately not complete. The live database was not backed up and no
  live controller was replaced.
- M4b and M5 have authenticated application composition, local run/SSE
  integration, and post-return verification coverage. Destructive lab
  validation and the separately approved host executor remain unproven.
- `APPROVED_MUTATION_CAPABILITIES` remains empty. No host reboot, shutdown, or
  container-stop capability can be enabled merely through an environment
  variable.
- The full profile correctly requires an explicit destructive live-round
  command. Lab container and host Happy Flow plus Chaos Mode evidence, exact
  allocation restoration, unrelated-resource preservation, and persistent
  mount/restart recovery therefore remain unproven.
- Explicit approval and a fresh, backed-up lab baseline are required before
  those destructive rounds can run. Until then, the M8 checkbox remains
  unchecked and the release artifact remains mutation-disabled.

## Done When Audit

| Requirement | Current evidence | State |
| --- | --- | --- |
| Read-only host preview | `test_host_preview_is_gated_idempotent_and_non_mutating` proves no run, lock, remote mutation, or allocation change. | Controller test passed |
| Isolated container preview | `test_explicit_host_and_container_maintenance_previews_are_read_only_and_isolated` proves a peer assignment is not included. | Controller test passed |
| Data-node allocation guard | Container and allocation-guard suites prove capture, `primaries`, owner persistence, `ready_to_stop`, and restoration behavior. | Controller and stub tests passed |
| Container Happy Flow | The assignment-scoped simulated Happy Flow and authenticated action API pass with runtime composition and run/SSE attachment. An authorized lab container stop and return has not run. | Live evidence missing |
| Host Happy Flow | The cross-cluster simulated flow and authenticated host action API pass with one run, pooled SSH, progress, handoff, selected companion reconciliation, and post-return endpoint/identity/cluster verification. An authorized host action has not run. | Live evidence missing |
| Safety predicates block mutation | Maintenance API, execution, container, host, and allocation-guard suites cover quorum, stale/conflicting, identity, provider, and target failures before dispatch. | Controller and stub tests passed |
| Restart classification | Workflow-recovery and platform-bootstrap suites cover persisted recovery classification. | Controller test passed |
| Disabled entry retains recovery | Capability and API tests prove exit and recovery remain available while new mutation entry is disabled. | Controller test passed |
| Expiry and missing-target recovery | Expiry, lock, container identity, and workflow-recovery tests retain ownership and require explicit recovery. | Controller test passed |
| Operator UI state | Maintenance workspace, recovery component, host projection, and action-console tests pass. The source candidate also passed authenticated desktop route and mobile Dashboard/Maintenance no-overflow checks with a clean console. | Unit and candidate-browser coverage passed |
| Focused technical validation | Current five-minute profile passed: backend, frontend, typecheck, boundaries, table ownership, and source safety. | Passed |
| Build-host profiles | Configured-build-host fifteen-minute candidate profile and isolated smoke passed. | Passed |
| Lab Happy Flow and Chaos Mode | No destructive lab round was authorized or run. | Missing |
| Release remains mutation-disabled | Capability allow-list is empty and API coverage proves protected actions fail closed. | Passed |
