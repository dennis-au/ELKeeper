# Changelog

All notable changes to ELKeeper are documented in this file.

## [1.1.0] - 2026-08-03

### Added

- Maintenance Phase 0 foundations: additive migrations, scoped locks, immutable
  plan snapshots, recovery checkpoints, and maintenance policy revisions.
- Provider and ownership capability controls for native Podman, adopted Podman,
  external API-managed, and ECK endpoint-only clusters.
- Authenticated maintenance policy and host plan APIs, plus a dashboard and host
  workflow for reviewing impact, predicates, operations, and recovery state.
- Host resource and network observations used by maintenance planning, alongside
  a staged one-shot post-reboot executor and resume service templates.

### Safety

- Maintenance execution remains capability-gated and fails closed without an
  approved adapter; planning and preview do not mutate managed hosts.
- Elasticsearch maintenance calls use CA-verified HTTPS, structured APIs, and
  redacted checkpoints. Unsupported providers remain read-only.
- Regression coverage now includes migrations, provider boundaries, plan
  idempotency, lifecycle recovery, execution locks, post-return handling, and
  host executor isolation.
