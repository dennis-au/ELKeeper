# Implementation Plan: Planned Host And Container Maintenance

Status: Draft for review
Date: 2026-08-05

## Overview

Implement the behavior defined in `PROMPT.md` as compatibility-preserving
vertical slices inside the maintenance module. This plan refines the existing
maintenance phases; it does not bypass the safety and release gates in
`maintenance_plan.md`.

Remote mutations remain behind orchestration adapters, platform runs, SSE,
redaction, recovery, and explicit release-artifact approval. Each milestone
must leave the application in a testable state, with incomplete mutation UI
hidden or disabled.

## Product Boundary

The implementation supports exactly two maintenance intents: planned host work
that requires a reboot or shutdown, and planned work on one managed container.
An expected short return of a data-bearing Elasticsearch node may use the
temporary `cluster.routing.allocation.enable=primaries` guard, following
Elastic rolling-restart guidance. Permanent or unbounded node absence is an
evacuation or recovery concern, not a maintenance plan.

## Milestones

- [x] Milestone 0: Contract And Baseline
- [x] Milestone 1: Capability And State Foundations
- [x] Milestone 2: Planning And Preflight Slice
- [x] Milestone 3: Elasticsearch Allocation Guard
- [x] Milestone 4a: Managed Workload Runtime Port
- [x] Milestone 4b: Container Execution Integration
- [x] Milestone 5: Host Maintenance Vertical Slice
- [x] Milestone 6: Expiry, Failure, And Recovery
- [ ] Milestone 7: Operator Experience And Compatibility
- [ ] Milestone 8: Full Verification And Release Gate

## Milestone 0: Contract And Baseline

**Outcome:** Agree on one public definition of planning, host maintenance,
container maintenance, stop, return, expiry, and recovery.

- Inventory the current maintenance routes, persistence, state machine,
  frontend controls, compatibility wrappers, and operation gates.
- Record the two supported operator intents and direct permanent removal to
  evacuation, decommission, detach, purge, or recovery workflows.
- Record the owner of every required read projection and mutation contract.
- Define public DTOs for target scope, affected workloads and clusters,
  preflight evidence, allocation guard state, checkpoints, and available
  actions.
- Define compatibility behavior for existing plan history and manual-mode
  records.
- Add characterization tests for the current behavior before changing it.

**Exit gate:** Public contracts and compatibility decisions are reviewed; no
runtime behavior or release capability has changed.

**Completed 2026-08-05:** Added the public planned-maintenance contract through
`app.modules.maintenance.contracts`, preserving the existing maintenance model,
allocation-guard, plan-history, and manual-mode contracts. Baseline ownership
and compatibility evidence is recorded in `STATUS.md`.

## Milestone 1: Capability And State Foundations

**Outcome:** Planning, entry, stop, host action, exit, cleanup, and recovery are
independently authorized and represented.

- Replace the use of `planning` as a proxy for manual maintenance entry and
  exit.
- Add separate capabilities for planning, maintenance preparation, container
  stop, host reboot or shutdown, and recovery.
- Ensure active-operation exit, cleanup, and recovery remain callable when new
  entry is disabled.
- Add the expanded persisted state model and repeatable additive migrations.
- Preserve old API response shapes through small compatibility adapters where
  required.
- Add migration fixtures for existing available, maintenance, expired, and
  recovery-required records.

**Exit gate:** Enabling planning cannot enable a mutation; disabling entry
cannot strand an active maintenance lock; upgrade and interruption fixtures
pass.

**Completed 2026-08-05:** Separated manual maintenance entry from read-only
planning, added independently gated container and host action capabilities, and
persisted an additive workflow-state projection alongside the compatible legacy
host state. Active windows retain exit and recovery paths even after entry is
disabled. Validation evidence is recorded in `STATUS.md`.

## Milestone 2: Planning And Preflight Slice

**Outcome:** Operators can preview either maintenance scope with complete,
read-only impact and blocker evidence.

- Add host-target and assignment-target planning requests.
- Resolve cross-cluster host impact and exact container isolation through named
  read projections.
- Evaluate freshness, conflicts, provider ownership, quorum, shard-copy safety,
  relocation, recovery, disk watermarks, cluster identity, and role budgets.
- Return ordered preparation, stop, return, verification, and cleanup steps.
- Add the host/container selector and plan preview to the Maintenance page.
- Keep all stop and preparation controls absent until their capabilities are
  enabled.

**Exit gate:** Preview requests create no run, lock, assignment change,
Elasticsearch setting, Ansible execution, Podman action, or host mutation.

**Completed 2026-08-05:** Added explicit `host_maintenance` and
`container_maintenance` preview requests to the authenticated planning route.
Host previews aggregate every affected managed workload on the selected host;
container previews are limited to the selected active assignment. The
Maintenance workspace now selects either scope and opens the persisted,
read-only preview. Validation evidence is recorded in `STATUS.md`.

## Milestone 3: Elasticsearch Allocation Guard

**Outcome:** Short planned interruptions of data-bearing nodes can suppress
wasteful replica allocation without losing the prior cluster configuration.

- Add a maintenance-owned CA-verified allocation-settings contract.
- Capture persistent, transient, and effective allocation values in redacted
  checkpoint evidence.
- Apply and read back `cluster.routing.allocation.enable=primaries` only for
  affected data-bearing nodes.
- Add cluster-wide guard ownership so concurrent plans cannot overwrite or
  prematurely restore settings.
- Restore the exact captured layers and verify the effective result.
- Add recovery paths for stale guards, target non-return, concurrent changes,
  and controller restart.
- Keep the optional Elasticsearch node-shutdown backend independently gated.

**Exit gate:** Stub and integration tests prove exact restoration on success,
failure, cancellation, timeout, and restart; no credential or setting value is
leaked into logs or browser state.

**Completed 2026-08-05:** Added a maintenance-owned allocation-guard ledger
with one active owner per cluster, optimistic revisions, legal phase
transitions, and persisted checkpoints. `AllocationGuardService` rehydrates the
owner checkpoint after a controller restart and delegates capture, activation,
and exact restoration to the existing CA-verified Elasticsearch controller.
The optional node-shutdown backend remains independently disabled. Validation
evidence is recorded in `STATUS.md`.

## Milestone 4a: Managed Workload Runtime Port

**Outcome:** The maintenance module can address exactly one managed workload
unit through the controller-owned transport boundary, while remaining disabled
by default.

- Resolve only a validated `ecp-*.service` unit for the selected assignment.
- Fail closed until the named workload-lifecycle runtime flag is enabled.
- Dispatch exact systemd stop/start argv through controller-managed SSH.
- Preserve selected-target isolation and redacted runtime results.

**Exit gate:** Focused tests prove fail-closed default behavior, exact unit
validation, exact SSH argv, and selected-target-only runtime mapping.

**Completed 2026-08-05:** Added `ControllerManagedWorkloadRuntime` and a
default-disabled controller I/O transport for exact managed systemd units. The
existing assignment-scoped workflow remains injectable and isolated; the
runtime port does not register a browser action or release capability.

## Milestone 4b: Container Execution Integration

**Outcome:** An operator can use the tracked maintenance workflow to prepare,
stop, and return exactly one managed container through the existing run and SSE
contract.

- Add a workflow-specific authenticated action API separate from the legacy
  reboot adapter route.
- Compose the container workflow with controller I/O, allocation guard,
  workload claim, readiness, companion reconciliation, platform run logging,
  and redacted SSE output.
- Create or attach the workflow run before a remote effect and append progress
  at every durable checkpoint.
- Keep the action capability disabled by default and fail closed when it is
  not explicitly approved.
- Prevent ordinary reconciliation from restarting the intentionally stopped
  assignment during the active window.
- Verify role-specific readiness, reconcile only the selected companions,
  restore guards, and release the selected claim on a successful return.

**Exit gate:** Controller and API integration tests prove run attachment, SSE
progress, exact selected-unit targeting, peer isolation, guard restoration,
and recovery-required behavior. The live Happy Flow and Chaos Mode remain M8
acceptance evidence.

**Completed 2026-08-05:** Registered the isolated authenticated container
workflow action API and composed it with controller-managed pooled SSH,
assignment claims, selected Filebeat companion reconciliation, platform run
progress, and CA-verified allocation-guard restoration. The capability remains
default-disabled and no release mutation capability was enabled. Validation
evidence is recorded in `STATUS.md`.

## Milestone 5: Host Maintenance Vertical Slice

**Outcome:** An operator can prepare a host-wide interruption across every
affected cluster and managed workload.

- Aggregate all assignments and clusters sharing the selected host.
- Compile dependency-aware stop and return ordering for Elasticsearch and its
  dependent services.
- Prepare all affected clusters atomically before any workload is stopped.
- Stop only controller-managed workloads and verify the host is
  `ready_for_host_action`.
- Execute reboot or shutdown only through the separately approved host-action
  adapter, or provide a tracked operator handoff when execution is disabled.
- Rediscover boot identity, Podman, systemd, workloads, endpoints, node
  identities, and cluster state after return.
- Restore every affected cluster guard and release locks only after verified
  service return or explicit recovery.

**Exit gate:** Redundant topology rounds pass for data, master-eligible, Kibana,
Fleet Server, Logstash, Agent, and multi-cluster hosts; singleton and unsafe
topologies block before host changes.

**Completed 2026-08-05:** Added host-scoped preparation, dependency-aware
managed workload stop and return, data-cluster allocation guards, and the
authenticated `/api/maintenance/host-workflows/{plan_id}/{action}` contract.
After preparation has attached the platform run, application assembly creates a
signed reboot coordinator restricted to the executor-stage and reboot playbooks.
It uses controller-owned pooled SSH, fixed redacted run/SSE progress, selected
Filebeat reconciliation, host rediscovery checks, controller-generated managed
endpoint probes, and CA-verified Elasticsearch post-return verification of node
identity, version, cluster UUID, shard recovery, health, and service budgets.
The executor is cleaned up only after a durable reboot-return checkpoint.
Invalid controller endpoint data is omitted from probing and causes the
persisted endpoint expectation to require recovery rather than passing. The
host execution capability remains default-disabled; no release mutation
capability was enabled. Focused topology, safety, post-return, API, reboot, and
strict ownership checks passed; live destructive acceptance remains M8 evidence.

## Milestone 6: Expiry, Failure, And Recovery

**Outcome:** Every incomplete maintenance window has an operator-visible,
evidence-backed way forward.

- Convert expiry into a persisted recovery condition rather than a silent
  unlock or successful exit.
- Provide actions to retry observation, continue waiting, return the target,
  restore allocation, recover stale locks, or transition to evacuation.
- Require fresh rediscovery evidence before releasing an expired lock or guard.
- Keep recovery actions available even when entry and execution capabilities
  are disabled.
- Add startup recovery classification for every new checkpoint and side-effect
  boundary.
- Make cleanup idempotent and restricted to plan-owned artifacts.

**Exit gate:** Restart-at-checkpoint, missing-target, expired-lock,
wrong-identity, partial-stop, failed-return, and concurrent-recovery tests leave
no silently abandoned state.

**Completed 2026-08-05:** Added a maintenance-owned workflow recovery
reconciler. Startup, expiry, and an authenticated recovery action now mark only
the previewed host and assignments as `recovery_required`, retain active locks,
claims, guards, checkpoints, and runs, and preserve missing-target evidence.
Reboot checkpoints are classified explicitly: before `reboot.intent` is
resumable, `reboot.intent` through reconnect requires host and executor
rediscovery, and `reboot.return-discovered` permits only post-return work. The
reconciler never replays a reboot or cleans up an executor. Recovery remains
available while host execution is disabled; it does not infer remote success or
release a stale lock without rediscovery evidence.

## Milestone 7: Operator Experience And Compatibility

**Outcome:** The product presents maintenance as one coherent operational
workflow across all relevant views.

- Redesign the Maintenance page around Host and Container target modes.
- Present `preparing`, `ready_to_stop`, `maintenance`, `returning`, `verifying`,
  `blocked`, and `recovery_required` consistently.
- Show target scope, affected clusters and roles, allocation guard owner,
  checkpoints, elapsed time, planned return, blockers, and next actions.
- Mark expected target downtime as planned maintenance while keeping independent
  cluster and peer-workload degradation visible.
- Project state into Dashboard, Hosts, Roles, managed workload tables, and the
  concurrent run console.
- Preserve keyboard focus, loading, empty, degraded, retry, SSE reconnect,
  paused auto-scroll, historical logs, route changes, and responsive layouts.
- Preserve existing maintenance history and legacy route contracts.

**Exit gate:** Frontend unit tests and desktop/mobile browser smoke flows prove
consistent state, independent degraded views, accessible dialogs, and correct
run-console behavior.

**Current status 2026-08-05:** Plan history opens a live detail view with the
persisted preview, checkpoint progress, generic maintenance controls, and the
recovery action; the workspace polls active detail state. Hosts project active
host maintenance by durable target ID. The page still needs scoped action
dispatch for container and host workflows, state-aware action availability, and
frontend validation before this milestone can be marked complete.

## Milestone 8: Full Verification And Release Gate

**Outcome:** Mutation capabilities are enabled only after source, image, live,
recovery, cleanup, and safety evidence is complete.

- Run focused backend, frontend, migration, Ansible or stub, redaction,
  ownership-boundary, route, import, and source-safety tests.
- Run the repository five-minute profile.
- Run the fifteen-minute profile for the built frontend and single-image
  artifact.
- Back up the live database and perform isolated candidate smoke before any
  live replacement.
- Run lab Happy Flow followed by Chaos Mode for container and host maintenance
  from fresh verified baselines.
- Verify allocation-setting restoration, no stale locks or runs, managed-only
  cleanup, unrelated-resource preservation, persistent mounts, and database
  restart recovery.
- Record a redacted evidence ledger and obtain explicit release-artifact
  approval for each mutation capability separately.

**Exit gate:** Every item in `PROMPT.md` under `Done When` is demonstrated, the
full required profiles pass, cleanup evidence is complete, and only the
approved capabilities are enabled in the release artifact.

**Current status (2026-08-05):** The local five-minute profile and configured
build-host fifteen-minute profile have passed, including 535 backend tests, 104
frontend tests, type checking, the production frontend build, bundled Ansible
syntax checks, the single-image build, and isolated candidate smoke. The
candidate was independently retested inside the image and inspected without a
source mount; it contains the application, compiled static assets, Ansible
runtime and playbooks, tests, and smoke tooling. M4b and M5 application
integration are complete and their focused tests pass. This milestone remains
unchecked because destructive lab container and host Happy Flow/Chaos Mode
rounds, their database-backup and cleanup evidence, and explicit
per-capability release approval have not been authorized. The candidate's
desktop and mobile browser shell acceptance has passed. The release allow-list
remains empty.

## Milestone Dependencies

```text
M0 Contract
  -> M1 Capabilities and state
    -> M2 Planning and preflight
      -> M3 Allocation guard
        -> M4a Managed workload runtime port
          -> M4b Container execution integration
            -> M5 Host maintenance
            -> M6 Recovery
              -> M7 Operator experience
                -> M8 Verification and release
```

Frontend contract work may proceed beside backend implementation after M1, but
mutation UI must remain disabled until the corresponding backend milestone and
exit gate are complete. Container maintenance must establish the reusable
single-workload path before host maintenance composes multiple workloads.

## Primary Risks

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Allocation guard remains active | Replica allocation remains suppressed | Durable owner, exact before-state, visible recovery, restart tests |
| Planning flag enables mutation | Unreviewed maintenance action becomes available | Separate capabilities and release approvals |
| Disabled entry blocks exit | Host remains locked indefinitely | Cleanup and recovery are independent and always available for active plans |
| Host scope misses another cluster | Unsafe cross-cluster outage | Aggregate from controller-owned host and assignment projections |
| Container stop affects peers | Unnecessary service disruption | Assignment-scoped adapter and peer-isolation tests |
| Reconciler restarts target | Operator cannot keep target stopped | Persist intentional maintenance desired state |
| Target never returns | Yellow cluster or stale lock | Deadline-driven recovery, allocation restoration, evacuation handoff |
| Controller restarts mid-action | Ambiguous remote state | Persisted checkpoints and read-only rediscovery before further mutation |

## Review Decisions Required Before Implementation

- Confirm whether host maintenance initially supports reboot only, shutdown
  only, both, or a tracked operator handoff before enabling remote execution.
- Confirm whether the first container slice covers all workload roles or starts
  with Elasticsearch data nodes and one stateless role.
- Confirm the default maintenance deadline and whether operators may extend it
  while a guard is active.
- Confirm the policy for restoring normal allocation when a data node misses
  its return deadline.
