# ELKeeper Node Maintenance And Upgrade Plan

## Document Status

- Status: Proposed
- Date: 2026-08-02
- Audience: ELKeeper maintainers, reviewers, and regression-test operators
- Scope: Future development only. This document does not change current runtime behavior.

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

- Make planned host reboot and rolling upgrade safe, observable, and resumable.
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

The implementation already provides foundations that must be extended rather
than replaced:

- SQLite cluster, membership, assignment, observation, run, and audit records.
- Assignment revisions and optimistic concurrency checks.
- Rollback-capable workload change batches with controller-restart recovery.
- Persistent rootful Podman Quadlets with local configuration, certificates,
  images, and data paths.
- Cluster-scoped operation exclusion through active run checks.
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
| `NoShardMovement` | Elasticsearch | No initializing or relocating shards before a planned disruption |
| `NoLastShardCopy` | data roles | Target node does not hold the only usable copy of a shard |
| `MasterQuorum` | master-eligible | Remaining available master-eligible nodes preserve quorum |
| `RoleAvailabilityBudget` | all | Aggregate host impact stays within role and cluster budgets |
| `DiskWatermarksSafe` | data roles | Remaining nodes have capacity to recover or accept required shards |
| `TargetArtifactReady` | upgrades | Required image is locally cached and matches the selected digest |
| `VersionTransitionSupported` | upgrades | No downgrade or unsupported major-version jump is requested |
| `SnapshotRecoveryReady` | major upgrades | Required recent snapshot and repository checks pass |
| `NoStaleShutdownRecord` | Elasticsearch | No unrelated or abandoned node-shutdown record exists |

`NoLastShardCopy`, `MasterQuorum`, `ExpectedClusterIdentity`, and
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
exact previous value, not an assumed default.

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

### In-Place Host Reboot

An in-place reboot affects every managed workload on the host across every
cluster. The plan must calculate aggregate impact before any cluster is changed.

Execution order:

1. Acquire a host lock and locks for every affected cluster and assignment.
2. Refresh host, container, image, endpoint, and Elasticsearch observations.
3. Evaluate all safety predicates for all affected clusters as one atomic plan.
4. Record exact pre-operation workload states and configuration/image digests.
5. Prepare each Elasticsearch node for restart using the selected backend.
6. Stage the one-shot host maintenance executor described below.
7. Reboot through systemd and allow the controller SSH connection to close.
8. Wait for the host, Podman socket, Quadlet generator, and systemd to return.
9. Verify every workload that was running before maintenance is running again.
10. Verify all cluster UUIDs, node membership, versions, service endpoints, and
    disruption budgets.
11. Clear every temporary shutdown marker or cluster setting.
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
- entered and updated timestamps.

Existing cluster, membership, assignment, run, observation, and audit schemas
remain authoritative. Maintenance tables reference them rather than duplicating
secrets or desired workload configuration.

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
| Controller restarts during a plan | Rediscover state; never infer completion from logs |
| Stateless service readiness fails | Restore previous service artifact when compatible |
| Elasticsearch readiness fails after new start | No downgrade; require retry, replacement, or snapshot recovery |
| Run log streaming disconnects | Operation continues; UI reconnects from persisted run state |

## Implementation Phases

### Phase 1: Planning And Safety Core

- Add additive maintenance tables and policy defaults.
- Implement plan generation, plan hashing, predicates, locks, and API responses.
- Add plan UI and non-mutating impact preview.
- Route the existing reboot action through plan validation but keep execution
  disabled until Phase 2 is verified.

### Phase 2: Safe In-Place Reboot

- Implement the documented rolling backend.
- Add the one-shot host executor and post-boot result import.
- Implement service budgets, host reboot state machine, recovery, and cleanup.
- Preserve the existing host reboot playbook as a low-level executor step.

### Phase 3: Shared Rolling Restart Engine

- Route Elasticsearch workload restart and resource-change restart through the
  maintenance engine.
- Add service-specific budgets and readiness.
- Extend workload batch recovery to understand maintenance checkpoints.

### Phase 4: Upgrade Integration

- Replace the current upgrade loop with maintenance plans.
- Implement official node ordering, immutable image digests, per-node checkpoints,
  and role-aware rollback boundaries.
- Add the node shutdown API backend behind an explicit capability flag.

### Phase 5: Evacuation And Replacement

- Add host draining, eligible replacement selection, `max_surge`, data migration,
  and permanent removal.
- Deliver only after flexible node profiles, failure domains, capacity inventory,
  and service HA are implemented.

## Test Plan

### Unit And API Tests

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
- Shutdown create, status polling, `stalled`, completion, and explicit deletion.
- Exact restoration of temporary allocation settings.
- One-shot executor stage, reboot checkpoint, post-boot resume, and self-disable.
- Controller disconnect at every side-effect boundary.
- Wrong cluster UUID, wrong node name, wrong version, and missing image behavior.
- Host timeout, SSH return, Podman socket return, and partial Quadlet recovery.
- Stateless rollback and Elasticsearch no-downgrade boundary.
- Cleanup removes only managed maintenance artifacts.

### Frontend Tests

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
2. Redundant stateless service maintenance: reboot one service host and verify the
   peer endpoint remains available.
3. Elasticsearch data-node reboot: verify shutdown preparation, one-node budget,
   local recovery, cluster membership, and shutdown-marker cleanup.
4. Master-eligible reboot: run only with a valid quorum topology and verify the
   elected master remains available.
5. Multi-cluster host reboot: verify all affected clusters are planned together and
   no cluster exceeds its budget.
6. Controller termination before reboot, during reboot, and after host return:
   verify the one-shot executor and recovery discovery.
7. Blocked singleton and last-shard-copy cases: verify no host or workload changes.
8. Rolling upgrade: verify ordering, version commits, health gates, and peer
   availability.
9. Failed stateless upgrade: verify prior artifact restoration.
10. Failed Elasticsearch upgrade after new process start: verify no downgrade and
    `recovery_required` state.
11. Repeat/recovery cleanup: verify no shutdown markers, locks, temporary settings,
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
- [Prepare a node for shutdown](https://www.elastic.co/docs/api/doc/elasticsearch/v8/operation/operation-shutdown-put-node)
- [Get node shutdown status](https://www.elastic.co/docs/api/doc/elasticsearch/v8/operation/operation-shutdown-get-node)
- [Clear a node shutdown request](https://www.elastic.co/docs/api/doc/elasticsearch/v8/operation/operation-shutdown-delete-node)
- [Voting configuration exclusions](https://www.elastic.co/docs/api/doc/elasticsearch/v8/operation/operation-cluster-post-voting-config-exclusions)
