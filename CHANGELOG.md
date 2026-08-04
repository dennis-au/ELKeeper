# Changelog

All notable changes to ELKeeper are documented in this file.

## [1.2.0] - 2026-08-04

### Changed

- Refactored the controller into explicit platform, orchestration, host,
  controller-identity, cluster, workload, version, observability, secret,
  certificate, and maintenance modules while retaining compatibility facades.
- Moved frontend behavior into feature-owned API clients, DTOs, components, and
  route-page composition facades without changing the primary application routes.
- Added checked-in route inventory, DTO fixtures, ownership metadata, and a
  regression ledger for modular-controller release evidence.

### Safety

- Added strict checks that reject route-level SQL, private repository imports,
  undeclared cross-owner reads, boundary violations, lab addresses, and insecure
  Podman TCP endpoints.
- Consolidated lifecycle, run, SSE, redaction, migration, remote-command, and
  telemetry seams behind public feature contracts.

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
