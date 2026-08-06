# ELKeeper Future Development Wishlist

This roadmap lists proposed ELKeeper improvements in descending priority. It
borrows useful operational principles from Elastic Cloud on Kubernetes (ECK)
without attempting to recreate Kubernetes. ELKeeper should remain focused on
managing Elastic Stack workloads directly on Linux hosts through SSH, Ansible,
Podman, and an operator-friendly web console.

## Capability Gap Assessment

The [Elastic Stack capability gap assessment](elastic_stack_gap_assessment.md)
records current implementation evidence, production risk, required outcomes,
and acceptance criteria for Elastic lifecycle gaps. This roadmap remains the
delivery-order source; the assessment is the evidence and prioritization record.

## Roadmap Principles

- Protect data, quorum, and recoverability before expanding product coverage.
- Treat every operation as an observable, resumable, and auditable run.
- Keep desired configuration separate from observed runtime state.
- Preserve unrelated host resources and the existing `ecp-*` ownership boundary.
- Prefer operator-approved recommendations until automatic behavior is proven safe.
- Keep secrets out of logs, browser storage, generated artifacts, and command lines.

## P0 - Safety And Control Foundations

### 1. Continuous Desired-State Reconciliation

- [ ] Add desired and observed generations to clusters, memberships, and workloads.
- [ ] Persist a normalized desired configuration hash for every managed workload.
- [ ] Detect missing containers, stopped services, changed Quadlets, stale
      certificates, resource drift, and unexpected image versions.
- [ ] Add configurable reconciliation modes: observe only, notify, and remediate.
- [ ] Retry transient failures with bounded exponential backoff.
- [ ] Pause reconciliation during upgrades, batch applies, purge, and maintenance.
- [ ] Recover interrupted reconciliation after controller restart.

Required conditions:

- `Ready`: desired state is present and verified.
- `Progressing`: a tracked operation is changing the resource.
- `Degraded`: the workload is running but an associated capability has failed.
- `Blocked`: a safety or dependency rule prevents reconciliation.
- `Drifted`: observed configuration differs from the desired configuration.
- `Unknown`: observations are unavailable or stale.

Success criteria: deleting or stopping a managed container is detected, reported,
and safely reconciled according to the configured policy without affecting
unrelated host resources.

### 2. Elasticsearch-Aware Shutdown And Scale-Down

- [ ] Use the Elasticsearch node shutdown API before planned node removal.
- [ ] Check shard allocation, last-copy shards, disk watermarks, and cluster health.
- [ ] Apply and remove voting configuration exclusions when master membership changes.
- [ ] Wait for shard evacuation before stopping or purging data workloads.
- [ ] Block unsafe removal unless an explicitly audited force operation is approved.
- [ ] Reuse the same safety engine for detach, purge, host maintenance, restart,
      resource changes, and upgrades.
- [ ] Add maintenance windows and a cluster-level operation pause.

Success criteria: ELKeeper cannot accidentally remove the last usable shard copy or
break master quorum through an ordinary UI or API operation.

### 3. Snapshot, SLM, And Restore Management

- [ ] Configure and verify S3, S3-compatible, GCS, Azure, and shared-filesystem
      snapshot repositories.
- [ ] Store repository credentials through encrypted cluster secrets.
- [ ] Create and manage Snapshot Lifecycle Management policies.
- [ ] Provide on-demand snapshots with tracked runs and progress.
- [ ] Inventory snapshots, contents, age, state, and repository health.
- [ ] Restore selected indices or complete clusters with rename and conflict options.
- [ ] Require a verified recent successful snapshot for guarded major upgrades.
- [ ] Add restore validation and a documented disaster-recovery workflow.

Success criteria: an operator can create, verify, browse, and restore a snapshot
without using Elasticsearch Dev Tools or handling repository credentials manually.

### 4. Controller State Protection

- [ ] Create scheduled, retention-managed SQLite backups outside the active data path.
- [ ] Verify backup integrity and expose the last successful backup on the dashboard.
- [ ] Provide an authenticated controller restore workflow with preflight validation.
- [ ] Export a redacted disaster-recovery manifest containing non-secret topology.
- [ ] Detect database corruption, filesystem exhaustion, and failed backup schedules.
- [ ] Document an active-passive controller recovery procedure.

Success criteria: a failed controller host can be rebuilt without reconstructing
cluster inventory and desired state manually.

## P1 - Topology And Availability

### 5. Flexible Elasticsearch Node Profiles

- [ ] Replace the one-assignment-one-role assumption with named node profiles.
- [ ] Allow validated combinations of Elasticsearch node roles.
- [ ] Add `data_content`, `data_cold`, `data_frozen`, `transform`,
      `remote_cluster_client`, and `voting_only` support.
- [ ] Preserve simple presets such as Dedicated master, Hot, Warm, Cold, and Ingest.
- [ ] Migrate existing assignments without changing their effective behavior.
- [ ] Calculate ports per workload instance rather than per role label.

Success criteria: ELKeeper can express normal production Elastic topologies without
requiring unnecessary containers or manual configuration edits.

### 6. Failure Domains And Placement Policy

- [ ] Add host labels for site, zone, rack, storage class, and workload capability.
- [ ] Support required placement, preferred placement, and anti-affinity rules.
- [ ] Configure Elasticsearch allocation awareness from selected failure domains.
- [ ] Warn when master, shard, Kibana, or Fleet placement lacks redundancy.
- [ ] Show placement violations and capacity gaps in topology views.
- [ ] Prevent unsafe plans before Ansible starts.

Success criteria: a host or rack failure does not remove all copies of a critical
service or shard when sufficient infrastructure is available.

### 7. Service High Availability And Stable Endpoints

- [ ] Support multiple Kibana, Fleet Server, Logstash, and coordinating instances.
- [ ] Add health-aware endpoint selection and explicit preferred endpoints.
- [ ] Integrate with operator-provided load balancers, virtual IPs, or reverse proxies.
- [ ] Include stable endpoint names in certificate SAN management.
- [ ] Report endpoint readiness independently from individual workload health.
- [ ] Preserve endpoint availability during rolling changes.

Success criteria: client access does not depend on one workload instance or require
manual endpoint changes after maintenance.

### 8. Storage Lifecycle Management

- [ ] Track desired, allocated, used, and available storage per workload.
- [ ] Alert on projected exhaustion and Elasticsearch disk watermark risk.
- [ ] Support safe filesystem or logical-volume expansion where the host permits it.
- [ ] Add storage-class labels and placement requirements.
- [ ] Provide a data migration workflow before changing storage paths.
- [ ] Prevent shrinking or replacing active data storage without a recovery plan.

Success criteria: storage changes are validated, observable, and do not silently
orphan Elasticsearch data.

## P2 - Security And Governance

### 9. Multi-User RBAC

- [ ] Add Administrator, Operator, Viewer, and Security Auditor roles.
- [ ] Support cluster-scoped and host-scoped permissions.
- [ ] Separate secret reveal permission from ordinary workload management.
- [ ] Add expiring API tokens with explicit scopes.
- [ ] Record login, authorization denial, token, and privilege-change audit events.
- [ ] Add OIDC first, followed by LDAP only when required.

Success criteria: routine operators can manage workloads without receiving controller
administration or secret-access privileges.

### 10. Certificate And Credential Rotation

- [ ] Track certificate expiry and rotation eligibility.
- [ ] Automatically stage and verify replacement certificates before activation.
- [ ] Support an external CA and operator-provided certificate chains.
- [ ] Rotate Elastic built-in user, monitoring, Filebeat, and service credentials.
- [ ] Add Elasticsearch, Kibana, Beats, and Logstash keystore setting management.
- [ ] Verify every consumer before retiring an old credential.

Success criteria: credentials and certificates can be rotated without exposing values
or causing an uncontrolled cluster outage.

### 11. Reusable Configuration Policies

- [ ] Create versioned policies for Elasticsearch and Kibana settings.
- [ ] Manage ILM policies, index templates, ingest pipelines, and component templates.
- [ ] Attach policies to one or more clusters.
- [ ] Preview policy impact and detect overrides or conflicts.
- [ ] Reconcile policy drift while preserving documented cluster exceptions.
- [ ] Provide policy history, rollback, and audit records.

Success criteria: common configuration can be maintained consistently across clusters
without editing every cluster independently.

### 12. Supply-Chain And Air-Gapped Support

- [ ] Support private registry and registry-mirror configuration.
- [ ] Create an offline artifact manifest and download bundle workflow.
- [ ] Pin images by digest after version selection.
- [ ] Verify expected repository, version, digest, and optional signatures.
- [ ] Report cached artifact completeness before offline deployment or upgrade.

Success criteria: an operator can prove exactly which images will run and deploy a
cluster without direct internet registry access.

## P3 - Operability And Automation

### 13. Controller Metrics And Diagnostics

- [ ] Add an authenticated Prometheus metrics endpoint.
- [ ] Measure reconciliation duration, failures, retries, queue depth, and stale state.
- [ ] Track SSH, Ansible, Podman, Elasticsearch, and database dependency health.
- [ ] Generate a downloadable redacted support bundle.
- [ ] Include configuration metadata, recent runs, conditions, versions, and logs.
- [ ] Add correlation IDs across API requests, runs, Ansible tasks, and observations.

Success criteria: common failures can be diagnosed without direct database access or
collecting credentials and unrelated host information.

### 14. Capacity Recommendations

- [ ] Calculate CPU, heap, disk, shard, and ingest pressure per node profile.
- [ ] Forecast disk exhaustion using bounded historical observations.
- [ ] Recommend resource changes, replicas, or additional hosts.
- [ ] Stage accepted recommendations through the existing batch workflow.
- [ ] Explain the evidence and safety constraints behind each recommendation.

Success criteria: recommendations are actionable and auditable without automatically
changing stateful workloads.

### 15. Guarded Autoscaling

- [ ] Implement only after continuous reconciliation and safe shutdown are proven.
- [ ] Scale stateless services first.
- [ ] Require an eligible spare-host pool and placement policy.
- [ ] Add minimum, maximum, cooldown, maintenance, and approval controls.
- [ ] Use Elasticsearch autoscaling capacity data where available.
- [ ] Roll back failed scale operations and retain a complete decision record.

Success criteria: automatic changes cannot violate quorum, shard safety, placement,
capacity, or maintenance constraints.

### 16. Remote Clusters And CCR

- [ ] Model trusted relationships between ELKeeper-managed clusters.
- [ ] Configure API-key or certificate-based remote-cluster authentication.
- [ ] Validate `remote_cluster_client` placement and connectivity.
- [ ] Manage cross-cluster search and replication relationships.
- [ ] Display remote connectivity, lag, and certificate status.
- [ ] Coordinate credential and certificate rotation across both clusters.

Success criteria: cross-cluster features can be configured and monitored without
manually distributing credentials or editing Elasticsearch configuration.

## P4 - Product Coverage

### 17. Additional Elastic Services

- [ ] Add an APM Server workload only where Fleet-managed APM is unsuitable.
- [ ] Add Elastic Package Registry for isolated Fleet environments.
- [ ] Evaluate Elastic Maps Server for licensed offline mapping deployments.
- [ ] Add service-specific readiness, upgrade, certificate, and purge behavior.
- [ ] Do not add a service until its complete lifecycle can be managed safely.

### 18. Templates And Environment Promotion

- [ ] Export a secret-free cluster blueprint.
- [ ] Import and validate blueprints against available hosts and storage.
- [ ] Version blueprints and show differences before application.
- [ ] Support development, staging, and production policy overlays.
- [ ] Keep generated secrets unique to each destination cluster.

### 19. Notification Integrations

- [ ] Add configurable email, webhook, Slack-compatible, and incident-tool targets.
- [ ] Notify on blocked operations, degraded clusters, expiring certificates,
      failed backups, disk pressure, and recovery completion.
- [ ] Deduplicate repeated alerts and send recovery notifications.
- [ ] Redact secrets, tokens, and sensitive paths from notification payloads.

## Explicit Non-Goals

- Reimplementing Kubernetes scheduling, CRDs, namespaces, or arbitrary pod templates.
- Automatically moving stateful workloads before safe shutdown and restore are proven.
- Exposing Podman over TCP or connecting browsers directly to managed hosts.
- Managing unrelated host containers, services, storage, or network resources.
- Supporting deprecated Elastic products without a current operational requirement.
- Adding broad plugin or arbitrary-command execution that bypasses ELKeeper validation.

## Recommended Delivery Order

1. Continuous reconciliation and resource conditions.
2. Safe shutdown, shard evacuation, restart, and downscale.
3. Snapshot, SLM, restore, and controller backup.
4. Node profiles, failure domains, service HA, and storage lifecycle.
5. RBAC, certificate rotation, policies, and supply-chain controls.
6. Metrics, diagnostics, capacity recommendations, and remote clusters.
7. Guarded autoscaling and additional Elastic services.

Each roadmap item should receive its own design document, migration plan, failure
model, API contract, UI behavior, rollback strategy, and regression test plan before
implementation begins.
