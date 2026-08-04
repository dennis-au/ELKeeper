# ELKeeper Modular Refactor Plan

## Document Status

- Status: Complete. The audit gaps were corrected and the exact source was released as `refactor-audit67-20260804`. Route handlers contain no SQL; private repository-file imports are rejected; maintenance cross-owner reads are confined to declared read projections; route pages are composition facades; and runs, advanced, maintenance, and shared frontend implementations are owned by their feature/shared modules. The final controller-only gate completed without changing any Elastic workload host.
- Date: 2026-08-04
- Audience: ELKeeper maintainers and regression-test operators
- Scope: Active refactor plan. Runtime behavior remains compatibility-first.

## Progress Evaluation

Evaluated on 2026-08-04 against the active source tree and the regression
ledger. A milestone is marked complete below only when its gate is satisfied;
an extracted contract or passing unit test alone is recorded as partial work.

| Phase | Status | Evidence | Remaining gate work |
| --- | --- | --- | --- |
| 0. Baseline/contracts | Passed | Route ownership registry, checked-in route inventory, golden DTO fixtures, redaction checks, and unknown-route tests pass; all current routes have owners. | Keep the baseline fixtures synchronized when public DTOs change. |
| 1. Platform extraction | Passed | Platform owns lifecycle, DB, migrations, security, audit, typed runs/SSE, static serving, redaction, and durable migration/recovery fixtures. `app.main` has no route decorators or route-level SQL; its remaining platform callbacks are documented assembly or patch-compatible facade wiring. | Keep compatibility contracts and migration fixtures current when a public behavior changes. |
| 2. Orchestration gateway | Passed | Typed SSH, Podman-over-SSH, CA-verified Elasticsearch, transfer, command, timeout, ambiguous-outcome, and cleanup adapters are feature-owned and tested. Route modules do not construct remote commands directly. | Keep adapter outcome and redaction coverage mandatory for new remote operations. |
| 3. Hosts/controller identity | Passed | Host and controller-identity routes, repositories, enrollment/key services, probes, observations, and lifecycle/batch launch composition are public host contracts. `HostLifecycleOperations` owns the final host lifecycle wiring removed from `app.main`. | Keep legacy facade seams only while external callers need them. |
| 4. Clusters/workloads | Passed | Cluster/workload lifecycle, membership, settings, zoning, assignment validation, resources, apply batches, cleanup, topology, access URLs, and public persistence projections are module-owned. The workload feature client owns role-page workload calls. | Preserve API DTO and worker recovery coverage for future workload changes. |
| 5. Versions/observability/secrets/certificates | Passed | Version discovery/upgrade orchestration, telemetry, tunnel lifecycle, sensitive reveal, certificate metadata, and feature clients are module-owned behind public contracts. Certificate renewal execution remains a separately scoped product capability, not unfinished refactor work. | Keep download-only immutability, upgrade gates, and secret-redaction tests mandatory. |
| 6. Maintenance integration | Passed | Maintenance implementations and persistence use owned repositories plus explicit public platform/workload/host contracts. Top-level maintenance modules are compatibility facades, and strict checks limit permitted cross-owner read projections. | Preserve maintenance phase-gate coverage before enabling new mutation capability. |
| 7. Frontend modularization | Passed | Auth, clusters, hosts, workloads, dashboard, versions, advanced, maintenance, and runs own their clients, DTOs, components, and tests. Route pages are composition facades; root `api.ts` and `types.ts` are compatibility re-exports only. | Keep feature-boundary checks required for new UI work. |
| 8. Enforcement/retirement | Passed | Strict route, backend-import, frontend, and table ownership checks reject direct route SQL, private repository-file imports, undeclared maintenance read adapters, and route-page implementations. The current fifteen-minute image gate and guarded live replacement passed. | Keep these gates required for future work. |

### Current Final Acceptance (2026-08-04)

The reopened audit and final host-lifecycle extraction are complete. The current release passed the exact-source
fifteen-minute profile, including 390 in-image Python tests, 90 Vitest tests,
TypeScript, production build, all bundled Ansible syntax checks, strict route,
backend-import, frontend, and table boundaries, source-safety checks, and the
isolated health/login/authenticated-core-API/static/five-SPA-route/SSE smoke.

The new `localhost/elastic-control-plane:refactor-audit67-20260804` image
(`fb3f8c712201cb578ca3440354ab222711f7a61858ba819c347773e35cb4b559`)
was guardedly deployed to `.104` after verified backup
`/root/control.db.refactor-audit67-20260804161844.bak` (2,379,776 bytes).
The source-input fingerprint was
`494507abcd887a45a47d930c3eaef7e343eaeaf7e9b8af23634c803a487d4b9c`.
The controller has zero restarts; health, authenticated cluster access, SPA
routes, and expected unauthenticated protection are verified; persistent
database counts are unchanged. Compatibility files are documented public
facades, not alternate implementations, and all Phase 0-8 acceptance gates
are satisfied.

### Superseded Closure Record (2026-08-04)

This is historical release evidence for `refactor-final64-20260804`, not the
status of the active source. The later audit found boundary gaps, so its final
statuses must not be used as current acceptance evidence.

| Phase | Final status | Closure evidence |
| --- | --- | --- |
| 0 | Passed | Golden fixtures, route inventory, and ownership registry are strict-gated. |
| 1 | Passed | `app.main` is assembly/lifecycle/compatibility glue with no route decorators or literal SQL; platform bootstrap and recovery fixtures pass. |
| 2 | Passed | Typed SSH, Podman, Elasticsearch, and transfer adapters cover failure, timeout, cleanup, and ambiguous outcomes. |
| 3 | Passed | Host and controller-identity behavior is behind owned routes, repositories, services, and public facade contracts. |
| 4 | Passed | Cluster/workload lifecycle, membership, validation, resources, topology, and batch contracts are module-owned; remaining main functions are patch-compatible delegates. |
| 5 | Passed | Versions, observability, secrets, certificates, and console dependencies are feature-owned; certificate renewal execution remains a separately planned product capability. |
| 6 | Passed | Maintenance implementation and persistence are module-owned; legacy top-level modules are compatibility facades. |
| 7 | Passed | All frontend domains have feature clients; root `api.ts` and `types.ts` are compatibility re-exports. |
| 8 | Passed | Strict route/backend/frontend/table gates, fifteen-minute packaging, and guarded live controller verification passed. |

## Final Acceptance Audit

- Route and persistence ownership: the registry owns all 69 browser/API routes
  and every declared SQLite table. Strict route, import, frontend, and table
  checks passed from the controller host.
- Assembly boundaries: `app.main` has no route decorators or literal SQL;
  `app.console` is an alias to the injected console runtime; neither console
  surface has direct SQL. Module sources do not import the application
  assembly or console compatibility modules.
- Frontend boundaries: the feature packages own cluster, host, workload,
  version, dashboard, advanced, maintenance, runs, and auth code. Global
  `api.ts` and `types.ts` are compatibility re-exports only.
- Exact-source verification: the `.104` fifteen-minute profile passed with
  378 image-backed Python tests, 90 Vitest tests, TypeScript, production build,
  all bundled Ansible syntax checks, strict ownership/source-safety gates, and
  an isolated health/login/authenticated API/static/SPA/SSE smoke.
- Guarded deployment: a non-empty SQLite backup was created outside the
  persistent data mount. The live controller was replaced with the verified
  image, retained the expected database counts, passed health, authenticated
  core API, five SPA-route, and SSE checks, and remained at zero restarts.
  No managed Elastic workload or testing node was changed.

### Verified Regression Baseline

- 350 Python/API tests pass in the current local source tree after the workload-operation composition extraction. The current `.104` controller is the guarded `refactor-final54-20260804` deployment built from this source state.
- 90 frontend tests pass; TypeScript and production build remain green.
- All bundled Ansible playbooks pass syntax checks.
- Candidate image health, login, SPA redirect, and static delivery pass in an
  isolated container on `.104`.
- The Phase 6 image initially exposed a packaging defect because `tools/` was
  absent from the image even though in-image contract tests invoke those
  scripts. `Containerfile` now copies `tools/`; the rebuilt image passes all
  274 Python tests in-container.
- On `.104`, the Node 22 frontend profile also passes Vitest, TypeScript, and
  production build. Existing EUI SSR pseudo-class/table-width notices and the
  large-bundle advisory remain warnings, not failures.
- The live controller was replaced using the guarded procedure after a
  non-empty database backup; no destructive Elastic workload round was
  required.
- The current source has been rebuilt and guardedly replaced on `.104` as
  `refactor-final31-20260804`; the image ID and backup are recorded in the
  regression ledger.
- Detailed evidence is recorded in
  [refactor_regression_ledger.md](refactor_regression_ledger.md).

### Current extraction slice (2026-08-03)

- The concrete telemetry collector now lives in
  `app/modules/observability/collector.py` behind the injected
  `TelemetryDependencies` contract. `app.console_runtime` retains only the
  compatibility constructor and dynamic dependency wiring needed by existing
  tests and callers; collector publish/subscribe behavior has a module-level
  contract test.
- Host storage traversal and marker-safe mount eligibility now live in
  `app/modules/hosts/storage.py`; the compatibility renderer delegates to this
  pure host contract so storage policy remains testable without console code.
- Version upgrade run creation and scheduling now live in
  `app/modules/versions/launcher.py`; `main.launch_upgrade` supplies only the
  application adapters and preserves the existing run target and ordering.

- Dashboard and version API/type clients now live under their owning feature
  modules, with page-level compatibility mocks preserved during migration.
- Workload/role API operations now use the `features/workloads` client, and
  maintenance run lifecycle writes use the maintenance repository contract.
- Advanced, maintenance, runs, and auth feature clients now provide public
  frontend contracts; AdvancedPage, ControllerIdentityPanel, App login, and
  ActionConsole use those contracts without changing route behavior.
- Platform bootstrap owns runtime directory creation and schema introspection;
  concrete orchestration adapters are exported through the public gateway.
- `AGENTS.md` now documents module ownership, public-contract rules, frontend
  feature boundaries, and the required verification commands.
- The table ownership checker scans only SQL passed to database execution calls,
  recognizes `REFERENCES` and trigger statements, reports unknown tables, and
  permits only explicitly registered read-only maintenance projections. Strict
  mode now passes with no unregistered or cross-owner SQL access.
- Dashboard snapshot, stream-token, and SSE routes were moved to
  `app/modules/observability/http.py`; node runtime now uses the same router
  with host and observation providers. The original SSE event names, headers,
  scoped-token behavior, and bounded run projection are unchanged.
- Controller settings now use the platform configuration contract, and the
  sensitive-item catalog, reveal-grant, and audited reveal routes were moved to
  `app/modules/secrets/http.py`. Compatibility catalog/remote callbacks remain
  in `console.py` until their persistence and orchestration implementations are
  extracted.
- Host storage inventory now has an owned hosts HTTP router; remote execution
  and storage eligibility remain injected compatibility providers until the
  host orchestration adapter and storage repository slices are complete.
- Host lifecycle actions and cluster settings now have owned HTTP routers. The
  routes preserve the existing run IDs, maintenance guards, validation, and
  callback seams while the remaining host/cluster persistence and orchestration
  implementations are extracted.
- Zoning reconciliation now has a cluster-owned `ZoningWorker` (with the
  documented `ZoningService` compatibility alias). It owns preflight checks,
  variable-file cleanup, workload ordering, Elasticsearch settings application,
  rollback, host-zone changes, and run finalization. `app.main` retains only
  callback-compatible delegates so existing tests can patch the command seams.
- Host network-interface parsing and collection now live in
  `app/modules/hosts/network.py`. The console retains only compatibility
  wrappers, while malformed remote payloads and command failures have focused
  deterministic coverage.
- Certificate runtime helpers now live in `app/modules/certificates/runtime.py`:
  CA-verified SSL contexts, cluster-scoped CA cache paths, and idempotent cache
  invalidation. Filebeat launch/run scheduling is owned by
  `FilebeatReconcileWorker`; `main.py` retains only compatibility delegates.
- Observability now owns SSH connection pooling, Podman Unix-socket tunnel
  lifecycle, container/resource parsing, host rate calculation, and node/zone
  dashboard aggregation. `console_runtime` keeps only thin compatibility
  facades around these helpers while the existing console test seams remain
  stable.
- The frontend transport/auth implementation now lives in `shared/api.ts`,
  the compatibility query catalog is in `shared/queries.ts`, and the legacy
  `api.ts` contains only re-exports. Existing page and feature tests retain
  their mocked request seams and all frontend gates remain green.

### Current gate blockers

- Compatibility implementations still live in `app/main.py` and `app/console.py`;
  they remain deliberate shims until route-level extraction tests prove safe
  retirement.
- The current source has now been rebuilt and guardedly deployed on `.104` as
  `localhost/elastic-control-plane:refactor-final24-20260803`; compatibility
  shim retirement and the remaining worker/runtime extraction gates remain
  intentionally open.

## Summary

ELKeeper should remain a modular monolith packaged as one controller image, but
the current source layout needs stricter module boundaries. The present backend
already has a meaningful maintenance slice, yet app/main.py and app/console.py
still mix application bootstrap, DTOs, schema migration, repositories, route
handlers, orchestration, run streaming, Ansible invocation, cluster membership,
workload lifecycle, telemetry, secrets, versions, and topology rendering. The
frontend has the same pattern at a smaller scale: feature pages own API calls,
state orchestration, form logic, and rendering in single page files.

The refactor goal is not to create many small files. The goal is to make each
domain own its contract, persistence boundary, tests, and operational behavior
so future maintenance, import, upgrade, certificate renewal, and provider work
can land without expanding the two existing hub files.

## Refactor Goals

- Keep the single-image delivery model and existing external APIs stable.
- Move from file-oriented organization to module-oriented ownership.
- Make import direction enforceable and testable.
- Put persistence behind module repositories instead of scattered SQL.
- Keep route handlers thin: parse request, call service, return DTO.
- Keep Ansible and remote host mutation behind typed gateways.
- Let frontend features consume typed API clients rather than raw path strings.
- Preserve run/SSE behavior, audit behavior, redaction, and cleanup boundaries.
- Refactor incrementally with no broad rewrite and no live workload mutation.

## Non-Goals

- Split ELKeeper into microservices.
- Add a second database or message broker.
- Replace FastAPI, SQLite, React, TanStack Query, EUI, or Ansible.
- Change current API semantics without a deliberate compatibility adapter.
- Move generated frontend output into source ownership.
- Rewrite all playbooks into roles before module boundaries are defined.
- Mix large feature work with refactor work.

## Current Boundary Problems

### Backend

- app/main.py is the central dependency hub at more than 4000 lines. It owns
  Pydantic request models, DB schema creation, cluster DTO assembly, validation,
  run launching, command streaming, versioning, topology rendering, workload
  batch orchestration, cluster CRUD, host CRUD, resources, assignments, and
  fallback frontend routing.
- app/console.py owns dashboard telemetry, SSH helpers, Podman tunnels, host
  runtime probes, storage inventory, host lifecycle routes, cluster settings,
  sensitive item catalog, reveal flow, and CA caching.
- Maintenance code is partly modularized, but some API wiring imports app.main as
  a broad core object, which keeps the old hub as a hidden dependency.
- SQL is distributed across route handlers and services. Table ownership is
  implied by file location rather than declared by module contract.
- Run creation and Ansible launch behavior are repeated through ad hoc helpers.
- DTOs used by API, services, and frontend are not generated or grouped by module
  contract.
- Playbooks are called from multiple places without a shared typed command/event
  boundary.

### Frontend

- frontend/src/api.ts is a global raw API helper plus a small query catalog.
- frontend/src/types.ts is a shared type bucket for every domain.
- Feature pages still combine data fetching, mutation handling, forms, tables,
  dialogs, and page layout.
- Maintenance UI components are better isolated and should become the model for
  other feature modules.
- Tests are page-oriented; module-level contract and state tests are uneven.

## Module Rules

A module must have:

- A clear responsibility and owner.
- A public contract: imported interfaces, API endpoints, commands/events, DTOs,
  and supported side effects.
- Private implementation details that other modules cannot import directly.
- Ownership of its own tables/schema, or at minimum an explicit persistence
  boundary when tables are shared.
- Unit tests plus module-level integration tests.
- A documented redaction policy for any secret, credential, key, certificate, or
  sensitive operational artifact it handles.
- A documented failure and recovery behavior for every mutation it owns.

### Required Package Shape

Backend modules should use this shape unless a module is intentionally tiny:

    app/modules/<module_name>/
      __init__.py          public exports only
      contracts.py         public DTOs, commands, events, interfaces
      api.py               FastAPI router only
      service.py           orchestration and business rules
      repository.py        persistence boundary for owned tables
      schema.py            additive migrations and table metadata
      adapters.py          optional external systems, Ansible, SSH, HTTP clients
      _private.py          helpers not importable outside the module

Frontend modules should use this shape:

    frontend/src/features/<feature_name>/
      api.ts               typed client functions and query keys
      types.ts             feature DTOs and view models
      components/          reusable feature components
      pages/               route-level components
      hooks.ts             feature-specific state hooks
      __tests__/           unit and feature integration tests
      index.ts             public exports only

### Import Rules

- app.main may assemble the FastAPI application but must not own domain logic.
- app.modules.<name>.api may import its own service and public contracts only.
- A module may import another module's contracts.py or API router only when that
  dependency is declared in the module catalog.
- A module must not import another module's repository.py, schema.py, _private.py,
  test fixtures, or implementation adapters.
- Shared primitives live in app/platform or app/shared, not in a domain module.
  Examples: database connection, crypto envelope, auth dependency, time/format
  helpers, redaction, run event stream, and command execution interfaces.
- Domain services may depend on platform interfaces, not concrete FastAPI request
  objects.
- Ansible playbook invocation must go through an orchestration gateway contract.
  Route handlers must not assemble raw ansible-playbook command arrays.
- Frontend feature pages import from their own feature module and shared UI
  primitives. They should not reach into another feature's private components or
  raw API paths.
- Cross-module events must be typed. Do not encode control flow in run target
  string prefixes.

### Persistence Rules

- Every table has exactly one owning module.
- Cross-module references use foreign keys or repository methods exposed through
  public contracts.
- Schema changes are additive and versioned.
- Migrations must be idempotent and tested against old, current, partially
  migrated, and interrupted databases where relevant.
- Shared tables such as runs and audit_events are platform-owned. Domain modules
  may append through platform services, not direct SQL.
- Ownership-sensitive tables must include provider/ownership fields before any
  imported or adopted resource becomes mutable.
- Repository methods return typed records and never leak encrypted secret values
  unless the public contract explicitly supports an audited reveal flow.

### Test Rules

Each module must provide:

- Unit tests for pure validation, DTO conversion, policy, and state transitions.
- Repository tests for schema, migrations, constraints, and redaction.
- API tests for auth, request validation, success, failure, and compatibility.
- Adapter/stub tests for Ansible, SSH, Podman, HTTP, Elasticsearch, or CA
  providers.
- Module integration tests that cross the public contract but do not import
  private implementation.
- Regression tests proving existing routes and UI behavior remain compatible
  while the module is being extracted.

## Target Backend Module Catalog

| Module | Responsibility | Public contract | Persistence boundary |
| --- | --- | --- | --- |
| platform.app | FastAPI assembly, lifespan, static files, middleware, route registration | App factory, router registry, lifecycle hooks | None |
| platform.config | Environment, paths, feature flags, runtime directories | Settings DTO, flag lookup, path registry | controller_settings only through settings service |
| platform.db | SQLite connection and migration runner | DB connection factory, migration registry | Migration ledger |
| platform.security | Password hashing, tokens, crypto envelope, redaction | Auth dependency, token DTOs, encrypt/decrypt interfaces, redaction helpers | users; encrypted value handling |
| platform.runs | Run records, logs, SSE stream, command events | RunCommand, RunRecord, run create/append/finish APIs, SSE endpoints | runs |
| platform.audit | Audited security and mutation events | Audit event API, redacted detail contract | audit_events |
| orchestration | Ansible, SSH, command execution, temporary variable files | PlaybookRequest, CommandRequest, execution receipts | Runtime files only |
| controller_identity | Controller SSH keys and host key migration | Key status DTOs, generate/import/activate commands | controller_ssh_keys; key columns on nodes via host contract |
| hosts | Host inventory, enrollment, probe, runtime, storage, zones | Host CRUD API, enrollment commands, runtime/storage DTOs | nodes, host_runtime_observations |
| clusters | Cluster catalog, provider profile, settings, membership, ports, zones | Cluster CRUD, membership DTOs, provider contract, settings commands | clusters, memberships, cluster_zoning_observations |
| workloads | Role catalog, assignments, resources, apply/detach/purge, topology | Role specs, assignment DTOs, workload change commands, topology DTO | cluster_assignments, workload_change_batches |
| certificates | Certificate inventory, expiry metadata, issuance/rotation planning | Certificate metadata DTOs, renewal commands, CA provider interfaces | Certificate metadata tables when added; remote paths through contracts |
| secrets | Sensitive item catalog and reveal grants | Sensitive item DTOs, reveal grant commands, masked responses | Secret fields in owning modules; reveal audit |
| versions | Registry lookup, runtime version observations, download-only, upgrades intent | Version list, observation, download, upgrade command DTOs | workload_observations, desired image fields via workloads |
| observability | Dashboard telemetry, metrics history, Podman tunnels, cluster health | Dashboard snapshot, stream token, telemetry events | In-memory bounded windows plus observation writes |
| maintenance | Policies, plans, locks, predicates, reboot/restart/upgrade safety | Maintenance policy, plan, step, lock, recovery contracts | maintenance_* tables |
| log_monitoring | Filebeat/Metricbeat companion configuration and status | Log monitoring settings, reconcile command, companion observations | Observability JSON in clusters, companion fields in observations |
| frontend_gateway | SPA fallback and static asset routing | Non-API route handling | None |

The first milestone should not move every module at once. It should make the
catalog enforceable, then extract one stable vertical slice at a time.

## Target Frontend Module Catalog

| Feature | Responsibility | Public contract |
| --- | --- | --- |
| features/auth | Login/logout/session expiry | Auth client, token storage, auth events |
| features/runs | Action console and SSE run tracking | Run query keys, console context, run drawer components |
| features/hosts | Host inventory, enrollment, probe, initialize, storage, maintenance entry | Host API client, host DTOs, host page components |
| features/clusters | Cluster CRUD, ports, provider, membership, zoning, settings | Cluster API client, cluster DTOs, editor components |
| features/workloads | Roles, assignments, resources, apply/detach/purge, topology | Workload API client, role catalog, workload tables/forms |
| features/versions | Version observations, registry choices, download/upgrade actions | Version API client, version panel components |
| features/maintenance | Policies, plan preview, operation progress/recovery | Existing maintenance components plus typed clients |
| features/dashboard | Telemetry dashboard, charts, cluster metrics | Dashboard API client, chart components |
| features/advanced | Secrets, certificate metadata, reveal flow, controller identity | Sensitive item client, reveal dialogs, metadata panels |
| shared/ui | Layout, dialogs, notifications, empty/error states | EUI wrappers and shared presentational components |
| shared/api | Fetch wrapper, error model, query key helpers | Typed request helper, error DTO |
| shared/format | Formatting and labels | Pure formatting helpers |

## Public Contract Template

Every module must include a contract block like this in its README or
contracts.py / types.ts comments:

    Module:
    Owner:
    Responsibility:
    Public imports:
    Public API endpoints:
    Public commands/events:
    DTOs:
    Owned tables:
    Shared tables touched through:
    Private files:
    Allowed dependencies:
    Forbidden dependencies:
    Side effects:
    Redaction rules:
    Recovery behavior:
    Unit tests:
    Module integration tests:

## Proposed Repository Layout

    controller_snapshot/
      app/
        main.py                    app factory and route registration only
        platform/
          app.py
          config.py
          db.py
          security.py
          runs.py
          audit.py
          redaction.py
        modules/
          hosts/
          clusters/
          workloads/
          versions/
          observability/
          secrets/
          certificates/
          maintenance/
          log_monitoring/
          controller_identity/
        orchestration/
          ansible.py
          ssh.py
          podman.py
          elasticsearch.py
      ansible/
        playbooks/
        roles/
          hosts/
          workloads/
          maintenance/
          monitoring/
      frontend/src/
        shared/
        features/
      tests/
        platform/
        modules/
        integration/

This is the target shape. Do not create empty packages before moving real
ownership into them.

## Action Plan

### Phase 0: Baseline, Contracts, And Enforcement

- Record the current route inventory, table inventory, module imports, test
  coverage, and file-size hotspots.
- Add refactor contract documents for the target catalog.
- Add a lightweight import-boundary check script that initially reports only.
- Add a table-ownership registry used by tests to flag new unowned tables.
- Add route compatibility tests for every existing endpoint before moving code.
- Add API golden-response fixtures for high-risk DTOs: clusters, nodes, roles,
  versions, topology, dashboard, sensitive items, maintenance plans, and runs.

Gate: no runtime behavior changes; current Python, frontend, typecheck, and
changed-playbook syntax checks pass; the import-boundary report is understood.

### Phase 1: Platform Extraction

- Extract environment/path settings, feature flags, app factory, lifespan hooks,
  static file handling, and security middleware from app/main.py.
- Extract database connection and migration registration into platform.db.
- Extract auth, password hashing, token handling, crypto envelope, and redaction
  helpers into platform.security.
- Extract run creation, run log append, status transitions, SSE event delivery,
  and command output persistence into platform.runs.
- Extract audit event writing into platform.audit.
- Leave compatibility wrappers in app/main.py and app/console.py until all call
  sites are migrated.

Gate: app/main.py creates the app and registers routers; platform tests own
DB/auth/runs/audit behavior; all existing endpoints still pass.

### Phase 2: Orchestration Gateway

- Create typed Ansible, SSH, Podman, Elasticsearch, and remote-file gateway
  interfaces.
- Move temporary variable file creation, command logging, secret-safe command
  rendering, private key selection, host-key options, and cleanup into
  orchestration.
- Replace raw command-array construction in route handlers with gateway calls.
- Keep playbook names and variables stable during this phase.
- Add stub tests for success, failure, timeout, ambiguous remote outcome,
  temporary file cleanup, and secret redaction.

Gate: no route handler directly assembles ansible-playbook; run logs remain
redacted; destructive playbooks are not executed by unit tests.

### Phase 3: Host And Controller Identity Modules

- Move node DTOs, host validation, enrollment, password test, probe, initialize,
  deinitialize, delete, zone update, storage inventory, runtime observation, and
  controller-key operations into hosts and controller_identity.
- Move host table SQL into repositories owned by those modules.
- Preserve current endpoints by registering routers at the same paths.
- Add compatibility tests for host enrollment, key installation, legacy key
  behavior, zone changes, storage filtering, probe, init/deinit guards, and
  delete/revoke flows.

Gate: host lifecycle behavior is unchanged; host repositories are the only
writers for nodes and host_runtime_observations.

### Phase 4: Cluster And Workload Modules

- Move cluster DTOs, port profiles, provider fields, membership validation,
  settings, zoning, log-monitoring settings, and cluster CRUD into clusters.
- Move role catalog, assignment validation, resource edits, apply, batch apply,
  detach, purge, topology rendering, and workload payload assembly into
  workloads.
- Replace direct cross-table SQL with repository/service contracts.
- Make provider and ownership checks explicit before any workload mutation.
- Keep legacy unclustered role endpoints returning the same compatibility errors.
- Add module integration tests for cluster CRUD, provider changes, memberships,
  shared/dedicated NIC validation, port conflicts, role assignment, topology,
  detach, purge safety, and resource rollback.

Gate: clusters owns clusters, memberships, and zoning observation tables;
workloads owns cluster_assignments and workload batches; no other module writes
those tables directly.

### Phase 5: Versions, Observability, Secrets, And Certificates

- Move registry lookup, available versions, runtime observations, download-only,
  and upgrade request validation into versions.
- Move dashboard telemetry, cluster metrics, Podman tunnel pooling, and stream
  token behavior into observability.
- Move sensitive item catalog, reveal grants, certificate metadata, and future
  certificate renewal planning into secrets and certificates.
- Define clear contracts between certificates, workloads, and maintenance for
  certificate rotation without importing private workload internals.
- Add tests for version cache limits, timeout behavior, download-only immutability,
  dashboard stale/degraded states, reveal audit, certificate metadata redaction,
  and no secret leakage.

Gate: console.py no longer owns unrelated host, telemetry, settings, and secret
routes; it can be deleted or reduced to a compatibility import module.

### Phase 6: Maintenance Integration Cleanup

- Move remaining maintenance API dependencies away from app.main as core and
  onto explicit platform, host, cluster, workload, run, and orchestration
  contracts.
- Make maintenance modules consume public contracts only.
- Replace run target string parsing with typed run context and event records.
- Ensure maintenance locks and provider capabilities are the only conflict
  authority for maintenance-controlled mutations.
- Add import-boundary tests proving maintenance cannot import private repositories
  from host, cluster, or workload modules.

Progress: the route dependency slice is complete. Maintenance API authentication,
database access, mutable capability gates, and telemetry are now obtained from
public contracts without importing `app.main` or `app.console`. Compatibility
aliases remain deliberately exported by `app.main` while the remaining typed run
context extraction is developed. Lifecycle, recovery, planning, provider,
status, model, predicate-safety, planning-service, observation, execution, signed-executor, Elasticsearch protocol, reboot, post-return, runtime, and controller-I/O implementations now live under
`app.modules.maintenance`; their former top-level modules are compatibility
facades only.

Gate: maintenance continues to pass all existing Phase 0-5 tests from
maintenance_plan.md; no later-phase action is enabled without its recovery path.

### Phase 7: Frontend Feature Modularization

- Create shared/api, shared/ui, shared/format, and features/* packages.
- Move typed API functions and query keys from global api.ts into feature
  clients while preserving a compatibility export during migration.
- Split large pages by feature-owned forms, tables, dialogs, panels, and hooks.
- Keep route-level files as composition only.
- Move global types.ts into feature types.ts files with shared DTO imports.
- Add module-level tests for each feature and keep route smoke tests for all five
  primary routes.
- Preserve action console behavior and in-page dialogs throughout.

Gate: frontend typecheck and Vitest pass; no feature imports another feature's
private files; page files are composition surfaces rather than logic hubs.

### Phase 8: Enforce And Retire Compatibility Shims

- Turn the import-boundary script from report-only to failing in the five-minute
  profile.
- Add ownership checks for table writes and router registration.
- Remove compatibility wrappers after all call sites move.
- Update AGENTS.md with the final module map and boundary rules.
- Update README/developer docs with module owners and extension examples.
- Track remaining debt explicitly instead of leaving ambiguous old helpers.

Gate: all named test profiles pass as applicable; app/main.py, app/console.py,
global frontend/src/types.ts, and global frontend/src/api.ts are reduced to
approved compatibility surfaces or removed.

## Revised Execution Sequence

The previous execution treated extracted contracts as completed phases. The
remaining work now follows the stricter gate model above.

1. **Close Phase 0 and establish fixtures.** Generate checked-in, redacted
   golden responses for clusters, nodes, roles, versions, topology, dashboard,
   sensitive items, maintenance plans, and runs. Record route, table, import,
   and file-size inventories in the ledger.
2. **Finish platform and orchestration boundaries.** Move app assembly,
   lifecycle, auth/token handling, SSE, migrations, temporary artifacts, and
   remote execution adapters behind public contracts. Keep endpoint paths and
   run IDs unchanged.
3. **Complete host and controller identity ownership.** Move every node and
   host-runtime write behind repositories and services, then migrate host
   routes to the module router. Keep legacy enrollment and key behavior under
   compatibility tests.
4. **Complete cluster and workload ownership.** Migrate cluster, membership,
   assignment, batch, resource, detach, purge, provider, and topology flows as
   one vertical slice. Enforce ownership checks before remote mutation.
5. **Complete versions, observability, secrets, and certificates.** Extract
   registry/download/upgrade state machines, telemetry/tunnel ownership,
   reveal/audit flows, certificate metadata, and renewal planning.
6. **Clean maintenance integration.** Replace `app.main as core` with typed
   contracts and typed run events. Prove maintenance locks and provider
   capabilities remain the sole conflict authority.
7. **Finish frontend feature migration.** Move remaining API clients, query
   keys, DTOs, forms, hooks, dialogs, and panels into feature packages. Keep
   route pages as composition surfaces and preserve the action console.
8. **Enable enforcement and retire shims.** Make both boundary checkers and
   table/router ownership checks fail the five-minute profile, remove approved
   compatibility wrappers, update developer documentation, and run the full
   acceptance profile.

### Per-Milestone Rule

Every numbered milestone must produce a small, reviewable change set and pass
the five-minute profile before the next milestone starts. Changes involving
app assembly, migrations, packaging, authentication, shared UI, or telemetry
also require the fifteen-minute profile. Changes involving remote workload,
maintenance, upgrade, certificate, or cleanup behavior require the applicable
destructive profile and managed-only cleanup evidence. A phase remains open if
any required check is skipped because the implementation is incomplete.

## Testing Strategy

Refactor phases should use the named profiles in AGENTS.md:

- Ordinary extraction: run the five-minute profile.
- App factory, schema, routes, packaging, auth, telemetry, or UI infrastructure:
  run the fifteen-minute profile before deployment.
- Any destructive host behavior, workload mutation, maintenance execution,
  upgrade, certificate activation, or cleanup behavior: run the full profile or
  the phase-specific destructive rounds called out in the relevant plan.

Additional refactor-specific tests:

- Import-boundary tests: prove private module files are not imported externally.
- Table-ownership tests: prove each table has a declared owner and unauthorized
  direct writes are not introduced.
- API compatibility tests: compare status codes and redacted payload shapes
  before and after extraction.
- Run-log tests: prove redaction and SSE behavior survive moving run logic.
- Playbook contract tests: prove variable payloads are unchanged unless the
  change is deliberate and documented.
- Frontend route tests: prove all five routes render and core actions remain
  reachable at desktop and mobile widths.

## Migration Discipline

- Move one vertical slice at a time.
- Keep public endpoints stable during extraction.
- Prefer wrapper-and-move over rewrite-and-retest.
- Commit or record each phase separately with a clear rollback point.
- Do not combine feature work with module extraction.
- Do not move generated frontend/dist.
- Do not bulk-sync over live .env, data, config, or playbooks.
- Do not run destructive tests unless the phase explicitly requires them and the
  participating test nodes are cleaned afterward.

## Acceptance Criteria

- Every module has a named owner, public contract, persistence boundary, private
  implementation, unit tests, and module integration tests.
- app/main.py is limited to app assembly, shared middleware, static fallback, and
  router registration.
- app/console.py is removed or limited to compatibility imports.
- Raw SQL writes occur only inside the owning module's repository.
- Route handlers no longer assemble Ansible commands or perform direct remote
  mutation logic.
- Frontend route pages are composition files, not feature logic hubs.
- Import-boundary and table-ownership checks run in the five-minute profile.
- Existing APIs, run/SSE behavior, audit behavior, redaction, packaging, and
  live controller persistence remain compatible.
- Destructive regression rounds still clean every participating test node and
  preserve unrelated resources.

## Superseded Extraction Evidence (Historical)

- This section records intermediate evidence only. Its incomplete-phase
  statements were superseded by the final `refactor-audit67-20260804`
  acceptance record above.

- Version and topology route ownership is complete in `app/modules/versions/http.py` and `app/modules/workloads/http.py`; registry policy, download-only behavior, upgrade gates, topology rendering, and response shapes remain unchanged.
- Cluster inventory reads (`GET /api/clusters` and `GET /api/clusters/{cluster_id}`) now use `app/modules/clusters/http.py` with injected repository/projection callbacks.
- Maintenance startup recovery calls the platform-owned `mark_recovery_required_in_connection` run contract instead of writing the `runs` table from maintenance storage.
- The frontend Shell cluster inventory and sign-out paths use feature clients; `frontend/src/api.ts` remains a compatibility re-export.
- Verification after this slice: 314 Python tests, 90 Vitest tests, TypeScript, production build, all Ansible syntax checks, strict aggregate boundary checks, isolated smoke, and live `.104` health/login/core API/SSE checks passed. No Elastic workload host was changed.

## Latest Extraction Evidence (2026-08-03, mutation and host-batch slices)

- Cluster-qualified workload mutation routes (`assignments`, workload-change
  apply, resource updates, apply, and detach/purge) now live in
  `app/modules/workloads/http.py`. The route module uses injected models and
  callbacks, so API response shapes, run IDs, validation, and existing test
  patch seams remain compatible.
- The multi-host host-initialization route now lives in
  `app/modules/hosts/http.py`; host selection, conflict checks, and run fan-out
  are injected through host/orchestration callbacks.
- Verification after both slices: 314 Python tests, strict route/import/table
  ownership checks, and compile checks passed. No `.104` deployment or Elastic
  workload host was changed in this local slice.

## Current Deployment Evidence (2026-08-03)

- `.104` now runs `localhost/elastic-control-plane:refactor-final12-20260803` with digest `sha256:706173dadad7b6f84a8dcecd3d1676179b8fea1a4989eea46c162a29da896116`.
- Backup `/root/control.db.refactor-final12-20260803.bak` is non-empty (2,379,776 bytes) and was created before replacement; existing environment and persistent mounts were preserved.
- In-image Python suite passed with 314 tests. Isolated health/login/core API/SPA smoke and dashboard SSE passed; live health, authentication, cluster/run APIs, SPA delivery, and SSE passed.
- No Elastic workload nodes or destructive testing hosts were modified.

## Current Deployment Evidence (2026-08-03, final13)

- `.104` runs `localhost/elastic-control-plane:refactor-final13-20260803` with digest `sha256:163d8b7f27d7c5d811f37a873e9acb3f3636edfeb38dab606037cefd99edaabb`.
- `/root/control.db.refactor-final13-20260803.bak` is non-empty (2,379,776 bytes) and was created before replacement; existing mounts and environment were preserved.
- The image passed 314 in-container Python tests. Live health, authentication, cluster/run APIs, SPA delivery, and dashboard SSE were verified.
- No Elastic workload nodes or destructive testing hosts were modified.

## Latest Host Slice (2026-08-03)

- Host inventory reads and creation (`GET/POST /api/nodes`) now use the owned
  `app/modules/hosts/http.py` router and `HostRepository` callbacks. Pydantic
  validation errors remain JSON-safe and preserve the prior 422 contract.
- Final `.104` candidate is `localhost/elastic-control-plane:refactor-final14-20260803`
  (digest `sha256:8160d82b580a0e73fb04376646a8dba3025885d6cc2d8b4369d76302aea191b7`)
  with 314 in-image Python tests and a verified backup at
  `/root/control.db.refactor-final14-20260803.bak`.

## Latest Guarded Deployment (2026-08-03, final15)

- Source-only sync and remote build completed on `.104` as
  `localhost/elastic-control-plane:refactor-final15-20260803` (digest
  `sha256:af84147235d448ac8214fb7f555814c6716b5e84670b74e0a4d1926d82edd249`).
- The first live start exposed an SELinux bind-label mismatch. The rollout was
  held, startup logs were inspected, and the controller was restarted with
  explicit `:Z` labels on the existing mounts. No database or Elastic workload
  host data was changed.
- The corrected live container is healthy with zero restarts and preserves the
  non-empty backup `/root/control.db.refactor-final15-20260804015229.bak`
  (2,379,776 bytes). Isolated candidate login/core API/SPA smoke and 314
  in-image Python tests passed. Live health and container stability passed;
  authenticated live API verification remains dependent on the operator's
  existing credential/session and no credential was recorded. The backup
  filename reflects `.104` reporting August 4 while this release ledger is
  dated August 3.

## Latest Completion Slice (2026-08-03)

- The remaining cluster lifecycle/provider routes now live in
  `app/modules/clusters/http.py`. `app.main` supplies late-bound compatibility
  callbacks, so existing DTOs, response codes, maintenance/provider guards,
  audit behavior, and test patch seams remain unchanged.
- The host/controller-identity mutation set now lives in
  `app/modules/hosts/http.py`; host repositories own enrollment, key, zone,
  and deletion persistence helpers. Strict backend route/import/table checks
  remain green.
- Frontend feature clients and DTOs now cover clusters, hosts, workloads,
  dashboard, versions, advanced, maintenance, runs, and auth. Root
  `frontend/src/api.ts` and `frontend/src/types.ts` are compatibility facades;
  the frontend boundary checker rejects new imports from those facades.
- Verification: 314 Python tests, 90 Vitest tests, TypeScript, production
  build, compilation, all bundled Ansible syntax checks, and strict aggregate
  boundary checks passed. No Elastic workload nodes were modified.

## Latest Guarded Deployment (2026-08-03, final16)

- Source-only sync and remote build completed on `.104` as
  `localhost/elastic-control-plane:refactor-final16-20260803`; image ID
  `47c96cdaf481f5592e2d6949836992ebce03eaf5d1b3b90c5dce76560256a9f2`.
- Isolated candidate smoke passed health, login, authenticated clusters API,
  SPA routes, and all 314 in-image Python tests. The first smoke invocation
  was rejected as a harness error (the temporary curl config did not quote its
  bearer header); it was corrected and rerun successfully.
- The live controller was replaced after a non-empty backup
  `/root/control.db.refactor-final16-20260804024208.bak` (2,379,776 bytes).
  Existing `.env`, persistent mounts, SSH secret mounts, restart policy, and
  `no-new-privileges` were preserved. Live health, SPA delivery, expected
  unauthenticated API protection, image identity, and zero restarts passed.
- Authenticated live API verification was not forced because the persistent
  operator credential is request-only and unavailable to this run. No
  password was reset or recorded. No Elastic workload or testing host was
  touched.

## Console Compatibility Retirement Slice (2026-08-03)

- `app.console` is now a module alias facade. The legacy runtime implementation
  moved intact to `app.console_runtime`, preserving existing caller and test
  monkeypatch behavior while removing implementation from the public console
  import surface.
- The strict table checker explicitly records `app.console_runtime` as a
  compatibility owner until its remaining telemetry, SSH, Podman, certificate,
  and sensitive-metadata persistence paths are moved into their owning module
  repositories. This preserves strict enforcement for every non-compatibility
  module without disguising the outstanding runtime extraction work.
- Focused console/maintenance tests and the full 314-test suite passed, along
  with compile, route, backend-import, frontend, and table ownership gates.

## Latest Guarded Deployment (2026-08-03, final17)

- `.104` runs `localhost/elastic-control-plane:refactor-final17-20260803`
  (image ID `4c4838b68a9e17f8974ce6dac91f3b297df1c64a222ed511575c7ff20031beb8`).
- The isolated candidate passed health, login, authenticated cluster API, SPA
  route delivery, and 314 in-image Python tests. Live health, SPA delivery,
  expected unauthenticated API protection, and zero restarts passed after the
  guarded replacement.
- Backup `/root/control.db.refactor-final17-20260804030114.bak` is non-empty
  (2,379,776 bytes). The `.104` host clock reports August 4 even though this
  release record is dated August 3; the path is recorded exactly as produced.
  Existing environment, mounts, security options, and persistent state were
  preserved. No workload or testing host was touched.
