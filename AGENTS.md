# ELKeeper Module Map

This repository is a compatibility-preserving modular monolith. `app.main` and
`app.console` are assembly/compatibility surfaces; new domain code belongs in
the modules below.

## Backend Ownership

- `app/modules/platform`: application lifecycle, runtime paths, SQLite
  connections, migration registry, authentication, security, audit, and runs.
- `app/modules/orchestration`: typed command specifications plus SSH,
  Podman-over-SSH, CA-verified Elasticsearch, and remote-file adapters.
- `app/modules/hosts`: node DTOs, validation, repository, and host service.
- `app/modules/controller_identity`: controller-key contracts and lifecycle
  boundaries.
- `app/modules/clusters`: cluster inventory, memberships, NIC validation, and
  cluster service.
- `app/modules/workloads`: assignment persistence, workload service, topology,
  access URLs, and managed cleanup contracts.
- `app/modules/versions`: image/version contracts, download plans, and upgrade
  safety gates.
- `app/modules/observability`: bounded telemetry, dashboard/runtime HTTP
  routes, stream tokens, and observation projections.
- `app/modules/secrets`: masked secret metadata, reveal-grant HTTP routes, and
  redaction. Legacy catalog and remote-file callbacks are injected during the
  compatibility migration and must not be imported by new modules.
- `app/modules/certificates`: certificate metadata and renewal planning.
- `app/modules/maintenance`: maintenance projections and maintenance-owned
  persistence; cross-owner writes use platform/workload contracts.

Each module exposes public contracts through `__init__.py`, `contracts.py`,
`api.py`, `service.py`, or `repository.py`. Private implementation files must
not be imported by another module. SQL writes stay inside the declared table
owner. Route ownership is recorded in `app/refactor_ownership.py`.

## Frontend Ownership

Feature API/type clients live under `frontend/src/features/` for clusters,
hosts, workloads, versions, dashboard, advanced, maintenance, runs, and auth.
`frontend/src/shared/api.ts` is the transport compatibility wrapper. Pages and
components should depend on feature clients, not construct feature URLs.

When extracting a route, create a builder in the owning module and inject
database, authentication, orchestration, and legacy compatibility providers
from `app.main`. The route module must not import `app.main` or reach another
module's tables directly. Keep compatibility callbacks temporary, test them at
the HTTP boundary, and retire them only after the owning repository/service is
green under the strict table check.

## Verification

Run the complete local gate from `controller_snapshot/`:

```bash
python -m unittest discover -s tests -q
python tools/check_refactor_boundaries.py --root . --strict
```

When frontend/shared contracts change, also run from `frontend/`:

```bash
npm test
npm run typecheck
npm run build
```

All bundled Ansible playbooks must pass `ansible-playbook --syntax-check`.
Secrets, credentials, tokens, and private key material must never appear in
logs, URLs, fixtures, telemetry, or regression ledgers.

When replacing the `.104` controller with Podman under SELinux, preserve the
existing bind mounts and include `:Z` on each controller data, config,
playbook, and SSH-secret mount. These paths retain a per-container MCS label;
omitting `:Z` can make an otherwise valid candidate fail before startup with a
permission error. Verify the replacement reaches `/api/health` before treating
the deployment as complete.
