# ELKeeper Workload Regression Test Plan

## Purpose

This runbook verifies cluster-qualified workload assignment, reconciliation,
resource updates, detach, and purge without leaving Elastic resources on test
hosts. It is the operational companion to the automated Python, frontend, and
Ansible tests.

## Scope And Safety

- Run controller and build checks on `192.168.0.104`. Never assign an Elastic
  workload to the controller.
- The only destructive workload targets are inventory-approved test nodes
  `192.168.0.101`, `192.168.0.102`, and `192.168.0.103`.
- Resolve node IDs, interfaces, addresses, and cluster IDs from the ELKeeper
  UI or API. Do not place laboratory IPs, credentials, keys, tokens, or
  fingerprints in source, fixtures, screenshots, or the result ledger.
- Each live round begins from a verified clean state and ends by purging every
  workload configured by that round. Test nodes are destroyed or restored to
  their approved baseline after every round by the lab owner.
- Preserve unrelated packages, containers, images, services, mounts, data, and
  listeners. Keep `firewalld` disabled and inactive; do not expose Podman TCP.

## Required Evidence

Record a redacted ledger entry for every round:

| Item | Record |
| --- | --- |
| Artifact | source SHA-256 or immutable controller image ID |
| Targets | ELKeeper node IDs and cluster ID, never credentials |
| Run evidence | apply, resource, detach, and purge run IDs plus final states |
| Verification | endpoint/status result, topology result, and cleanup result |
| Outcome | pass, failure signature, correction, regression result |

Do not record passwords, enrollment tokens, service tokens, private keys,
encrypted configuration, decoded secrets, or raw Ansible variable files.

## Shared Preflight

1. Run the required source profile from the controller source tree:

   ```bash
   python -m unittest discover -s tests -q
   python tools/check_refactor_boundaries.py --root . --strict
   cd frontend && npm test && npm run typecheck
   ```

2. Syntax-check every changed Ansible playbook. Run `npm run build` when the
   frontend or a shared contract changed.
3. In ELKeeper, confirm every target host is enabled, reachable, and has no
   active run, maintenance lock, recovery-required state, or unmanaged listener
   conflict.
4. Create or select a test-only cluster. Add only selected test nodes as
   members. Configure either verified dedicated Data/User NIC bindings or an
   explicit shared-NIC membership. The recorded address must exist on the named
   interface.
5. On every participating host capture the baseline: `ecp-*` containers,
   managed Quadlets, Elastic listeners (`9200`, `9300`, `5601`, `8220`, `9600`),
   `/etc/elastic-control/clusters`, controller-marked data directories, active
   systemd failures, firewalld state, and the unrelated extra-bind test path.
6. Stop the round if a previous managed workload, stale lock, active run, or
   unexpected non-ELKeeper Elastic resource is present. Resolve it through the
   owning controller flow; never delete arbitrary host paths to make a test
   pass.

## Standard Workload Flow

1. In **Roles**, select the test cluster and choose a cluster member test node.
2. Stage the workload with an explicit absolute storage path reserved for that
   round. Use one path per cluster, role, and node. Configure CPU, container
   memory, and optional runtime heap according to the role policy.
3. For Logstash, supply a minimal valid structured pipeline. For Kibana, Fleet
   Server, and Elastic Agent, stage their prerequisite roles in the same batch
   or complete the prerequisite run first.
4. Apply the staged changes. Follow the returned `run_id` in the action console
   until it reaches `succeeded`, `failed`, or `recovery_required`.
5. Verify the workload row becomes managed only after success. Confirm the
   terminal topology includes the correct host, role box, NICs, resource limits,
   storage path, and user access URLs where the role exposes one.
6. Verify only the intended workload was started or restarted. A failure must
   retain redaction, roll back reversible changes, and leave no active batch
   record or stale assignment operation ID.

## Role Coverage Rounds

Run each item from a fresh clean baseline. Use the smallest topology that meets
the role prerequisites.

| Round | Assign to test node | Required checks |
| --- | --- | --- |
| 1 | Bootstrap master and hot data | verified HTTPS, matching cluster UUID, membership, transport on Data NIC, HTTP on User NIC |
| 2 | Additional master | joins the bootstrap cluster; membership check tolerates normal discovery delay; no second cluster UUID |
| 3 | Warm, ML, ingest, coordinating | role-specific node roles, cluster membership, resource limits, no unexpected listener binding |
| 4 | Kibana | CA-verified User-NIC URL, `kibana_system` credential path, Node.js heap policy, no Data-NIC HTTP listener |
| 5 | Fleet Server and Elastic Agent | Fleet readiness, enrollment relationship, Agent has no user listener, secrets remain redacted |
| 6 | Logstash | configured API URL, pipeline rendering, JVM heap policy, persistent storage ownership |
| 7 | Mixed roles across two or more test nodes | topology ordering, access URLs, port collision prevention, shared/dedicated NIC validation |

For every round exercise at least one invalid input before the happy flow:
invalid storage path, insufficient Elasticsearch memory, invalid heap budget,
missing prerequisite, stale assignment revision, unavailable NIC binding, or
port conflict. Verify ELKeeper rejects it before making a remote change.

## Resource And Operation Coverage

1. For each supported role, change CPU, memory, storage path where safe, and
   runtime heap through the Resources dialog. Apply the staged change and verify
   the target Quadlet persists the limit and only that workload restarts.
2. Force the stub-suite readiness failure and confirm the prior controller
   configuration is restored. Elasticsearch must become recovery-required rather
   than being automatically downgraded after a new process can access its data.
3. Select **Detach** and confirm the controller assignment is removed while the
   remote workload remains running. Re-add it only after documenting that
   behavior, then continue with a fresh cleanup round.
4. Select **Purge** for each remaining configured test workload. Type the
   required confirmation in the in-page dialog, wait for its `run_id` to
   succeed, then refresh the cluster. A successful purge removes both the
   remote managed workload and its controller assignment.

## Mandatory Cleanup After Every Round

1. Purge all configured workloads in reverse dependency order: Elastic Agent,
   Fleet Server, Kibana, Logstash, data/ML/ingest/coordinating roles, additional
   masters, then the bootstrap master. Do not use **Detach** as cleanup because
   it intentionally leaves the remote workload running.
2. Wait for every purge run to finish. If a purge fails, use the failed run
   output and the marker-protected recovery flow; do not manually remove an
   arbitrary configured storage path.
3. Verify from ELKeeper and each participating host:
   - no assignment remains for the test cluster;
   - no `ecp-*` containers, pods, systemd units, or active/failed scoped units;
   - no managed Quadlets, scoped configuration/certificate files, topology
     artifacts, controller executor files, or controller-marked data paths;
   - no listeners on `9200`, `9300`, `5601`, `8220`, or `9600` from the round;
   - the unrelated extra bind path is unchanged;
   - firewalld is disabled/inactive and Podman has no TCP listener.
4. Remove empty test-only cluster membership or the empty test cluster through
   the controller UI when that is part of the round. Preserve hosts and their
   normal enrollment records unless the round explicitly covers host removal.
5. Destroy or restore every participating testing node (`.101` through `.103`)
   to the approved baseline. Re-probe it before it is eligible for the next
   round.

## Failure And Regression Rules

- On an apply failure, preserve the redacted run evidence, verify rollback and
  cleanup, add a focused automated regression test, then rerun the same round
  from a clean baseline.
- When an Elasticsearch join is delayed, the verifier must poll for matching
  cluster UUID and membership using CA-verified HTTPS. Its eventual timeout is
  reported as `ECP_CLUSTER_JOIN_TIMEOUT` without revealing credentials.
- After every fix, rerun the focused test, the full Python suite, strict
  boundaries, relevant frontend checks, and the affected Ansible syntax check.
- Re-run the previously successful round affected by the change before marking
  the new round passed.

## Completion Checklist

- [ ] Every role coverage round passed from a clean test-node baseline.
- [ ] Every successful apply has a matching verified purge and removed
      controller assignment.
- [ ] Resource edits, rollback, detach, purge, collision, stale revision, and
      NIC validation behavior were tested.
- [ ] No round left a managed workload, stale run/lock, failed scoped unit, or
      controller-marked data path on `.101`, `.102`, or `.103`.
- [ ] Unrelated resources and firewalld/Podman safety checks matched baseline.
- [ ] Automated regression and boundary checks passed after the final round.
