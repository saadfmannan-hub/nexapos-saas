# Nexa Backup & Restore — Phase 2A

## Purpose

Phase 2A establishes the safe orchestration boundaries for future backup work.
It can validate a tenant-scoped request and produce an immutable, deterministic
execution plan with an in-memory manifest foundation.

Phase 2A does **not** produce a recoverable backup. It does not read or copy the
live SQLite database, export tenant rows or media, build an archive, encrypt
data, upload an object, verify an artifact, or restore operational data.

## Engine package

The engine foundation is under `apps/backups/engine/`:

- `availability.py` — central setting and code-capability guard.
- `checks.py` — staging-root system check.
- `context.py` — immutable execution and actor identity.
- `contracts.py` — typed future provider interfaces and result objects.
- `events.py` — stable Phase 2A activity event names.
- `exceptions.py` — sanitized engine error model.
- `exporters.py` — component exporter integration point; no implementation.
- `manifest.py` — versioned in-memory manifest types and builder.
- `metadata.py` — authoritative context and manifest metadata builder.
- `orchestration.py` — non-operational execution-plan preparation.
- `packaging.py` — deliberately disabled package builder.
- `pipeline.py` — stages, reports, component dependency ordering, and plan types.
- `workspace.py` — private empty-workspace lifecycle and opaque references.

The package adds no tenant-data model and requires no migration.

## Provider contracts

The following abstract interfaces define later integration boundaries:

- `SnapshotProvider` accepts an explicit execution context and returns an
  opaque `SnapshotReference`. It cannot return an arbitrary filesystem path.
- `ComponentExporter` accepts an explicit context, registered component, and
  snapshot reference. It returns typed counts and an opaque export reference.
- `ManifestBuilder` builds an in-memory `BackupManifest`.
- `PackageBuilder` accepts typed manifest/export inputs. The only Phase 2A
  concrete package builder is disabled and raises `BackupEngineDisabled`.
- `VerificationProvider` returns a typed verification result and sanitized
  issues.
- `StorageProvider` defines store, retrieve, and delete operations using
  private opaque object references.

There are no SQLite, logical export, package, verification, or storage
providers in this phase.

## Execution context

`BackupExecutionContext` is a frozen dataclass containing:

- backup public UUID;
- business database ID and public UUID;
- requested scope and currently resolved products;
- trigger type;
- sanitized actor identity snapshot;
- application, backup-format, minimum-restore, and migration fingerprint
  versions;
- idempotency key;
- deterministic operation correlation UUID;
- an optional opaque workspace reference.

The context contains no model instances, credentials, keys, session tokens,
storage secrets, or filesystem paths. Tenant identity is checked against both
the record relation and its immutable tenant UUID snapshot. A workspace can be
associated only by returning a new frozen context.

## Pipeline stages

The future ordered stage vocabulary is:

1. `AUTHORIZE`
2. `RESOLVE_SCOPE`
3. `ACQUIRE_LOCK`
4. `PREPARE_WORKSPACE`
5. `PREPARE_SNAPSHOT`
6. `RESOLVE_COMPONENTS`
7. `EXPORT_COMPONENTS`
8. `BUILD_MANIFEST`
9. `BUILD_PACKAGE`
10. `VERIFY_ARTIFACT`
11. `FINALIZE_METADATA`
12. `CLEANUP`
13. `COMPLETE`

Phase 2A reports authorization, scope resolution, lock availability,
component resolution, and in-memory manifest metadata as validated. Snapshot
preparation is explicitly blocked. Other operational stages remain
`NOT_STARTED`; `COMPLETE` is never reported as reached.

## Execution-plan behavior

`prepare_backup_execution()`:

1. rejects a supplied record belonging to another tenant;
2. re-fetches through `BackupRecord.objects.for_business()`;
3. re-resolves current POS/WMS entitlement with the Phase 1 scope service;
4. reuses the existing backup permission service;
5. requires the record to remain `QUEUED`;
6. rejects a conflicting active tenant operation lock;
7. builds the immutable context from authoritative server-side values;
8. resolves registered components and dependency closure;
9. builds in-memory manifest placeholders;
10. returns a plan with `real_execution_available=False`;
11. appends sanitized, tenant-scoped planning evidence.

It does not acquire and retain a planning lock, create a workspace, dispatch a
task, or transition the record to an operational or successful state.

Repeated preparation from unchanged authoritative inputs produces the same
context, component order, manifest, and plan. Activity evidence is append-only
and therefore grows on each request.

## Entitlement and registry integration

Subscription truth remains in the existing subscription access service.
Component truth remains in the Phase 1 fail-closed `COMPONENT_REGISTRY`.

- POS scope resolves shared and POS components only.
- WMS scope resolves shared and WMS components only.
- `ALL_ENABLED` resolves the business's current entitled products in stable
  POS-then-WMS order.
- Explicit unknown component requests fail closed.
- Missing dependencies and circular graphs raise distinct sanitized errors.
- Export and import orders are deterministic topological orders. Declared
  numeric order and component key provide stable tie breaks.
- Reference-only, dependency-only, replaceable, and non-restorable
  classifications are retained rather than converted to implicit behavior.

## Manifest foundation

`BackupManifest` is a frozen, versioned in-memory object. Its ordered mapping
contains:

- format, application, minimum restore, and schema/migration versions;
- backup and tenant public UUIDs;
- scope and products;
- ordered component keys, component versions, ownership, restore behavior, and
  dependency metadata;
- trigger and deterministic UTC timestamp;
- engine-neutral compatibility metadata;
- `None` record, media, component hash, and whole-artifact hash placeholders;
- `NOT_VERIFIED` at manifest and component level.

The builder does not serialize JSON, write a file, hash content, or claim
verification. The structure includes no internal path or secret field.

## Workspace security

`BACKUP_STAGING_ROOT` controls the private staging root. The default is
`BASE_DIR/.backup-staging`, outside `MEDIA_ROOT`, `STATIC_ROOT`, and their URL
routes.

`BackupWorkspaceManager`:

- validates an absolute, non-public root;
- creates `ws-<random UUID hex>` names only;
- uses opaque UUID references in contexts;
- applies mode `0700` where supported;
- rejects absolute child paths, separators, `.` and `..`;
- resolves and checks containment before use or cleanup;
- reconstructs cleanup targets against its own configured root;
- rejects symlink cleanup targets;
- makes cleanup idempotent.

Phase 2A orchestration does not create a workspace. Tests create and remove
empty workspaces only. No tenant name, business name, email, or user input is
used in a directory name.

The Django system check reports `backups.E020` when the configured root is
relative or inside a public media/static root.

## Engine-disabled guard

Real execution requires both:

1. `BACKUP_EXECUTION_ENGINE_ENABLED=True`; and
2. the code-owned `OPERATIONAL_PROVIDER_STACK_READY=True`.

The setting defaults to false. The provider-stack capability is hard-coded
false in Phase 2A, so a deployment setting alone cannot activate incomplete
code. The Phase 1 `BACKUP_ENGINE_ENABLED` alias remains supported but cannot
bypass the provider guard.

When enablement is requested with a valid asynchronous configuration, the
Django system check emits `backups.E012` because providers are missing.
Existing broker and eager-mode checks continue to emit `backups.E010` and
`backups.E011`.

Owner views call the central capability check. There is no operational POST
route.

## Celery integration

No Celery task is registered, dispatched, retried, or scheduled. The plain
`execute_backup()` function is a disabled future integration boundary and has
no `.delay()` or `.apply_async()`.

If invoked, it:

- tenant-scopes both public identifiers;
- calls the central engine guard;
- transitions a non-terminal record to `FAILED` when possible;
- records sanitized `ENGINE_DISABLED` and `EXECUTION_BLOCKED` evidence;
- leaves integrity unverified and all artifact/storage/encryption metadata
  blank;
- raises `BackupEngineDisabled`.

It never falls back to eager execution and never marks a record successful.

## Activity evidence

Phase 2A defines:

- `backup.execution_plan_requested`
- `backup.execution_plan_created`
- `backup.execution_blocked`
- `backup.workspace_created`
- `backup.workspace_cleaned`
- `backup.component_plan_resolved`
- `backup.engine_disabled`

Planning uses the applicable plan, component, engine-disabled, and blocked
events. Workspace event names are reserved for the later internal worker
lifecycle; the current planning service does not fake workspace activity.

All events use the existing append-only `BackupActivity` service, tenant
foreign keys, actor snapshots, and metadata sanitizer. They contain no raw
paths, serialized models, stack traces, or secrets.

## Security assumptions

- Business public UUID and immutable tenant snapshot remain trustworthy
  server-side identifiers.
- Current product entitlement is re-evaluated immediately before planning.
- Permission checks remain authoritative in subscription/access services.
- The explicit component registry is reviewed when tenant models change.
- Workspace root configuration is private and writable only by the
  application/worker account.
- Planning is not evidence that an artifact exists or is recoverable.

## SQLite and PostgreSQL considerations

Production currently uses SQLite, but Phase 2A performs no database snapshot
operation and encodes no SQLite path or file-copy assumption. The migration
fingerprint uses Django's applied migration graph and remains database-engine
neutral. A later `SnapshotProvider` can implement the SQLite online backup API
while a future PostgreSQL provider can satisfy the same opaque-reference
contract.

## Deliberately deferred to Phase 2B and later

- SQLite online snapshots, WAL handling, and snapshot cleanup;
- logical POS/WMS row and media exporters;
- deterministic record serialization;
- package/archive creation and compression;
- content and artifact hashes;
- encryption and key-envelope handling;
- deep verification and scratch restores;
- private object storage;
- operational Celery registration, queues, retries, and scheduling;
- downloads, retention, deletion, restore mutation, rollback, and
  notifications.

No Phase 2A output is a recoverable backup.
