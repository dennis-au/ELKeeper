# ELKeeper Node Maintenance And Upgrade Plan

## Document Status

- Status: Planning, persistence, and guarded controller release complete; execution gates pending
- Date: 2026-08-04
- Audience: ELKeeper maintainers, reviewers, and regression-test operators
- Scope: Phased implementation. Operator mutations remain disabled until their phase gates pass.

## Implementation Status

- Phase 0 persistence, ownership, locking, redaction, and startup recovery are implemented. Startup now invokes named host, workload, observability, and Elasticsearch projection contracts to classify persisted checkpoints as complete, incomplete, ambiguous, or recovery-required before transient artifacts are cleaned. The default startup adapter is local and read-only; it performs no SSH, Ansible, Podman, or Elasticsearch call.
- Phase 1 policy, observation, predicate, impact, plan compiler, generic preview/list APIs, and the `/maintenance` workspace are implemented without enabling remote mutation. A preview persists only its plan, steps, and audit event; it never creates a run or lock. The workspace provides plan history, plan details, capability state, and in-page manual-maintenance controls.
- Phase 2 manual maintenance mode is implemented as a controller-only state machine: it persists a plan, run, audit event, host state, lock, expiry, and fresh-health exit/recovery path without remote I/O. A production-shaped reboot composition factory now joins the CA-verified Elasticsearch, allocation guard, controller I/O, signed executor, reboot orchestration, and post-return contracts, but its orchestrator is always disabled and it remains unregistered.
- Phase 3 now persists one-workload maintenance previews with ordered steps, role-specific readiness/disruption contracts, checkpoints, and durable recovery classification. Workload-batch recovery can consume an injected public observation decision rather than assuming the completed list is authoritative. Redacted checkpoint progress is projected into workload rows, the selected-cluster Dashboard, terminal topology role boxes, and the action console's recovery-required state. Stateless artifacts may later restore a compatible prior artifact; Elasticsearch becomes recovery-required if a new process may have opened its data path. No workload executor is registered or enabled.
- Phase 4 now routes the legacy upgrade endpoint through a maintenance-owned, immutable digest plan. It persists an ordered per-assignment manifest, checkpoints, rollback policy, audit event, and closed planning run while preserving the established `run_id` response. It does not launch Ansible, Podman, or remote work until an approved executor is assembled; Elasticsearch remains recovery-required after a new process starts rather than being auto-downgraded.
- Phase 5 now derives evacuation preview evidence only from controller-owned projections: provider/ownership, maintenance policy, host status and zones, memberships and NIC observations, active assignments, role ports, encrypted resource summaries, and image observations. Endpoint-only ECK/external providers remain read-only. Missing durable allocatable capacity is a fail-closed `replacement_capacity_unobserved` blocker, not an operator-supplied override. No drain, replacement, or provider mutation adapter exists.
- Mutation flags are now a two-part gate: a runtime request plus an explicit release-artifact approval. The current release artifact approves no mutation capability.
- The verified Phase 0-5 controller artifact is deployed on the controller host after a non-empty database backup, isolated candidate smoke, and authenticated post-release checks. The release changed no managed Elastic workload host and did not alter any execution capability.
- Phase 2 live reboot execution remains disabled until runtime adapters, single-image packaging, controller-disconnect tests, redundant-topology live acceptance, cleanup evidence, and the full phase ledger pass.
- The legacy host reboot endpoint remains on its existing path until the Phase 2 exit gate is complete.

## Summary

ELKeeper will adopt the safety model used by Elastic Cloud on Kubernetes (ECK)
without adopting Kubernetes itself. Planned node maintenance, rolling restarts,
resource-driven restarts, and Stack upgrades will use one shared orchestration
engine with:

- immutable operation plans;
- role-aware disruption budgets;
- Elasticsearch safety predicates;
- one-node-at-a-time execution;
- persisted checkpoints and recovery state;
- explicit rollback boundaries;
- controller-independent completion of an already-started host reboot; and
- complete run, audit, and cleanup evidence.

The primary availability requirement is:

> When the ELKeeper controller is unavailable while no mutation is active,
> existing Elastic Stack services continue serving traffic without depending on
> the controller.

ELKeeper remains the management plane only. Workload traffic, Elasticsearch
transport, systemd restart behavior, local images, certificates, configuration,
and data must not depend on controller availability.

## Goals

- Make manual host maintenance mode, planned host reboot, and rolling upgrade
  safe, observable, and resumable.
- Prevent ordinary operations from breaking master quorum or removing the last
  available shard copy.
- Limit planned unavailability according to an explicit cluster policy.
- Reuse the same safety predicates for maintenance, upgrade, resource changes,
  workload restart, detach, purge, and future host evacuation.
- Keep existing APIs, assignments, Quadlets, storage paths, certificates,
  credentials, runs, and SSE behavior compatible.
- Preserve unrelated host resources and the existing `ecp-*` ownership boundary.
- Keep ELKeeper non-critical to the steady-state availability of managed services.

## Non-Goals

- Reimplement Kubernetes, StatefulSets, CRDs, or a general-purpose scheduler.
- Add ZooKeeper or an active-active ELKeeper control plane.
- Route Elasticsearch, Kibana, Fleet, Logstash, or Agent traffic through ELKeeper.
- Automatically force unsafe maintenance to make a blocked plan progress.
- Automatically downgrade Elasticsearch after a failed upgrade.
- Manage operating-system package upgrades in the first delivery phase.
- Move arbitrary files, containers, mounts, or services not owned by ELKeeper.

## Current Baseline

The implementation already provides persistence and workload primitives that
must be extended rather than replaced. The maintenance engine itself is not
implemented yet. Existing recovery and locking are limited to selected
workload-batch paths and must not be treated as complete maintenance support:

- SQLite cluster, membership, assignment, observation, run, and audit records.
- Assignment revisions and optimistic concurrency checks.
- Rollback-capable workload change batches with limited controller-restart
  rollback; general runs do not yet rediscover side effects.
- Persistent rootful Podman Quadlets with local configuration, certificates,
  images, and data paths.
- Partial cluster-scoped exclusion through active run checks; host-wide locks
  and cross-cluster impact locks do not yet exist.
- Version discovery, download-only, preflight, and upgrade endpoints.
- A host reboot playbook and an in-page host action in the frontend.
- Run output streaming through the existing SSE action console.

The maintenance engine must preserve all of these interfaces. It must not rewrite
existing assignments or restart workloads merely because a database migration or
controller upgrade was installed.

## ECK Principles To Adopt

### Declarative Intent And Observed State

ECK compares a desired resource specification with observed Kubernetes and
Elasticsearch state. ELKeeper will use the same principle through immutable
maintenance plans and persisted step observations:

- desired action: what the operator approved;
- planned impact: workloads and clusters expected to be disrupted;
- observed state: current host, container, and Elasticsearch facts;
- applied checkpoint: the last side effect verified successfully; and
- conditions: why an operation is ready, progressing, blocked, degraded, or in
  recovery.

### Safety Predicates

ECK evaluates predicates before allowing a node restart. ELKeeper will implement
stable, documented predicates and show every result in the plan preview. A
predicate is evaluated again immediately before its protected side effect.

Predicates are not hidden inside Ansible tasks. The controller stores their
redacted inputs and outcome so an operator can understand why a plan proceeded or
stopped.

### Change Budget

ECK supports `maxUnavailable` and `maxSurge` constraints. ELKeeper will initially
support the applicable subset:

- `max_unavailable`: maximum planned unavailable managed workloads per cluster;
- role-specific minimum availability for master, data, Kibana, Fleet Server,
  Logstash, and coordinating workloads; and
- `max_surge`: reserved for a later replacement/evacuation workflow and fixed to
  zero until ELKeeper can provision replacement workloads safely.

The default is one unavailable workload per cluster, subject to stricter quorum,
shard, and service-replica checks. A budget never overrides Elasticsearch safety.

### One Node At A Time

An Elasticsearch rolling restart or upgrade changes exactly one Elasticsearch
node at a time. The next node is not touched until the previous node:

- is running the expected image and configuration;
- has rejoined the expected cluster UUID;
- reports the expected node name and version;
- has completed local primary recovery;
- no longer has an active shutdown marker; and
- leaves the cluster within the configured health and shard budget.

### In-Progress Operations

ECK exposes node-level progress. ELKeeper will add persistent maintenance steps
and expose them through the existing run console and new maintenance status API.
The run log remains useful for humans, but the database status is authoritative.

## Maintenance Policy

Each cluster receives an effective maintenance policy. Existing clusters use
defaults without receiving a database row until an operator saves a customized
policy.

Recommended defaults:

| Setting | Default | Meaning |
| --- | --- | --- |
| `max_unavailable` | `1` | At most one planned workload unavailable at a time |
| `minimum_master_eligible` | quorum | Keep a majority of current master-eligible nodes available |
| `minimum_data_per_tier` | `1` | Keep at least one healthy node in each active data tier |
| `minimum_kibana` | `1` | Keep at least one ready Kibana instance |
| `minimum_fleet_server` | `1` | Keep at least one ready Fleet Server instance |
| `minimum_logstash` | `1` | Keep at least one ready Logstash instance when its endpoint is used |
| `allow_agent_interruption` | `true-with-warning` | Agent interruption does not block a host reboot but is recorded |
| `required_cluster_health` | `green` | Conservative default for planned Elasticsearch disruption |
| `allocation_guard` | `primaries-for-data` | Temporarily block replica allocation only for data-node disruption |
| `observation_max_age_seconds` | `120` | Safety observations older than this block execution |
| `restart_allocation_delay` | unset | Use the Elasticsearch default unless explicitly configured |
| `host_return_timeout_seconds` | `900` | Maximum wait for SSH and systemd after reboot |
| `workload_ready_timeout_seconds` | `900` | Maximum wait for each workload readiness check |
| `plan_validity_seconds` | `300` | Re-plan when execution does not begin within five minutes |

Policy validation must reject configurations that cannot make progress, such as
`max_unavailable=0` for an in-place reboot. A policy cannot weaken hard safety
predicates involving cluster identity, quorum, last shard copies, downgrade
prevention, or managed-path ownership.

## Safety Predicates

The first implementation will use the following stable predicate identifiers:

| Predicate | Applies to | Pass condition |
| --- | --- | --- |
| `HostEnabled` | all | Inventory host is enabled and initialized |
| `HostReachable` | all | Fresh authenticated SSH probe succeeds |
| `NoConflictingOperation` | all | No overlapping cluster, workload, host, upgrade, or recovery run exists |
| `MembershipReady` | cluster workloads | Configured data and user interfaces and addresses exist on the host |
| `FreshRuntimeObservation` | all | Container, systemd, image, and host facts are within policy age |
| `ExpectedClusterIdentity` | Elasticsearch | CA-verified endpoint reports the configured cluster name and UUID |
| `SupportedNodeLifecycleMode` | Elasticsearch | Selected shutdown backend is supported and its preflight succeeds |
| `ClusterHealth` | Elasticsearch | Health meets policy and is not red |
| `NoShardMovement` | Elasticsearch | No initializing or relocating shards before a data-node allocation guard; bounded wait is recorded |
| `NoLastShardCopy` | data roles | Target node does not hold the only usable copy of a shard |
| `PrimaryPromotionSafety` | data roles | Every target primary has an available in-sync copy or remains safely available |
| `AllocationSettingCaptured` | data roles | Persistent and transient allocation values were captured before mutation |
| `MasterQuorum` | master-eligible | Remaining available master-eligible nodes preserve quorum |
| `RoleAvailabilityBudget` | all | Aggregate host impact stays within role and cluster budgets |
| `DiskWatermarksSafe` | data roles | Remaining nodes have capacity to recover or accept required shards |
| `TargetArtifactReady` | upgrades | Required image is locally cached and matches the selected digest |
| `VersionTransitionSupported` | upgrades | No downgrade or unsupported major-version jump is requested |
| `SnapshotRecoveryReady` | major upgrades | Required recent snapshot and repository checks pass |
| `NoStaleShutdownRecord` | Elasticsearch | No unrelated or abandoned node-shutdown record exists |

`NoLastShardCopy`, `MasterQuorum`, `ExpectedClusterIdentity`, and
`PrimaryPromotionSafety`, `AllocationSettingCaptured`, and
`VersionTransitionSupported` are never forceable. A planned singleton service
outage may be allowed only through a separate audited maintenance mode that shows
the exact expected outage and requires typed in-page confirmation.

## Elasticsearch Maintenance Backend

Create an internal `ElasticsearchMaintenanceBackend` boundary with two
implementations. The backend is selected per cluster and recorded in every plan.
It must never switch backend after an operation has started.

### Documented Rolling Backend

This is the default initial backend. It follows the public Elasticsearch rolling
restart and upgrade guidance using CA-verified APIs, allocation controls where
appropriate, one-node-at-a-time restart, rejoin verification, and guaranteed
restoration of any temporary cluster setting.

Every temporary setting must be captured before modification and restored to its
exact previous value, not an assumed default. Persistent and transient values are
captured independently; `null` is sent only for a setting that was absent in the
corresponding layer.

### Data-Node Allocation Guard

For a planned data-node restart, ELKeeper uses a short-lived allocation guard to
avoid rebuilding replica copies onto other nodes while the original node is
expected to return shortly. This is a cluster-wide setting, so it is applied only
when the target Elasticsearch workload has a data role. Master-only,
coordinating-only, ingest-only, and ML-only workloads skip this guard.

The guarded sequence is:

1. Verify `ClusterHealth`, `NoShardMovement`, `NoLastShardCopy`, and
   `PrimaryPromotionSafety`.
2. Capture both `persistent.cluster.routing.allocation.enable` and
   `transient.cluster.routing.allocation.enable`, including whether each key was
   absent.
3. Set the persistent allocation value to `primaries` through the CA-verified
   `/_cluster/settings` API.
4. Read the setting back and verify that the effective value is `primaries`.
5. Restart exactly one data node using the selected maintenance backend.
6. Verify cluster UUID, persistent node ID, node name, version, and local primary
   recovery. An in-sync replica may be promoted if the target held a primary;
   the guard does not guarantee that a primary remains assigned when no usable
   copy exists.
7. Restore the exact captured persistent and transient values. Use `null` only
   when that layer was previously absent; restore `all`, `none`, or any other
   prior value verbatim.
8. Wait for primary and replica recovery, no initializing or relocating shards,
   policy-compliant health, and the expected active-shard counts.
9. Only then proceed to the next node or release the maintenance plan.

If any step fails, restoration is attempted before the operation becomes
`recovery_required`. If restoration cannot be verified, ELKeeper blocks further
maintenance and reports the exact cluster setting and layer requiring recovery.
The original persistent data path may allow local shard-copy reuse, but the plan
must verify recovery results and must not assume that network recovery was avoided.

The allocation guard is not used with the Node Shutdown API backend unless the
cluster policy explicitly enables both mechanisms. When both are enabled, the
plan records their ordering and cleanup independently.

### Node Shutdown API Backend

This backend adapts the mechanism ECK uses for rolling restarts:

1. Resolve the persistent Elasticsearch node ID through the structured nodes-info
   API, never `_cat/nodes` parsing.
2. Register `PUT /_nodes/{node_id}/shutdown` with type `restart`, a run-specific
   reason, and the configured allocation delay.
3. Poll `GET /_nodes/{node_id}/shutdown` and interpret `not_started`,
   `in_progress`, `stalled`, and `complete` explicitly.
4. Stop or restart the workload only when the shutdown preparation is complete.
5. After the node rejoins and readiness passes, explicitly call
   `DELETE /_nodes/{node_id}/shutdown`.
6. Verify the shutdown record is absent before proceeding.

Elastic documents this API as designed for indirect use by ECE and ECK and says
direct use is unsupported. Therefore ELKeeper must keep this backend behind an
explicit cluster capability flag until it has version-specific stub and live
coverage. It must not silently use the API merely because the endpoint exists.

## Operation Types

### Manual Host Maintenance Mode

An authenticated operator can explicitly put a host into maintenance mode before
approved out-of-band work. This is a persistent controller state, not a
best-effort browser toggle. Entry is non-disruptive: it must not reboot the host,
drain shards, stop workloads, change Elasticsearch allocation, or alter Quadlets.

Entry creates a fresh `manual-maintenance` plan with a reason and policy-bounded
expiry, refreshes the host and every affected cluster, rejects conflicting active
operations, then records `host_maintenance_state=maintenance`, creates a run,
and emits an audit event. While active, ELKeeper blocks new controller-initiated
deployments, resource changes, upgrades, restarts, detach/purge operations, and
automatic reconciliation targeting that host; existing workloads remain running.

Exit is explicit. It creates a run and refreshes SSH, Podman, Quadlet, workload,
endpoint, and affected Elasticsearch observations before clearing the maintenance
guard. Failed readiness, an expired session, or unresolved maintenance artifacts
leave the host in `recovery_required`; expiry must never silently make an
unhealthy host available. Manual mode does not bypass the independent quorum,
shard-copy, budget, or freshness predicates required by reboot and evacuation.

### In-Place Host Reboot

An in-place reboot affects every managed workload on the host across every
cluster. The plan must calculate aggregate impact before any cluster is changed.

Execution order:

1. Acquire a host lock and locks for every affected cluster and assignment.
2. Refresh host, container, image, endpoint, and Elasticsearch observations.
3. Evaluate all safety predicates for all affected clusters as one atomic plan.
4. Record exact pre-operation workload states and configuration/image digests.
5. Prepare each affected Elasticsearch data node using the selected backend and
   data-node allocation guard; non-data nodes skip the guard.
6. Stage the one-shot host maintenance executor described below.
7. Reboot through systemd and allow the controller SSH connection to close.
8. Wait for the host, Podman socket, Quadlet generator, and systemd to return.
9. Verify every workload that was running before maintenance is running again.
10. Verify all cluster UUIDs, node membership, versions, service endpoints, and
    disruption budgets.
11. Restore every captured allocation layer and clear every temporary shutdown
    marker. Verify the effective allocation value and cluster settings after
    cleanup.
12. Release locks, leave maintenance mode, and mark the run succeeded.

If a singleton Kibana, Fleet Server, Logstash endpoint, or quorum-critical
Elasticsearch node would become unavailable, the default zero-impact plan is
blocked. ELKeeper must explain which additional instance or topology change is
required.

### Rolling Workload Restart

A rolling restart uses the same engine but does not reboot the operating system.
It is used for certificate rotation, selected configuration changes, resource
changes requiring restart, and operator-requested restart.

Only the selected workload is restarted. Peer workloads on the same host must not
be restarted. Elasticsearch workloads use the maintenance backend; stateless
services use role availability budgets and service-specific readiness checks.

For an Elasticsearch data workload, the allocation-guard sequence is completed
and replica recovery is verified before the restart operation succeeds. A
non-data Elasticsearch workload does not change shard allocation settings.

### Rolling Stack Upgrade

The existing `POST /api/clusters/{cluster_id}/upgrades` contract remains. Its
implementation will create and execute a maintenance plan rather than use a
separate direct reconcile loop.

Upgrade phases:

1. Refresh all running versions, image digests, health, and membership facts.
2. Validate compatibility, upgrade path, artifact cache, snapshot gate, and
   redundancy.
3. Record an immutable target-version and target-digest manifest.
4. Upgrade supported Elasticsearch data tiers in official order: frozen, cold,
   warm, hot, then remaining data nodes.
5. Upgrade ingest, coordinating, machine-learning, transform, and other
   non-master Elasticsearch nodes.
6. Upgrade dedicated master-eligible nodes last, one at a time.
7. Upgrade Kibana only after every Elasticsearch node is on the target version.
8. Upgrade Fleet Server, Logstash, Elastic Agent, Metricbeat, and Filebeat using
   their explicit compatibility and readiness rules.
9. Refresh observations and commit desired assignment versions only after each
   workload succeeds.

The current legacy `master` assignment combines master, hot, and content roles and
the current assignment model allows only one bootstrap master. Existing clusters
must remain valid, but zero-downtime maintenance or upgrade of that workload is
blocked unless live topology proves another master-eligible quorum and shard
availability. Full rolling-upgrade support depends on the planned flexible node
profiles and multiple master-eligible assignments. No migration may silently
change the effective roles of an existing workload.

### Host Evacuation And Permanent Removal

This is a later phase built on the same engine:

- mark the host `draining` and reject new assignments;
- create replacement workloads on eligible hosts before removing existing ones;
- migrate shards and verify role availability;
- use shutdown type `remove` only with an enabled and tested backend;
- stop and purge only controller-managed resources after evacuation completes;
- preserve unrelated host resources; and
- return the host to `maintenance` or `available` explicitly.

The first maintenance release must not claim host evacuation support.

## Controller-Outage Behavior

### Idle Controller Outage

When no mutation is active, stopping ELKeeper must cause no workload restart,
traffic interruption, certificate reload, Quadlet rewrite, or Elasticsearch
configuration change. Managed hosts continue using systemd and local files.

### Outage Before First Side Effect

The operation remains `planned` or `blocked`. It is safe to re-plan after the
controller returns. No automatic remote mutation occurs.

### Outage During A Workload Operation

The run is marked `recovery_required` on controller startup. ELKeeper observes the
actual host and cluster state before offering resume, abort, or recovery. It does
not assume that the last logged command completed.

### Outage During Host Reboot

Use an operation-specific, one-shot systemd executor on the managed host so an
already-approved reboot can reach a stable state without the controller:

- stage an `0600` operation manifest under the cluster-owned maintenance path;
- install and enable an `ecp-maintenance-resume@<operation-id>.service` unit;
- record the pre-reboot checkpoint before invoking reboot;
- after boot, wait for the exact previously-running Quadlets;
- perform local endpoint checks with existing CA and protected credentials;
- clear shutdown records or restore temporary cluster settings when possible;
- write a redacted result file; and
- disable itself after reaching `complete` or `recovery_required`.

This is not a continuously running node agent. It exists only for an approved
maintenance operation and is removable through the managed ownership boundary.
The controller imports its result and deletes the completed operation artifacts
when connectivity returns.

If the one-shot executor cannot prove safe completion, it must stop making changes
and leave a redacted recovery record. It must never purge data or select a rollback
image autonomously.

## Rollback And Recovery

Rollback behavior is role-aware and phase-aware.

### Before A New Elasticsearch Process Starts

If image staging, Quadlet rendering, or systemd preparation fails before the new
Elasticsearch process opens its data path, ELKeeper may restore the previous
Quadlet and exact image digest, then restart the previous workload.

### After A New Elasticsearch Process Starts

Do not automatically downgrade Elasticsearch. Stop the rollout, preserve the
failed node and diagnostics, restore temporary allocation/shutdown state when
safe, and mark the operation `recovery_required`. Recovery choices are:

- retry the same target version after remediation;
- replace the failed node with a clean target-version node; or
- restore from a verified snapshot according to an explicit recovery plan.

### Stateless And Edge Components

Kibana, Fleet Server, Logstash, Elastic Agent, Metricbeat, and Filebeat may restore
their previous Quadlet, configuration, and pinned image when service-specific
readiness fails, provided compatibility with the already-upgraded Elasticsearch
cluster is still valid.

### Host Reboot Failure

There is no operating-system rollback. If the host does not return, ELKeeper marks
it unavailable, leaves the plan in `recovery_required`, evaluates cluster impact,
and blocks further planned disruption. Future evacuation can replace workloads;
the first release provides diagnosis and manual recovery guidance only.

## Persistent State Model

Use additive SQLite migrations only.

### `maintenance_policies`

- `cluster_id` primary key and foreign key;
- `policy_json` validated non-secret policy;
- `revision` for optimistic concurrency;
- `updated_by` and `updated_at`.

### `maintenance_plans`

- immutable `id`, optional `run_id`, and operation kind;
- target host, cluster, or assignment identifiers;
- redacted `plan_json` containing ordered steps and predicate results;
- `plan_hash`, policy revision, and observed-state timestamps;
- lifecycle state: `draft`, `ready`, `blocked`, `executing`, `paused`,
  `recovery_required`, `succeeded`, `failed`, or `cancelled`;
- requester, approval time, creation time, and expiry time.

Plans never contain passwords, API keys, private keys, enrollment tokens, or
private certificate material.

### `maintenance_steps`

- operation/plan ID and stable step sequence;
- affected cluster, assignment, node, and Elasticsearch node ID;
- step kind, state, attempt count, and timestamps;
- redacted before/after observation JSON;
- last error category and resumability decision.

### `host_maintenance_state`

- node ID;
- state: `available`, `planning`, `maintenance`, `draining`, or
  `recovery_required`;
- active plan ID;
- entered and updated timestamps;
- manual-mode requester, redacted reason, expiry, and last successful
  observation timestamp while in `maintenance`; and
- durable exit-verification evidence or recovery reason when a host cannot
  safely return to `available`.

Existing cluster, membership, assignment, run, observation, and audit schemas
remain authoritative. Maintenance tables reference them rather than duplicating
secrets or desired workload configuration.

### `maintenance_locks`

- lock scope and identifier for host, cluster, assignment, or recovery scope;
- owning plan ID, run ID, and opaque owner token;
- acquired, heartbeat, and expiry timestamps;
- uniqueness across each active scope and identifier; and
- stale-lock recovery metadata and the observation that justified release.

Locks are transactional coordination records, not evidence that a remote side
effect completed. Releasing an expired lock requires state rediscovery whenever
its owner reached or may have reached an executable checkpoint.

## Public APIs

All mutations authenticate through the existing dependency, create or attach to a
run, return `run_id`, emit audit events, and stream through the existing SSE API.

### Policy

- `GET /api/clusters/{cluster_id}/maintenance-policy`
- `PUT /api/clusters/{cluster_id}/maintenance-policy`

Policy updates use an expected revision and do not restart workloads.

### Plan And Execution

- `POST /api/nodes/{node_id}/maintenance/plans`
- `GET /api/maintenance/plans/{plan_id}`
- `POST /api/maintenance/plans/{plan_id}/execute`
- `POST /api/maintenance/plans/{plan_id}/pause`
- `POST /api/maintenance/plans/{plan_id}/resume`
- `POST /api/maintenance/plans/{plan_id}/cancel`
- `POST /api/maintenance/plans/{plan_id}/recover`
- `POST /api/nodes/{node_id}/maintenance-mode/enter`
- `GET /api/nodes/{node_id}/maintenance-mode`
- `POST /api/nodes/{node_id}/maintenance-mode/exit`

Initial plan request:

```json
{
  "operation": "reboot",
  "reason": "Operating-system maintenance",
  "availability_mode": "zero-impact"
}
```

Planning is non-mutating. Execution rejects expired plans, changed assignment
revisions, stale observations, policy changes, or a changed plan hash.

Manual maintenance-mode entry and exit are tracked mutations. Entry accepts a
reason and policy-bounded expiry, returns `run_id`, and makes no remote workload
change. Exit returns `run_id` and releases the guard only after fresh readiness
checks succeed; otherwise the result is blocked or `recovery_required`.

Pause and cancel take effect only at a safe checkpoint. They do not interrupt an
active host reboot or kill an Elasticsearch process mid-transition.

### Compatibility Adapters

- Preserve `POST /api/nodes/{node_id}/reboot`. It creates a fresh zero-impact
  reboot plan, evaluates it, and executes only when ready. Existing callers still
  receive `run_id`; blocked requests return a clear `409` or `422` without host
  changes.
- Preserve `POST /api/clusters/{cluster_id}/upgrades` and its existing payload.
  Internally it creates a maintenance plan and uses the shared engine.
- Preserve assignment apply, resource update, batch apply, detach, and purge
  endpoints. Maintenance locks prevent conflicts; those APIs are migrated to the
  shared predicates incrementally.
- Preserve run list, run event SSE, frontend action-console behavior, and existing
  run-history rows.

Legacy endpoints are not removed or silently changed to asynchronous semantics
that differ from their current `run_id` contract.

## UI Design

### Hosts Page

- Replace the direct conceptual reboot action with `Plan maintenance` while
  retaining the existing reboot API adapter.
- Add feature-gated `Enter maintenance mode` and `Exit maintenance mode` actions
  with in-page confirmation, reason, expiry, affected-cluster impact, and a clear
  explanation that entry does not stop workloads or reboot the host.
- Show host maintenance state, affected clusters, affected workloads, and current
  operation.
- Present predicate results grouped as passed, warning, or blocking.
- Show estimated outage explicitly for singleton services.
- Require in-page confirmation; never use browser-native dialogs.

### Maintenance Plan View

- Header: host, operation type, reason, requester, plan freshness, and policy.
- Impact band: workloads, endpoints, master quorum, data tiers, and agents.
- Ordered steps with current state and last verified checkpoint.
- Blocking conditions with concrete remediation.
- Execute, pause, resume, cancel, and recover actions only when valid.
- Keep manual maintenance entry and exit distinct from reboot execution, with
  their own available, active, expired, and recovery-required states.
- Linked run output in the existing action console.

### Cluster Upgrade View

- Show the selected version and immutable target image digests.
- Show upgrade order by role and host.
- Show the currently active node, completed nodes, and blocked predicate.
- Distinguish pre-start rollback eligibility from snapshot recovery required.
- Keep download-only independent and non-disruptive.

### Dashboard And Topology

- Show maintenance, draining, and recovery-required host states.
- Mark endpoints whose redundancy would be lost by the planned operation.
- Keep stale observations visibly stale rather than treating them as healthy.

## Security Requirements

- Use CA-verified HTTPS for every Elasticsearch lifecycle and readiness request.
- Use structured APIs and JSON parsing; never parse `_cat` output in application
  logic.
- Put credentials in protected `0600` temporary curl configuration files or use a
  protected structured client. Never place credentials in process arguments.
- Keep one-shot executor manifests free of secrets where possible. Where existing
  workload credentials must be read, use the existing protected local files and
  never copy values into result files or logs.
- Audit plan creation, approval, execution, unsafe override, cancellation,
  recovery, and completion.
- Redact API responses, plan JSON, run logs, support artifacts, and browser state.
- Restrict cleanup to controller-marked maintenance files and `ecp-*` resources.

## Failure Handling

| Failure | Required behavior |
| --- | --- |
| Predicate becomes false before a side effect | Re-plan or block; make no change |
| Elasticsearch shutdown status is `stalled` | Stop and show shard/task/plugin reason |
| Host disconnects before reboot is confirmed | Discover actual boot ID and executor checkpoint |
| Host does not return before timeout | Mark `recovery_required`; disrupt no other node |
| Workload returns with wrong cluster UUID | Stop rollout and quarantine the step |
| Workload returns with wrong image/version | Stop rollout; do not commit desired version |
| Shutdown marker cleanup fails | Mark recovery required and block next maintenance |
| Allocation setting restoration fails | Mark recovery required, preserve the captured layers, and block further maintenance |
| No in-sync primary copy is available | Block before setting `primaries` or stopping the node |
| Replica recovery remains incomplete | Keep the plan blocked and do not touch the next node |
| Controller restarts during a plan | Rediscover state; never infer completion from logs |
| Stateless service readiness fails | Restore previous service artifact when compatible |
| Elasticsearch readiness fails after new start | No downgrade; require retry, replacement, or snapshot recovery |
| Run log streaming disconnects | Operation continues; UI reconnects from persisted run state |

## Implementation Phases

The phases below are deliberately ordered. A later phase must not expose an
operator action whose provider capability, persistence, or recovery path was
only designed in a later phase. Every phase requires its unit/stub tests and a
clean regression run before the next phase is enabled.

### Phase 0: Ownership, Migration, And Recovery Foundation

This phase makes the existing application safe to extend without changing
steady-state workload behavior.

- Add additive, versioned SQLite migrations and migration markers. Preserve
  existing clusters, memberships, assignments, observations, runs, audits, and
  imported metadata. Do not use unconditional legacy-row deletion as migration.
- Add cluster ownership/provider fields and a capability matrix for native
  ELKeeper Podman, adopted Podman, external API-managed, and ECK endpoint-only
  clusters. Imported resources are read-only until ownership and capabilities
  are explicitly verified.
- Add `maintenance_policies`, `maintenance_plans`, `maintenance_steps`, and
  `host_maintenance_state`, plus scoped `maintenance_locks`, with foreign keys,
  indexes, retention rules, and uniqueness constraints preventing overlapping
  active plans.
- Add plan idempotency keys, expected assignment/policy revisions, immutable
  plan hashes, target image-digest manifests, and redacted checkpoint payloads.
- Replace startup blanket failure/rollback with state rediscovery. A restart
  must classify each side effect as complete, incomplete, ambiguous, or safe to
  resume before offering resume, abort, or recovery.
- Introduce transactional host, cluster, assignment, and recovery locks. A host
  lock must aggregate workloads across all clusters sharing that host.
- Add provider-specific connection, CA, and credential references without
  storing secrets in plans, logs, URLs, or browser state.

**Exit gate:** old databases migrate without data loss; idle controller outage
is harmless; restart-at-each-checkpoint tests pass; no maintenance mutation is
available from the UI yet.

### Phase 1: Planning And Safety Core

- Implement non-mutating plan generation, plan hashing, policy defaults,
  capability checks, safety predicates, aggregate host impact, and API responses.
- Make predicate results explicit and redacted, including provider capability,
  shared/dedicated NIC readiness, cluster identity, quorum, shard copies,
  disk watermarks, and service budgets.
- Add plan expiry, stale-observation rejection, changed-revision rejection,
  expected-hash checking, and idempotent execution requests.
- Add plan preview UI with blocked, warning, and passed predicate groups.
- Convert existing reboot, resource, settings, zoning, apply, detach, and purge
  entry points into compatibility adapters that create plans while preserving
  their current `run_id` response contracts. Execution remains feature-gated
  until the relevant later phase is verified.

**Exit gate:** planning is non-mutating, conflicting operations are rejected,
legacy APIs still return `run_id`, and multi-cluster shared-host tests pass.

### Phase 2: Safe In-Place Reboot

- Add feature-gated manual host maintenance-mode entry and exit with persistent
  guards, operation runs, audit records, expiry, conflict checks, and verified
  return-to-service. Manual mode must not mutate remote workloads on entry.
- Implement the documented CA-verified rolling backend and data-node allocation
  guard. Handle transient allocation precedence by either setting both layers
  temporarily or failing closed; restore both layers exactly.
- Add the one-shot host executor with a signed/versioned manifest, `0600` state,
  bounded lifetime, post-boot result import, self-disable, and managed cleanup.
- Implement service budgets, host reboot state machine, host return checks,
  Podman/systemd readiness, cluster UUID/node identity checks, and recovery.
- Preserve `host-reboot.yml` only as a low-level executor step; it must not be a
  public safety boundary.

**Exit gate:** manual maintenance entry/exit, redundant service, and data-node
reboot rounds pass; singleton, last-shard-copy, quorum, stale-shutdown, and
controller-disconnect cases block without host changes; all temporary settings,
manual guards, and executor artifacts are removed.

### Phase 3: Shared Rolling Restart Engine

- Route Elasticsearch restart, certificate rotation, resource changes, and
  operator-requested restarts through the maintenance engine.
- Add service-specific readiness and budgets for Kibana, Fleet Server, Logstash,
  Agent, Filebeat, and Metricbeat. Peer workloads on the same host must remain
  untouched.
- Extend workload-batch recovery to use maintenance checkpoints rather than a
  completed-list assumption. Keep stateless rollback separate from the
  Elasticsearch no-downgrade boundary.
- Add provider capability enforcement so endpoint-only ECK/imported clusters
  can receive supported API maintenance but never Podman/Kubernetes mutations.

**Exit gate:** resource, detach/purge, restart, and certificate rounds verify
  persisted limits, readiness, rollback, peer isolation, and managed-only cleanup.

### Phase 4: Upgrade Integration

- Replace the current direct upgrade loop with maintenance plans and per-node
  checkpoints. Resolve and pin immutable image digests before the first side
  effect; tags alone are insufficient.
- Implement supported-role ordering from a capability matrix. Do not promise
  frozen, cold, transform, or other roles until their renderers exist.
- Verify every node’s cluster UUID, persistent node ID, node name, image digest,
  version, shard recovery, health, and endpoint readiness before advancing.
- Enforce downgrade, major-jump, snapshot, health, quorum, and three-master
  gates. A recent snapshot must include repository/listing validation, not only a
  snapshot list response.
- Keep Elasticsearch rollback phase-aware: restore artifacts only before the
  new process opens the data path; after a successful new start, stop and mark
  `recovery_required` rather than downgrading automatically.
- Keep the Node Shutdown API backend optional, version-gated, capability-gated,
  and disabled by default until stub and live evidence proves compatibility.

**Exit gate:** download-only causes no restart or desired-state change; rejected
  upgrades restart nothing; stateless rollback and Elasticsearch recovery paths
  are separately tested.

### Phase 5: Evacuation, Replacement, And External-Provider Operations

- Add host draining, eligible replacement selection, `max_surge`, capacity and
  failure-domain checks, shard migration, and permanent removal.
- Add explicit maintenance adapters for imported external clusters. Support only
  capabilities proven by the provider: API settings, endpoint probes, and
  operator-approved node maintenance where possible.
- Keep ECK endpoint-only imports read-only for Kubernetes lifecycle operations;
  never mutate ECK CRs, Pods, StatefulSets, PVCs, or scale-down settings.
- Support mixed ECK/Podman migration only with manually imported transport CA,
  prepared transport endpoints/SANs, explicit certificate handling, and an
  operator-controlled ECK node-set reduction.
- Deliver evacuation and replacement only after flexible node profiles, failure
  domains, capacity inventory, service HA, and provider-specific recovery are
  implemented.

**Exit gate:** migration and evacuation tests prove no unmanaged resource is
  changed, imported resources remain protected, replacement capacity is verified,
  and managed-only cleanup is complete after every destructive test round.

## Detailed Implementation Action Plan

This section converts the architectural phases into bounded work packages. Each
work package must leave the application buildable and testable. Public mutations
remain feature-gated until the exit gate for their phase has passed. A database
migration, controller image replacement, or UI release must never activate an
unfinished maintenance path automatically.

### Delivery Rules

- Implement vertically where possible: persistence, typed model, API behavior,
  frontend state, and focused tests for one capability before widening scope.
- Freeze shared persistence and API contracts before parallel implementation uses
  them. Contract changes after a gate require an explicit root-integrator review.
- Put new maintenance logic in focused modules. Keep `app/main.py` and
  `app/console.py` as integration surfaces rather than expanding every state
  transition inline.
- Keep all new operator mutations behind capability and feature gates until their
  stub, recovery, packaging, and live exit evidence is complete.
- Run focused tests after every work package, the five-minute profile at every
  phase checkpoint, the fifteen-minute profile before any deployment, and the
  full profile before release acceptance.
- Build, package, deploy, and run live-host verification only on the configured
  controller/build host. Sub-agents do not replace the live controller or run
  destructive host tests.
- Record redacted evidence for each gate: schema version, test commands, image
  digest, run IDs, recovery result, temporary-setting cleanup, and managed-only
  host cleanup.

### Phase 0 Work Packages: Ownership, Migration, And Recovery Foundation

Likely integration points include `app/main.py`, `app/console.py`, new
`app/maintenance_*.py` modules, `tests/test_api.py`, and new focused maintenance
test modules.

#### P0.1 Baseline And Feature Gates

- Record current database schema, API contracts, active run states, assignment
  revision behavior, and controller-startup recovery behavior in tests.
- Add disabled-by-default capability flags for maintenance planning, reboot
  execution, rolling restart, upgrade integration, and evacuation.
- Prove that installing the new image with every maintenance flag disabled makes
  no workload, cluster-setting, assignment, or host change.

**Done when:** the old database opens in place, the five existing routes and APIs
remain compatible, and an idle controller restart creates no maintenance run.

#### P0.2 Versioned Additive Migration Runner

- Introduce an explicit migration ledger and ordered, transactional migration
  functions instead of relying only on column-presence checks.
- Create `maintenance_policies`, `maintenance_plans`, `maintenance_steps`, and
  `host_maintenance_state`, plus transactional `maintenance_locks`, with foreign
  keys, indexes, active-plan and lock-scope uniqueness, lifecycle validation, and
  retention metadata.
- Add plan idempotency keys, policy and assignment revisions, plan hashes, target
  digest manifests, and redacted checkpoint fields.
- Add migration fixtures representing the oldest supported database, the current
  live schema, partially migrated databases, and an interrupted migration.

**Done when:** every fixture migrates transactionally without deleting valid
legacy rows, repeated migration is idempotent, and backup/restore tests pass.

#### P0.3 Maintenance Repository And State Transitions

- Add typed repository functions for policies, plans, steps, host state, locks,
  checkpoints, and audit records.
- Centralize legal lifecycle transitions and reject skipped, repeated, or stale
  transitions with optimistic concurrency.
- Store redacted before/after observations and error classifications separately
  from human-readable run output.

**Done when:** transition-matrix tests cover every lifecycle state and a repeated
request cannot duplicate a step or side effect.

#### P0.4 Transactional Lock Manager

- Implement host, cluster, assignment, and recovery lock primitives with stable
  ownership, expiry, heartbeat, and stale-lock recovery semantics.
- Aggregate all clusters and workloads sharing a host before granting a host lock.
- Root integration must then make existing apply, batch, zoning, settings,
  upgrade, detach, purge, and host actions visible to the lock manager before
  converting any execution path.

**Done when:** multi-cluster shared-host and conflicting-run tests prove that only
one overlapping mutation can become executable.

#### P0.5 Ownership And Provider Capabilities

- Persist cluster ownership and provider type: native ELKeeper Podman, adopted
  Podman, external API-managed, and ECK endpoint-only.
- Define a capability matrix for host mutation, workload mutation, cluster-setting
  changes, lifecycle APIs, observation, and recovery.
- Require explicit ownership verification before imported resources become
  mutable.

**Done when:** endpoint-only and unverified resources remain read-only and no
provider can invoke a mutation outside its declared capabilities.

#### P0.6 Startup Rediscovery And Recovery Classification

- Replace blanket startup failure handling with observation-driven classification
  of each incomplete step as complete, incomplete, ambiguous, or safe to resume.
- Reorder startup explicitly: run migrations, preserve incomplete run and
  operation artifacts, establish the observation clients needed for recovery,
  classify checkpoints, and only then clean artifacts that classification proves
  are no longer required.
- Reconcile stored checkpoints with run state, assignment revision, systemd,
  Podman, image, host boot ID, and Elasticsearch identity observations.
- Expose resume, abort, and recovery choices only after rediscovery has produced a
  persisted classification.

**Done when:** restart-at-every-checkpoint tests prove that controller restart does
not repeat a verified side effect or assume success from a log line.

#### P0.7 Security, Redaction, And Retention

- Add structured redaction for plan JSON, checkpoints, API errors, audit details,
  run context, executor results, and support artifacts.
- Define retention and cleanup for completed plans, steps, temporary files, locks,
  and one-shot executor artifacts without deleting run history or desired state.
- Verify that credentials, private keys, API keys, enrollment tokens, certificate
  private material, and temporary authentication files never enter a plan record.

**Phase 0 gate:** run migration fixtures, restart-at-checkpoint tests, API
compatibility tests, the five-minute profile, an isolated single-image migration,
and an idle-controller outage check. No maintenance execution control is visible.

### Phase 1 Work Packages: Planning And Safety Core

#### P1.1 Maintenance Policy Model

- Implement defaults without inserting rows for untouched clusters.
- Validate availability budgets, timeouts, health requirements, allocation-guard
  selection, provider capabilities, and impossible policies.
- Support expected-revision updates and audit every customized policy change.

#### P1.2 Immutable Observation Snapshot

- Collect one bounded planning snapshot containing host, boot, SSH, systemd,
  Podman, container, image, membership, endpoint, cluster UUID, node identity,
  shard, health, disk, setting, and snapshot facts.
- Record source timestamps and independent source failures. Do not convert stale or
  absent observations into healthy values.
- Hash or reference large observations without placing secrets in the plan.
- Implement collection in a dedicated maintenance observation module. The safety
  lane may own structured collectors and fake clients; root owns wiring to the
  existing telemetry manager, SQLite observations, authentication, and startup
  lifecycle.

#### P1.3 Stable Predicate Library

- Implement each documented predicate as a pure evaluator over typed, redacted
  inputs and policy.
- Return stable identifier, severity, outcome, evidence summary, remediation, and
  observation timestamp.
- Mark hard predicates as non-forceable in code and tests rather than relying on UI
  behavior.

#### P1.4 Aggregate Impact And Budget Calculator

- Calculate the effect of a host action across every cluster and workload on that
  host.
- Evaluate master quorum, active data tiers, shard copies, singleton services,
  endpoint loss, agents, and existing unavailable workloads.
- Produce a deterministic impact manifest used by both plan preview and execution.

#### P1.5 Plan Compiler And Hash

- Compile policy, target, provider backend, immutable observations, predicate
  results, impact, ordered steps, rollback boundaries, and expiry into one plan.
- Canonicalize and hash the redacted plan representation.
- Reject execution when the hash, target revision, policy revision, capability,
  or observation freshness has changed.

#### P1.6 Planning APIs And Compatibility Adapters

- Add policy and plan CRUD/read endpoints with typed responses and stable error
  categories.
- Make plan creation non-mutating and idempotent.
- Add plan-preview adapters for reboot, resource, settings, zoning, apply, detach,
  purge, and upgrade without disabling their current verified execution paths.
- Switch each existing mutation to maintenance-plan execution only at the exit
  gate for the phase that implements and tests that operation. Until then, legacy
  behavior and responses remain active behind their existing safety checks.
- Preserve each endpoint's actual response contract. Operations that currently
  return `run_id` continue to do so; detach currently returns its immediate
  compatibility response and requires a deliberate transition contract before it
  becomes asynchronous.

#### P1.7 Plan Preview UI

- Add host and cluster maintenance entry points, impact summary, plan freshness,
  ordered steps, and predicate groups for passed, warning, and blocking results.
- Show exact remediation for quorum, shard, zone, capacity, capability, and
  singleton blockers.
- Keep execute controls absent or disabled until the matching execution capability
  is enabled. Use only in-page EUI dialogs.

**Phase 1 gate:** planning produces no host, Podman, Quadlet, Elasticsearch, or
assignment mutation; stale plans and conflicts fail closed; hard predicates cannot
be overridden; API, frontend, responsive, and multi-cluster shared-host tests pass.

### Phase 2 Work Packages: Safe In-Place Reboot

#### P2.0 Manual Host Maintenance Mode

- Add a feature-gated `manual-maintenance` operation kind and durable host-mode
  guard that is separate from reboot execution.
- Require a fresh non-mutating plan, reason, policy-bounded expiry, conflict
  checks, run creation, and audit event before entering mode.
- Block conflicting controller mutations while active without stopping
  workloads, changing Elasticsearch allocation, or modifying Quadlets.
- Add verified exit that refreshes SSH, Podman, workload, endpoint, and cluster
  state before releasing the guard; unresolved checks become
  `recovery_required`.
- Surface entry, active, expiry, exit, and recovery state on Hosts, Dashboard,
  topology, and the action console. Do not show the controls until the backend
  capability and recovery path are enabled.

#### P2.1 Elasticsearch Maintenance Client And Backend Contract

- Add a protected structured client for CA-verified health, settings, nodes-info,
  recovery, allocation, pending-task, and optional shutdown APIs.
- Implement the documented rolling backend first. Keep backend selection immutable
  after plan creation.
- Add version and provider capability checks for the optional shutdown backend,
  disabled by default.

#### P2.2 Data-Node Allocation Guard

- Capture persistent and transient allocation layers, including absence and
  precedence.
- Wait for no shard movement and verify last-copy and primary-promotion safety
  before setting the guard.
- Set and read back the guarded effective value, then restore both layers exactly
  on success, failure, cancellation, or recovery.
- Persist cleanup checkpoints so unresolved restoration blocks later maintenance.

#### P2.3 One-Shot Host Executor

- Define a signed and versioned manifest containing operation identity, expected
  boot transition, previously running managed units, bounded checks, checkpoint
  location, and expiry without embedded secrets.
- Add managed systemd unit and scripts that wait for the exact Quadlets, perform
  local protected checks, write a redacted result, self-disable, and stop changing
  state when safety cannot be proven.
- Restrict files, units, and cleanup to the controller-owned maintenance path and
  `ecp-*` boundary.

#### P2.4 Reboot State Machine

- Prepare all affected clusters atomically, checkpoint before reboot, stage the
  executor, and invoke the low-level reboot playbook only after every predicate is
  re-evaluated.
- Treat SSH disconnect as expected only after reboot invocation is verified.
- Discover boot ID and executor state after reconnect instead of assuming a reboot
  occurred.

#### P2.5 Post-Return Verification And Cleanup

- Verify SSH, Podman socket, Quadlet generator, systemd units, previously running
  workloads, endpoints, node identities, versions, cluster UUIDs, shard recovery,
  service budgets, and health.
- Restore allocation settings and shutdown records before releasing locks.
- Import the executor result and remove only completed managed executor artifacts.

#### P2.6 Recovery Operations And UI

- Add pause, resume, cancel, and recover semantics that act only at safe
  checkpoints.
- Present active checkpoint, host boot state, unresolved cluster settings,
  executor evidence, and concrete recovery options.
- Do not interrupt an active reboot or kill an Elasticsearch process during a
  transition.

#### P2.7 Reboot Acceptance Rounds

- Cover manual-mode entry with healthy workloads, conflicting-operation
  rejection, controller restart persistence, expiry without silent release,
  failed exit recovery, and successful verified exit.
- Cover redundant stateless services, data nodes, master-eligible nodes,
  multi-cluster hosts, controller termination at every side-effect boundary, host
  timeout, wrong identity, singleton blocks, and last-shard-copy blocks.
- Verify that every blocked plan causes no remote change and every successful plan
  removes settings, markers, locks, units, manifests, and temporary files.

**Phase 2 gate:** manual-mode entry/exit, the documented backend, and one-shot
executor pass stub, controller-disconnect, packaging, and live
redundant-topology tests. The existing reboot compatibility endpoint uses the
engine only after this gate.

### Phase 3 Work Packages: Shared Rolling Restart Engine

#### P3.1 Role Readiness And Disruption Providers

- Implement role-specific readiness, endpoint, compatibility, and minimum-service
  checks for Elasticsearch, Kibana, Fleet Server, Logstash, Agent, Filebeat, and
  Metricbeat.
- Keep data-node allocation behavior separate from non-data Elasticsearch roles.

#### P3.2 Workload Restart Adapter

- Route resource changes, certificate rotation, selected configuration changes,
  and operator restart through one-workload-at-a-time maintenance plans.
- Verify peer workloads on the same host are unchanged.

#### P3.3 Batch Recovery Migration

- Replace completed-list assumptions in workload batches with persisted step
  checkpoints and observation-driven recovery decisions.
- Preserve assignment revision and current compatibility behavior during the
  incremental migration.

#### P3.4 Stateless Rollback Boundary

- Capture prior Quadlet, configuration, and image digest for compatible stateless
  rollback.
- Re-evaluate compatibility with the current Elasticsearch version before restoring
  a prior service artifact.
- Keep this path distinct from the Elasticsearch no-downgrade boundary.

#### P3.5 Provider Enforcement And UI Integration

- Expose only operations supported by the selected provider and ownership state.
- Add workload maintenance progress to Roles, Dashboard, topology, and the action
  console without changing unrelated workflows.

**Phase 3 gate:** resource, restart, certificate, detach, purge, failure, recovery,
peer-isolation, managed-cleanup, frontend, and full single-image regression passes.

### Phase 4 Work Packages: Upgrade Integration

#### P4.1 Immutable Target Manifest

- Resolve every required repository, exact version, image digest, component, role,
  and host before the first side effect.
- Verify each required digest is cached and keep download-only independent from
  desired assignment state.

#### P4.2 Compatibility, Snapshot, And Ordering Rules

- Encode supported transitions, downgrade and major-jump blocks, component
  compatibility, snapshot repository/listing validation, quorum, and health gates.
- Build the role order only from currently supported renderers and capabilities.

#### P4.3 Upgrade Plan Compiler

- Produce ordered, one-node steps for supported data tiers, other Elasticsearch
  roles, master-eligible nodes, Kibana, Fleet Server, Logstash, Agent, Metricbeat,
  and Filebeat.
- Record why skipped or unsupported roles are blocked rather than silently
  ignoring them.

#### P4.4 Per-Workload Upgrade Execution

- Re-evaluate predicates before each restart, verify image digest and version after
  return, wait for recovery and health, and commit desired assignment version only
  after success.
- Stop immediately on failed identity, readiness, shard, or cleanup verification.

#### P4.5 Recovery And Rollback Boundaries

- Restore the prior artifact only before the new Elasticsearch process opens its
  data path.
- After a successful new Elasticsearch start, prohibit automatic downgrade and
  enter `recovery_required` with retry, replacement, or snapshot recovery choices.
- Preserve compatible stateless rollback independently.

#### P4.6 Upgrade UI And Acceptance

- Show immutable targets, role order, active node, completed nodes, blocked
  predicate, cleanup state, and recovery boundary.
- Prove download-only causes no restart or state change, rejected upgrades restart
  nothing, and rolling/stub failure paths preserve the expected boundary.

**Phase 4 gate:** existing upgrade API compatibility, digest pinning, ordering,
snapshot, quorum, recovery, no-downgrade, stateless rollback, browser, packaging,
and live safe-topology tests pass.

### Phase 5 Work Packages: Evacuation And External Providers

#### P5.1 Draining And Placement Eligibility

- Add explicit draining state, reject new assignments, and calculate replacement
  eligibility from role, zone, network, port, storage, CPU, memory, image, provider,
  and maintenance budgets.

#### P5.2 Replacement And Surge Planning

- Implement `max_surge`, capacity reservation, replacement creation, readiness,
  identity, endpoint, and rollback checkpoints before removing an existing
  workload.

#### P5.3 Shard Migration And Permanent Removal

- Verify allocation, recovery, role availability, and managed ownership before
  stopping or purging the old workload.
- Use shutdown type `remove` only through an explicitly enabled and tested backend.

#### P5.4 External And ECK Provider Adapters

- Permit only proven API-level operations for imported providers.
- Keep endpoint-only ECK resources read-only for Kubernetes lifecycle and require
  explicit operator-controlled migration boundaries.

#### P5.5 Evacuation Acceptance

- Prove replacement capacity, failure-domain coverage, provider protection,
  no unmanaged mutations, and complete managed cleanup in stub and live rounds.

**Phase 5 gate:** evacuation, replacement, migration, recovery, and cleanup pass
the full release profile without altering unmanaged resources.

## Sub-Agent Task Allocation Plan

ELKeeper maintenance work may use three sub-agents plus one root integrator. The
root integrator owns shared contracts and merges. Sub-agents work in isolated new
modules and tests wherever possible because all agents share one worktree.

| Lane | Primary responsibility | Exclusive working boundary | Required handoff |
| --- | --- | --- | --- |
| Sub-agent 1: Persistence and recovery | Phase 0 schemas, repositories, plan hashing, revisions, idempotency, locks, checkpoints, and restart classification | New maintenance store, migration helper, lock, and recovery modules plus focused tests; no direct edits to shared routers or frontend | Migration contract, repository API, transition matrix, redaction notes, and restart-at-checkpoint evidence |
| Sub-agent 2: Safety and Elasticsearch | Policies, aggregate impact, predicates, documented rolling backend, allocation capture/restore, node identity, and optional shutdown adapter | New policy, predicate, impact, and Elasticsearch backend modules plus fake-client tests; no shared router, database initialization, or frontend edits | Predicate result schema, backend contract, captured-setting format, failure classifications, and CA-verification evidence |
| Sub-agent 3: Host executor and UI components | One-shot executor artifacts, maintenance-specific playbooks, mocked plan-preview components, progress views, and responsive component tests | New maintenance playbooks/templates and new frontend maintenance component directory; no edits to existing host reboot playbook, shared API/types, global CSS, or existing pages without root handoff | Manifest/result schemas, Ansible syntax and stub evidence, component props/API examples, accessibility and responsive evidence |
| Root integrator | Architecture decisions, shared models, migration wiring, API routes, orchestration, compatibility adapters, existing-file edits, packaging, deployment, and regression | Exclusive ownership of `app/main.py`, `app/console.py`, existing frontend pages, shared `api.ts`, `types.ts`, global styles, `host-reboot.yml`, `Containerfile`, and live deployment | Integrated source, contract decisions, conflict resolution, image digest, database backup, run ledger, browser evidence, and cleanup proof |

### Dispatch Waves

#### Wave 0: Contract Freeze

- Root records the baseline, defines module boundaries, freezes lifecycle states,
  plan/predicate payloads, migration numbering, error categories, feature flags,
  and handoff templates.
- Sub-agents may perform read-only spikes but do not edit shared files.

**Integration gate:** root approves the persistence, API, predicate, executor, and
frontend mock contracts before parallel implementation begins.

#### Wave 1: Phase 0 Foundation

- Sub-agent 1 implements P0.2, P0.3, and P0.4 in new modules and focused tests.
- Sub-agent 2 prepares pure capability, policy, and predicate fixtures without
  enabling execution.
- Sub-agent 3 prepares executor manifest/result schemas and non-mutating UI fixture
  components against frozen examples.
- Root implements P0.1, existing-mutation integration for P0.4, P0.5, ordered P0.6
  startup integration, P0.7 shared redaction, migration wiring, and the Phase 0
  gate.

#### Wave 2: Phase 1 Planning

- Sub-agent 1 extends plan persistence, idempotency, expiry, and checkpoint queries.
- Sub-agent 2 implements P1.1, structured collectors for P1.2, and P1.3 through
  P1.5 pure planning and safety logic.
- Sub-agent 3 implements P1.7 preview components and tests using mocked contracts.
- Root wires P1.2 into current telemetry and database observations, implements
  P1.6 routes and non-disruptive preview adapters, owns shared frontend API/types,
  page wiring, and the Phase 1 gate. Existing mutation execution switches only at
  its later phase gate.

#### Wave 3: Phase 2 Reboot

- Sub-agent 1 implements reboot state persistence and recovery classification.
- Sub-agent 2 implements P2.1 and P2.2 with structured fake Elasticsearch clients.
- Sub-agent 3 implements P2.3 artifacts and P2.6 maintenance UI components.
- Root implements P2.4, P2.5, existing-playbook integration, controller-disconnect
  testing, packaging, deployment, and P2.7 live rounds.

#### Wave 4: Shared Restart And Upgrade Preparation

- Sub-agent 1 migrates workload-batch checkpoints and prepares target-manifest
  persistence.
- Sub-agent 2 implements role readiness, stateless rollback checks, compatibility,
  snapshot, digest, ordering, and upgrade predicate logic.
- Sub-agent 3 implements workload maintenance and upgrade progress components.
- Root integrates Phases 3 and 4 sequentially, preserving existing API contracts,
  then owns all destructive restart and upgrade rounds.

#### Wave 5: Evacuation

- Sub-agent 1 implements draining, reservation, and replacement state persistence.
- Sub-agent 2 implements placement, capacity, failure-domain, migration, and
  provider-capability evaluators.
- Sub-agent 3 implements evacuation preview and progress components.
- Root integrates provider adapters, replacement execution, permanent removal,
  deployment, and live evacuation acceptance.

### Coordination And Handoff Rules

- One agent owns a file at a time. Shared-file changes are proposed as a patch or
  interface note and applied by the root integrator.
- Every handoff names changed files, tests run, untested paths, schema/API
  assumptions, recovery implications, and remaining risks.
- Sub-agents do not change `.env`, persistent `data/`, `config/`, `playbooks/`,
  generated `frontend/dist/`, live inventory, or controller deployment state.
- Sub-agents do not run destructive operations, reboot hosts, apply cluster
  settings, upgrade workloads, or clean managed test resources.
- Root rebases the integration plan after every phase gate and does not dispatch a
  later mutation provider against an unfrozen earlier contract.
- Failed or incomplete sub-agent work is not enabled behind a UI action. The root
  either completes and verifies it or leaves the capability disabled.

### Work That Must Remain Sequential

- Versioned schema migration wiring and verification against the live database.
- Shared state-machine and API-route integration.
- Compatibility-adapter conversion of existing mutating endpoints.
- Re-evaluation immediately before a protected side effect.
- Host reboot, controller-outage, rolling restart, rolling upgrade, and evacuation
  live rounds.
- Restoration of cluster settings and shutdown records before the next node.
- Database backup, image replacement, post-deployment verification, final cleanup,
  and release acceptance.

## Test Plan

### Unit And API Tests

- Manual maintenance-mode entry, conflict rejection, durable state, audit
  records, policy-bounded expiry, blocked mutation adapters, verified exit, and
  failed-exit `recovery_required` behavior.
- Default and customized maintenance-policy validation and revisions.
- Non-mutating planning and immutable plan hashes.
- Plan expiry, stale observations, changed assignment revisions, and changed policy.
- Aggregate impact for a host serving multiple clusters.
- Master quorum calculations for odd and even master-eligible counts.
- Last shard copy, relocating shard, disk watermark, and cluster identity blockers.
- Service budgets for singleton and redundant Kibana, Fleet, and Logstash.
- Conflict detection with apply, batch, upgrade, purge, and recovery runs.
- Hard predicates cannot be force-disabled.
- Existing reboot and upgrade endpoint contracts still return `run_id`.
- Secrets absent from API responses, plan records, logs, and audit details.

### Ansible And Stub Tests

- Structured node-ID discovery and CA-verified API calls.
- Data-node allocation guard using persistent and transient setting snapshots.
- Exact restoration of absent, `all`, `none`, and custom allocation values.
- Primary promotion with an in-sync replica and last-copy primary blocking.
- Non-data node restart proving that allocation settings remain unchanged.
- Recovery verification proving whether local shard reuse occurred or full recovery was required.
- Shutdown create, status polling, `stalled`, completion, and explicit deletion.
- One-shot executor stage, reboot checkpoint, post-boot resume, and self-disable.
- Controller disconnect at every side-effect boundary.
- Wrong cluster UUID, wrong node name, wrong version, and missing image behavior.
- Host timeout, SSH return, Podman socket return, and partial Quadlet recovery.
- Stateless rollback and Elasticsearch no-downgrade boundary.
- Cleanup removes only managed maintenance artifacts.

### Frontend Tests

- Enter/exit maintenance mode dialogs, active and recovery-required host states,
  expiry messaging, disabled controls, keyboard focus, and action-console runs.
- Plan preview and predicate grouping at desktop and mobile widths.
- Blocked plans cannot execute.
- In-page confirmation and typed singleton-outage override.
- Concurrent run tabs and SSE reconnect during maintenance.
- Pause, resume, cancel, and recovery actions appear only at valid phases.
- Existing Hosts, Roles, Clusters, Dashboard, and Advanced flows remain usable.
- No `alert`, `confirm`, or `prompt` browser calls.

### Live Regression Rounds

All live builds and execution run from the configured controller/build host. Test
targets are resolved from controller inventory; addresses are never embedded in
source or test code. Every participating destructive test node is cleaned after
every round before the next round starts.

1. Idle controller outage: stop ELKeeper and verify all workloads and endpoints
   continue without restart or configuration change.
2. Manual maintenance mode: enter on a healthy host, verify workloads remain
   unchanged and conflicting controller mutations are blocked, restart the
   controller, then verify a clean explicit exit restores normal operations.
3. Redundant stateless service maintenance: reboot one service host and verify the
   peer endpoint remains available.
4. Elasticsearch data-node reboot: verify shutdown preparation, one-node budget,
   local recovery, cluster membership, and shutdown-marker cleanup.
5. Master-eligible reboot: run only with a valid quorum topology and verify the
   elected master remains available.
6. Multi-cluster host reboot: verify all affected clusters are planned together and
   no cluster exceeds its budget.
7. Controller termination before reboot, during reboot, and after host return:
   verify the one-shot executor and recovery discovery.
8. Blocked singleton and last-shard-copy cases: verify no host or workload changes.
9. Rolling upgrade: verify ordering, version commits, health gates, and peer
   availability.
10. Failed stateless upgrade: verify prior artifact restoration.
11. Failed Elasticsearch upgrade after new process start: verify no downgrade and
    `recovery_required` state.
12. Repeat/recovery cleanup: verify no shutdown markers, locks, temporary settings,
   transient units, or managed maintenance files remain.

After every round verify on every participating test node:

- expected managed workloads are either restored or intentionally purged;
- no unintended listeners, containers, Quadlets, certificates, or data markers;
- no stale node-shutdown entries or voting exclusions;
- no temporary allocation settings left behind;
- no active transient maintenance units or manifests;
- unrelated containers, services, files, mounts, images, and packages unchanged;
- `firewalld` remains disabled and inactive; and
- no Podman TCP listener exists.

Run the complete five-minute profile after every fix, the fifteen-minute profile
before deployment, and the full test profile before release acceptance.

## Acceptance Criteria

- ELKeeper can be stopped while idle without restarting or interrupting managed
  workloads.
- A host reboot cannot begin when any hard safety predicate fails.
- Planned disruption never exceeds the effective maintenance policy.
- Elasticsearch nodes are changed one at a time and fully verified before progress.
- A controller restart cannot duplicate a completed side effect.
- An interrupted operation is observable and recoverable from persisted state.
- No successful Elasticsearch start is followed by an automatic downgrade.
- Existing API clients, frontend routes, assignments, storage, and run history
  continue working after additive migration.
- All temporary shutdown records, settings, locks, and executor artifacts are
  cleared after successful completion.
- Test-node cleanup preserves unrelated host resources after every live round.

## Risks And Design Decisions

- The Elasticsearch node shutdown API is intentionally used by Elastic-managed
  orchestrators but documented as unsupported for direct use. ELKeeper therefore
  keeps it optional and version-gated rather than making it the only backend.
- Zero-impact host maintenance is impossible for singleton services, single-master
  clusters, or indices without another usable shard copy. ELKeeper blocks these by
  default instead of disguising an outage as rolling maintenance.
- A one-shot host executor adds code on managed hosts, but it closes the most
  important controller-outage gap without introducing a permanent agent.
- Existing hybrid bootstrap-master workloads cannot provide a normal three-master
  rolling topology. Compatibility is preserved, while the UI explains why certain
  maintenance plans remain blocked.
- Automatic recovery is limited to actions that are provably idempotent. Ambiguous
  Elasticsearch state becomes `recovery_required` rather than triggering guesses.

## Official References

- [ECK nodes orchestration](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/nodes-orchestration)
- [ECK update strategy and change budget](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/update-strategy)
- [ECK pod disruption budget](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/pod-disruption-budget)
- [Elasticsearch rolling upgrade](https://www.elastic.co/docs/deploy-manage/upgrade/deployment-or-cluster/elasticsearch)
- [Cluster settings API](https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-cluster-put-settings)
- [Index shard recovery API](https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-indices-recovery)
- [Prepare a node for shutdown](https://www.elastic.co/docs/api/doc/elasticsearch/v8/operation/operation-shutdown-put-node)
- [Get node shutdown status](https://www.elastic.co/docs/api/doc/elasticsearch/v8/operation/operation-shutdown-get-node)
- [Clear a node shutdown request](https://www.elastic.co/docs/api/doc/elasticsearch/v8/operation/operation-shutdown-delete-node)
- [Voting configuration exclusions](https://www.elastic.co/docs/api/doc/elasticsearch/v8/operation/operation-cluster-post-voting-config-exclusions)
