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

## Application Development Guidelines

### Module Contract

Each module must own one cohesive responsibility and expose a small public
contract. The contract may include DTOs, interfaces, repository/service
methods, router builders, commands, or events. Internal helpers, SQL details,
Ansible variables, and provider-specific implementation files are private.
Other modules may import only package exports and documented public contract
files.

The owning module is responsible for its persistence boundary. It owns table
creation and migrations, SQL writes, repository invariants, and module-level
read projections. A cross-owner read must be explicit and narrow; a
cross-owner write must call the owner's public service or repository. Never
solve a boundary violation by adding a broad exception to the checker.

### Backend Implementation

- Keep `app.main` limited to application assembly, dependency injection,
  lifecycle wiring, route registration, and temporary compatibility delegates.
- Keep route functions thin: parse and authorize input, invoke an owning
  service, and return the established response DTO.
- Put all remote effects behind orchestration adapters. Do not assemble raw
  SSH, Podman, Ansible, or Elasticsearch commands in domain routes.
- Mutations must use platform run creation/status/events, audit where needed,
  redacted output, and an explicit recovery or rollback path for partial work.
- Prefer immutable DTOs at module boundaries and validate values before any
  persistence or remote side effect.
- Keep provider-specific behavior behind provider capability contracts; do not
  let a native, imported, or ECK-managed resource fall through to Podman
  mutation code.

### Frontend Implementation

- A feature owns its API client, DTOs, query keys, hooks, components, forms,
  and tests under `frontend/src/features/<feature>`.
- Route pages are composition layers. They may coordinate feature state, but
  must not construct feature URLs, call unrelated feature internals, or own
  domain validation.
- `frontend/src/shared` may contain only dependency-light transport, UI,
  formatting, clipboard, and query helpers that are genuinely reusable.
- Use the existing in-page dialogs and run drawer for operations. Do not add
  browser-native dialogs or direct managed-service connections.
- Preserve accessible focus behavior, loading/error/degraded states,
  responsive layouts, secret masking, and SSE reconnect behavior during an
  extraction.

### Testing A Module

Every module change requires:

1. Unit tests for validation, transformation, and failure/rollback rules.
2. Module integration tests through the public contract, without importing
   private implementation files.
3. Boundary checks for route ownership, private imports, and table ownership.
4. Redaction tests for commands, run context, URLs, telemetry, and fixtures
   whenever secrets or remote operations are involved.
5. The smallest named regression profile that covers the changed backend,
   frontend, migration, Ansible, or packaging surface.

Compatibility tests must remain until the old route/import/DTO seam is retired.
When retiring a seam, remove the implementation only after the owning module
tests and a full regression profile pass.

### Adding Or Moving Code

Before editing, record the responsibility, owner, public contract, table
owner, provider boundary, and compatibility plan. Add the public contract and
focused tests before moving private code. Move one vertical slice at a time,
keep imports pointed inward toward public contracts, and run the boundary
checker after each slice. Update the module map when ownership changes.

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
