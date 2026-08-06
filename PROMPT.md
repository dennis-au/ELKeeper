# ELKeeper Planned Host And Container Maintenance

Status: Draft for operator review
Date: 2026-08-05

## Goal

Build Maintenance Mode around the operator's real intent: temporarily stop
either an entire managed host or one managed workload container, perform planned
maintenance, and return the target to service without causing avoidable Elastic
Stack disruption.

There are only two supported reasons to enter Maintenance Mode:

1. **Host work** - the managed host must be rebooted or shut down for planned
   out-of-band maintenance.
2. **Workload work** - one managed container must be stopped or restarted while
   the host and its peer workloads remain in service.

Anything that is not expected to return promptly is not maintenance. It belongs
to evacuation, decommission, detach, purge, or a separately approved recovery
workflow.

The feature must support two explicit maintenance scopes:

1. **Host maintenance** - prepare and stop every ELKeeper-managed workload on a
   selected host before the host is rebooted or shut down.
2. **Container maintenance** - prepare and stop exactly one selected managed
   workload while leaving peer workloads on the same host untouched.

Maintenance is a planned interruption workflow, not only a controller lock. The
operator flow must be:

```text
select target -> preview impact -> prepare -> ready to stop -> stop
-> maintain -> return -> verify -> available
```

Any state that cannot be verified safely must become `blocked` or
`recovery_required`; it must never be silently treated as successful.

## User Model

- A maintenance target is temporary and is expected to return to service.
- Permanent removal belongs to evacuation, detach, purge, or decommission
  workflows.
- Planning is read-only. It must not create a mutation run, acquire a lock,
  change Elasticsearch settings, or stop a target.
- Preparing maintenance creates the durable plan, run, audit record, target
  lock, and any required Elasticsearch safety guard.
- Stopping a target is a separate, explicit operator confirmation after the
  system reports `ready_to_stop`.
- ELKeeper continues observing the target and its affected clusters during the
  maintenance window, but it must not automatically restart an intentionally
  stopped target.
- Exiting and recovering an active maintenance window must remain available
  even when new maintenance entry has been disabled.

## Destructive Lab Boundary

The operator grants standing authorization for future destructive maintenance
validation only on these designated development testing nodes:

- `192.168.0.101`
- `192.168.0.102`
- `192.168.0.103`

`192.168.0.104` is the controller and build host. It must not host managed
Elastic workload disruption tests. This standing authorization removes the need
for separate approval of each future destructive test round within the three
listed nodes, but every round still requires a verified database backup, a
captured host and cluster baseline, managed-only cleanup, and a recorded
redacted evidence ledger. This authorization never extends to unrelated hosts,
containers, services, files, network listeners, or data paths.

## Required Behavior

### Scope And Impact

- Resolve affected workloads, clusters, roles, endpoints, and shared hosts from
  controller-owned inventory and current observations.
- Host maintenance must aggregate impact across every cluster with a managed
  workload on the host.
- Container maintenance must mutate only the selected assignment, its managed
  companion resources, and explicitly owned maintenance artifacts.
- Preserve unrelated containers, files, services, listeners, images, mounts,
  and host resources.

### Preflight

- Require fresh host, Podman, workload, endpoint, and Elasticsearch
  observations appropriate to the selected target.
- Reject conflicting maintenance, deployment, upgrade, restart, detach, purge,
  or recovery operations.
- Evaluate master quorum, shard-copy safety, active relocation and recovery,
  disk watermarks, role-specific minimum availability, and cluster identity.
- Present passed checks, warnings, blockers, affected services, and concrete
  remediation before any mutation is offered.

### Elasticsearch Allocation Guard

- Apply an allocation guard only when the maintenance target includes a
  data-bearing Elasticsearch node expected to return after a short planned
  interruption.
- Before stopping that node, capture the exact persistent and transient
  allocation settings and set `cluster.routing.allocation.enable` to
  `primaries` through CA-verified HTTPS.
- This follows Elastic's rolling-restart guidance: a short, planned absence
  should not cause the cluster to begin wasteful replica allocation after the
  node-left delay. The guard permits primary allocation while the expected node
  returns, but does not relax quorum, shard-copy, or recovery checks.
- Treat the guard as cluster-wide owned state. Concurrent maintenance plans
  must not overwrite or prematurely restore another plan's guard.
- Restore the exact prior settings after the node rejoins and passes identity,
  version, readiness, and local recovery checks.
- If the node does not return, enter recovery and offer an explicit path to
  restore normal allocation or move into evacuation. Never leave `primaries`
  active without a visible owner and recovery path.
- Master-only, coordinating-only, ingest-only, Kibana, Fleet Server, Logstash,
  Agent, and Beat maintenance must use role-specific safety checks and must not
  change shard allocation unless a data-bearing node is also affected.

### Stop And Return

- Container maintenance stops only the selected controller-managed unit and
  prevents ordinary reconciliation from restarting it during the window.
- Host maintenance prepares all affected clusters, stops managed workloads in
  a dependency-aware order, and performs a reboot or shutdown only through an
  explicitly approved mutation capability.
- Start Elasticsearch before dependent Elastic services when returning a host
  to service, then verify each workload through its public observation and
  readiness contracts.
- Verify Elasticsearch cluster UUID, node identity, roles, image version,
  shard recovery, cluster health, and service budgets before closing a data-node
  maintenance plan.
- Verify role-specific endpoints and health before closing stateless workload
  maintenance.

### State, UX, And Recovery

- Use an explicit state model: `available`, `preparing`, `ready_to_stop`,
  `stopping`, `maintenance`, `returning`, `verifying`, `blocked`, and
  `recovery_required`.
- Show planned maintenance as an expected operational state, not as an
  unexplained offline failure.
- Display the target, affected clusters and roles, allocation guard owner,
  elapsed time, planned return time, current checkpoint, and available actions.
- Expiry must transition to a visible recovery state. It must not silently
  unlock the target, claim success, or discard the allocation guard.
- Separate capabilities for read-only planning, maintenance entry, container
  stop, host reboot or shutdown, and recovery. Cleanup and recovery for an
  already-active operation cannot be disabled by turning off new entry.
- Every mutation creates or attaches to a platform run, returns `run_id`,
  streams redacted timestamped output through SSE, and remains recoverable after
  controller restart.

## Non-Goals

- Performing operating-system patching, firmware upgrades, hardware repair, or
  arbitrary commands inside the maintenance window.
- Permanently removing a host or Elasticsearch node; use evacuation,
  decommission, detach, or purge instead.
- Applying `primaries` for an unbounded or permanent node absence. That target
  must enter evacuation, decommission, detach, purge, or explicit recovery.
- Building a general-purpose scheduler or replacing systemd, Podman, Ansible,
  or Elasticsearch allocation logic.
- Automatically overriding quorum, last-shard-copy, stale-observation,
  conflicting-operation, or provider-ownership blockers.
- Stopping unrelated host resources or workloads outside the `ecp-*` ownership
  boundary.
- Allowing browsers to connect directly to managed hosts, Podman, or
  Elasticsearch.
- Using insecure TLS, embedding credentials in plans or logs, or storing
  decrypted secrets in browser state.
- Enabling broad rolling restart, upgrade, or evacuation execution merely
  because the maintenance preparation workflow exists.

## Done When

- An operator can preview host maintenance without creating a run, lock, remote
  mutation, or Elasticsearch setting change.
- An operator can preview container maintenance and see that peer containers on
  the same host will remain untouched.
- Preparing a data-node interruption captures the previous allocation state,
  applies `primaries`, records its owner, and reaches `ready_to_stop` before the
  stop action is enabled.
- A container maintenance Happy Flow stops only the selected managed workload,
  reports planned maintenance, returns it to service, restores any guard, and
  closes its run and audit trail successfully.
- A host maintenance Happy Flow safely prepares every affected cluster, stops
  only managed workloads, performs the approved host action, returns workloads
  in dependency order, verifies them, and closes without stale artifacts.
- Master quorum, last-shard-copy, singleton service, stale observation,
  conflicting operation, wrong cluster identity, and unsupported provider cases
  block before any target or cluster setting is changed.
- Controller restart at every persisted checkpoint results in a correct
  complete, incomplete, ambiguous, or `recovery_required` classification.
- Disabling new maintenance entry never prevents exit, cleanup, or recovery of
  an already-active plan.
- Expired locks and missing targets have explicit, tested recovery paths; no
  active allocation guard, lock, run, or host state is silently abandoned.
- The Dashboard, Maintenance page, host and workload views, and action console
  show consistent target scope, state, progress, degradation, and recovery
  information on desktop and mobile layouts.
- Focused backend, frontend, migration, orchestration, redaction, source-safety,
  and restart-recovery tests pass.
- The five-minute and fifteen-minute profiles pass on the configured build host.
- Lab Happy Flow and Chaos Mode rounds prove role isolation, cluster safety,
  exact allocation restoration, unrelated-resource preservation, managed-only
  cleanup, and no residual locks or maintenance artifacts.
- Mutation controls remain disabled in the release artifact until their named
  acceptance gates and redacted evidence ledger are complete.
