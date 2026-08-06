# Elastic Stack Capability Gap Assessment

**Status:** Active production-readiness register
**Scope:** ELKeeper-managed Elasticsearch, Kibana, Fleet Server, Elastic Agent,
Logstash, and their host-level lifecycle. This is not a generic controller UI
or identity audit unless the issue directly changes Elastic Stack safety.

## Purpose And Baseline

This register records confirmed and partial Elastic Stack capabilities against
production operations. Each item states the smallest safe outcome required
before the capability can be represented as production-ready.

Elastic reference baseline:

- [Availability and resilience](https://www.elastic.co/docs/deploy-manage/production-guidance/availability-and-resilience)
- [Snapshot and restore](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore)
- [Elasticsearch upgrade guidance](https://www.elastic.co/docs/deploy-manage/upgrade/deployment-or-cluster/elasticsearch)
- [Secure cluster communications](https://www.elastic.co/docs/deploy-manage/security/secure-cluster-communications)

`P0` blocks production Elastic workloads. `P1` must be completed before the
affected lifecycle can be claimed as safely managed. `P2` is a planned
production capability gap.

## Existing Strengths

- Cluster-qualified workload names, data markers, certificates, paths, and
  ports reduce accidental cross-cluster collisions.
- Elasticsearch HTTP readiness checks use a CA and managed workloads use TLS.
- Version observation records image, digest, version, cache state, and probe
  results. Upgrade planning resolves immutable target digests and blocks
  downgrade or unsafe major-version progression.
- Major-upgrade preflight requires a recent successful Elasticsearch snapshot,
  while the maintenance model prohibits automatic Elasticsearch downgrade after
  a newer process opens its data path.
- Managed-only purge, resource rollback, run/SSE logs, provider capability
  gates, and read-only ECK endpoint handling provide a useful foundation.

These are foundations, not a production readiness declaration.

## P0: Production Safety Gaps

### `ES-DR-001` Snapshot, SLM, And Restore Are Not Managed

**Evidence:** `wishlist.md` lists repository setup, Snapshot Lifecycle
Management, inventory, restore, and recovery validation as unimplemented.
Upgrade preflight can require a recent snapshot, but ELKeeper cannot create,
verify, retain, browse, or restore one.

**Required outcome:** Add cluster-scoped snapshot repositories, encrypted
repository credentials, SLM, on-demand snapshot runs, inventory, restore
preview/runs, and tested disaster recovery. Major upgrades must consume an
immutable verified snapshot record, not only a transient API observation.

**Acceptance:** A clean cluster can snapshot, restore to an isolated validation
target, and verify data plus system-index recovery. Failed verification blocks a
major upgrade.

**Owner/roadmap:** `clusters`, `versions`, `maintenance`, `certificates`;
[wishlist item 3](wishlist.md#3-snapshot-slm-and-restore-management).

### `ES-SSH-001` Controller-Key Hosts Can Run Without A Per-Host Pin

**Evidence:** The host policy permits `StrictHostKeyChecking=no` when a
controller-key host has no `ssh_host_key` record. Legacy `known_hosts` entries
do not make that controller-key path strict.

**Required outcome:** Capture a verified host key during enrollment, migrate
verified legacy records to node pins, show fingerprints without key material,
and require strict checking for every controller-key operation. A key change
must block mutation until explicit audited approval.

**Acceptance:** Controller-key enrollment cannot complete without a verified
pin; a changed key fails closed; migration does not expose key material in logs.

**Owner/roadmap:** `hosts` and `controller_identity`; add a P0 roadmap item.

### `ES-HA-001` No Production Topology Admission Gate

**Evidence:** ELKeeper intentionally supports singleton and two-node lab
layouts. Maintenance checks exist, but apply does not require production master
redundancy, replica capacity, independent failure domains, or redundant
Kibana/Fleet/Logstash endpoints.

**Required outcome:** Add a production topology profile and pre-apply report
for master quorum, replica placement, role redundancy, zones, storage headroom,
and stable client endpoints. Retain an explicit visible `lab` profile.

**Acceptance:** Production apply is blocked when the declared availability
target cannot be met; topology names every unmet master, shard, or service
redundancy requirement.

**Owner/roadmap:** `clusters`, `workloads`, `maintenance`, `observability`;
[wishlist items 2, 6, and 7](wishlist.md#2-elasticsearch-aware-shutdown-and-scale-down).

### `ES-REC-001` Drift Detection And Interrupted-Run Recovery Are Incomplete

**Evidence:** Workloads are reconciled by requested apply operations. Desired
and observed generations, drift conditions, retry, pause coordination, and
controller-restart recovery remain roadmap work.

**Required outcome:** Persist desired generation and normalized configuration
hashes; continuously observe managed workloads; publish `Ready`,
`Progressing`, `Degraded`, `Blocked`, `Drifted`, and `Unknown`; recover
only from observation-backed checkpoints.

**Acceptance:** Stopping a managed workload produces bounded drift evidence and
the selected observe/notify/remediate action. A controller restart never marks
an unobserved batch healthy.

**Owner/roadmap:** `workloads`, `observability`, `platform`, `maintenance`;
[wishlist item 1](wishlist.md#1-continuous-desired-state-reconciliation).

## P1: Lifecycle And Service-Operation Gaps

### `ES-MAINT-001` Maintenance Safety Is Planned But Not Yet Operable

**Evidence:** Plans, locks, checkpoints, allocation guards, and post-return
checks exist, but mutation capabilities remain disabled pending redundant live
test acceptance.

**Required outcome:** Enable each mutation only after its own gate: shard
evacuation, allocation restoration, master voting safety, service disruption
budgets, host-return verification, and recovery-required handling. Do not add a
broad maintenance switch.

**Acceptance:** Reboot, resource change, rolling restart, detach, purge, and
upgrade use one checkpointed engine and leave no allocation setting, lock,
shutdown marker, or executor artifact behind.

**Owner/roadmap:** `maintenance`, `workloads`, `versions`; see
`maintenance_plan.md` and [wishlist item 2](wishlist.md#2-elasticsearch-aware-shutdown-and-scale-down).

### `ES-UPGRADE-001` Upgrade Safety Needs a Complete Executable Workflow

**Evidence:** Digest resolution, observations, snapshot checks, quorum checks,
and recovery-required behavior exist in validation. Fully accepted
maintenance-backed rolling execution with real snapshot evidence and
role-specific post-upgrade verification is not enabled.

**Required outcome:** Make upgrades maintenance plans with immutable digests,
freshness limits, snapshot and health gates, documented Elastic role ordering,
readiness evidence after every restart, and no automatic Elasticsearch downgrade.

**Acceptance:** Major upgrade cannot start without a verified restorable
snapshot and required master redundancy. Stateless rollback restores the prior
artifact only when compatible; Elasticsearch failure stops at
`recovery_required`.

**Owner/roadmap:** `versions`, `maintenance`; [wishlist items 2 and 3](wishlist.md#2-elasticsearch-aware-shutdown-and-scale-down).

### `ES-CERT-001` Certificate Rotation Is Metadata And Preview Only

**Evidence:** Certificate inventory, expiry planning, trust-domain analysis,
and rotation previews exist. Mutation remains disabled and legacy shared trust
domains are blocked.

**Required outcome:** Stage CA-verified replacement certificates for
Elasticsearch transport/HTTP, Kibana, Fleet, Logstash, and Agent consumers;
activate them with role-aware rolling operations; verify all consumers before
retiring prior material; support external CAs.

**Acceptance:** A renewal rotates one trust domain without breaking cluster
transport, browser access, Fleet enrollment, or existing agents. Invalid or
unverified certificates block activation.

**Owner/roadmap:** `certificates`, `maintenance`, `workloads`;
[wishlist item 10](wishlist.md#10-certificate-and-credential-rotation).

### `ES-PLACEMENT-001` Failure-Domain And Service HA Controls Are Partial

**Evidence:** Host zones and allocation zoning exist, but host capability
labels, placement requirements, anti-affinity, redundant services, and
load-balanced stable endpoints are still roadmap work.

**Required outcome:** Add failure-domain metadata and placement rules; validate
shard awareness/service redundancy before apply; integrate operator-provided
load balancers, VIPs, or reverse proxies into certificate SAN and health logic.

**Acceptance:** The system rejects a plan that places all master-eligible,
replica-bearing, Kibana, or Fleet workloads in one failure domain when eligible
alternatives exist.

**Owner/roadmap:** `clusters`, `workloads`, `certificates`;
[wishlist items 6 and 7](wishlist.md#6-failure-domains-and-placement-policy).

### `ES-TIERS-001` Node Profiles Do Not Express the Full Production Role Model

**Evidence:** The catalog covers master, hot, warm, ML, ingest, coordinating,
Kibana, Fleet, Logstash, and Agent. It does not expose first-class
`data_cold`, `data_frozen`, `transform`, `voting_only`, or general
validated node profiles.

**Required outcome:** Replace one-assignment-one-role with versioned node
profiles that validate supported role combinations, heap, storage class, ports,
and placement while preserving current presets.

**Acceptance:** Operators can model hot-warm-cold/frozen, transform,
remote-cluster, and voting-only topologies without manual Quadlet editing.

**Owner/roadmap:** `workloads`, `clusters`;
[wishlist item 5](wishlist.md#5-flexible-elasticsearch-node-profiles).

### `ES-FLEET-001` Fleet And Integration Lifecycle Is Not End-to-End

**Evidence:** ELKeeper deploys Fleet Server and Agent, but does not manage
Elastic Package Registry availability, package policy promotion, integration
asset lifecycle, or offline package/artifact workflows.

**Required outcome:** Add EPR/private-registry configuration, package
availability checks, policy/enrollment-token lifecycle, integration
compatibility reports, and an air-gapped artifact manifest.

**Acceptance:** A disconnected environment proves required images, agent
artifacts, and approved integration packages are available before a Fleet or
Agent change begins.

**Owner/roadmap:** `versions`, `workloads`, `clusters`;
[wishlist items 12 and 17](wishlist.md#12-supply-chain-and-air-gapped-support).

### `ES-OBS-001` Stack Signals Do Not Yet Produce Durable Alerts Or Support Evidence

**Evidence:** Dashboard telemetry, runtime observations, Filebeat companions,
and SSE run logs exist. Alert delivery, Prometheus metrics, support bundles,
disk-watermark forecasting, and end-to-end correlation remain roadmap work.

**Required outcome:** Add alert rules for health, disk watermarks, shard
recovery, certificate expiry, snapshot failure, Fleet/Agent status, stale
observations, and reconciliation failure. Add redacted support bundles and a
metrics endpoint.

**Acceptance:** A simulated disk, certificate, snapshot, or agent failure
creates a deduplicated alert and recovery notification linked to redacted run
and workload evidence.

**Owner/roadmap:** `observability`, `versions`, `certificates`;
[wishlist items 8, 13, and 19](wishlist.md#8-storage-lifecycle-management).

## P2: Configuration, Supply Chain, And Import Gaps

### `ES-POLICY-001` Elastic Configuration Policies Are Not Managed Assets

**Required outcome:** Manage ILM, index/component templates, ingest pipelines,
and approved Elasticsearch/Kibana settings as versioned cluster policies with
preview, conflict detection, audit, and rollback.

**Acceptance:** A policy changes declared assets through a run, detects manual
drift, and preserves documented exceptions until an operator resolves them.

**Owner/roadmap:** `clusters`, `workloads`;
[wishlist item 11](wishlist.md#11-reusable-configuration-policies).

### `ES-SUPPLY-001` Image Observation Is Stronger Than Supply-Chain Enforcement

**Evidence:** ELKeeper observes image digests and resolves immutable target
digests for upgrades. It does not retain approved digest/signature policy,
private registry/mirror configuration, or offline completeness reports.

**Required outcome:** Make approved repository/version/digest/signature data a
release artifact and reject unapproved image substitution, including changed
tags.

**Acceptance:** A deployment/upgrade runs from a verified offline manifest and
a digest mismatch blocks before Podman pulls or restarts a workload.

**Owner/roadmap:** `versions`;
[wishlist item 12](wishlist.md#12-supply-chain-and-air-gapped-support).

### `ES-IMPORT-001` Imported And ECK Cluster Migration Is Observation-Only

**Evidence:** Provider types and ECK endpoint capability gates exist, but
import discovery, compatibility validation, certificate ownership transfer, and
host-add migration have not reached an end-to-end managed workflow.

**Required outcome:** Implement import inventory, read-only observation first,
explicit provider capability negotiation, and an operator-approved migration
plan for adding ELKeeper-managed hosts. Never mutate ECK resources directly.

**Acceptance:** Imported Podman/ECK clusters are observed without mutation; a
migration requires approved planning, verified certificates, data movement
evidence, and a reversible ownership boundary.

**Owner/roadmap:** `clusters`, `workloads`, `certificates`, `maintenance`.

### `ES-HOST-001` Lab Host Hardening Must Not Become Production Default

**Evidence:** Host initialization can disable SELinux and `firewalld` under the
lab policy. This is compatible with the destructive test lab, not production
Elastic host hardening.

**Required outcome:** Preserve explicit lab mode; add production host preflight
that preserves enforcing SELinux and approved least-privilege network exposure.
Refuse a production profile that requests lab relaxations.

**Acceptance:** Preflight reports SELinux, firewall exposure, Podman socket
locality, kernel settings, storage prerequisites, and TLS reachability before
creating Elastic workloads.

**Owner/roadmap:** `hosts`, `clusters`, `workloads`; add a production
host-hardening work item.

## Recommended Delivery Order

1. `ES-SSH-001`, `ES-HA-001`, and `ES-HOST-001` admission/host safety gates.
2. `ES-DR-001` snapshot/SLM/restore and tested disaster recovery.
3. `ES-REC-001`, `ES-MAINT-001`, `ES-UPGRADE-001`, and `ES-CERT-001`.
4. `ES-PLACEMENT-001`, `ES-TIERS-001`, and stable service endpoints.
5. `ES-FLEET-001`, `ES-POLICY-001`, `ES-SUPPLY-001`, and `ES-OBS-001`.
6. `ES-IMPORT-001` after native lifecycle/recovery boundaries are proven.

## Evidence Rules

- Update an item only with source, test, or redacted live-run evidence.
- Never store credentials, private keys, tokens, raw certificates, database
  copies, or unredacted inventories in this register.
- Future implementation designs and regression evidence must reference the
  relevant finding ID.
