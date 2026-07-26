# Nexa Backup & Restore System v1.0 — Phase 1 Foundation

## Purpose and authority

`apps.backups` is the bounded context for tenant backup and restore control-plane
data. Phase 1 establishes durable metadata, authorization, product-scope
resolution, explicit state machines, a fail-closed component registry, operation
locking, compatibility metadata, audit evidence, and read-only UI foundations.

The authoritative Phase 1 specification is
`C:\Users\Admin\Desktop\Nexa Backup& Restore.md`. Where older repository
documentation conflicts with it, this document records the conflict rather than
silently inheriting the older behavior.

Phase 1 does **not** create backup artifacts or mutate tenant operational data.
A metadata record in `QUEUED` state is not evidence that a backup exists, passed
integrity verification, or can be restored.

## Architectural boundary

The foundation is designed for three tenant product profiles:

- POS only
- WMS only
- POS + WMS

Future backup packages are logical, tenant-scoped packages. They are not copies,
slices, or downloadable extracts of the shared SQLite database. Full-platform
disaster recovery is a separate future operator capability and must not be
confused with tenant backup history.

Backup execution must eventually run through dedicated asynchronous workers. It
must never run synchronously inside a Gunicorn request. Phase 1 may define queue
names and safe task-module boundaries, but it intentionally contains no task
that creates or restores a backup.

## Model overview

### `BackupRecord`

The durable identity and lifecycle record for a tenant backup request. It uses a
non-guessable public UUID and records the immutable tenant UUID snapshot, scope,
included products/components, trigger, lifecycle and integrity states,
retention/protection flags, compatibility/version metadata, storage and
encryption placeholders, counts and sizes, lifecycle timestamps, actor
snapshots, sanitized failure information, and an idempotency key.

Deletion of a future artifact must be represented as a tombstone: metadata
remains, while lifecycle/deletion fields record that the artifact is no longer
available. Business Owners have no delete capability.

### `BackupComponent`

Stores per-component metadata beneath a `BackupRecord`: stable component key,
product owner, component version, counts and sizes, hash placeholder, and
verification result. A database constraint permits a component key only once
per backup.

### `BackupSchedule`

Stores the single daily schedule foundation for a business: enabled state,
tenant-timezone snapshot, local execution time, next/last claim, last successful
and failed backup references, and scope. V1 defaults to `ALL_ENABLED`.

Only one schedule may exist per business in Phase 1. No Beat entry, dispatcher,
catch-up logic, or scheduled backup execution is active.

### `RestoreOperation`

Records a restore request without executing it. It relates a tenant to a source
backup and, in a later phase, a mandatory fresh verified safety backup. It also
captures requested scope, explicit restore state, actor/reason snapshots,
dependency and compatibility results, rollback metadata, timestamps, and
sanitized failure information.

Source and safety evidence uses protective deletion behavior where audit
integrity requires it. Phase 1 never deletes tenant rows, applies package data,
creates a safety artifact, or rolls back operational data.

### `TenantOperationLock`

Provides a database-backed exclusive lease for tenant operations. A lease
records operation kind/reference, an unguessable token, optional worker/task
identity, acquisition/heartbeat/expiry/release timestamps, and active state.

Acquisition must be transactional and enforce uniqueness at the database level.
It must not depend only on `select_for_update()`, because SQLite does not provide
the row-locking semantics required for that design. Expired leases may be
reclaimed atomically; a caller that does not hold the current token must not
release another caller's lease.

### `BackupActivity`

The append-only, tenant-scoped evidence stream for detailed backup and restore
events. It captures public UUID, backup/restore references, event and severity,
actor and platform/support snapshots, reason, a sanitized message, structured
non-secret metadata, request IP, user agent, and creation time.

Application code may append activity. It must not update or delete existing
activity rows. These rows are control-plane evidence and are permanently outside
future mutable restore scope.

### `DownloadGrant`

The Phase 1 schema includes authorization metadata only: public UUID, backup,
tenant, recipient, token hash, expiry/use/revocation timestamps, single-use
state, request IP, and creation time. It does not generate a token response,
stream an object, or create a presigned storage URL.

## Permission model

Tenant permissions are platform-layer permission codes:

- `backups.view`
- `backups.create`
- `backups.download`
- `backups.schedule`
- `backups.pin`
- `backups.restore`

There is deliberately no tenant `backups.delete` permission.

The Business Owner receives all six approved permissions through the existing
owner-role convention. Existing and newly provisioned non-owner roles receive
none by default, including Business Administrator, Branch Manager, and WMS-only
roles. Granting a permission later must still be combined with membership,
tenant, entitlement, object-scope, and subscription checks; a permission string
alone never selects another tenant's data.

Platform Admin access is separate from tenant-role permissions. Platform staff
may inspect tenant-filtered backup metadata through platform-only views, but
Phase 1 does not grant an emergency restore, artifact download, or cleanup/delete
path. A Platform Admin without tenant membership does not acquire ordinary
tenant access.

## Scope and product entitlement

The existing subscription plan is the single source of product entitlement:
`feature_sales` enables POS and `feature_wms` enables WMS. The backup app does
not introduce a second entitlement store.

| Tenant entitlement | Explicit scopes | `ALL_ENABLED` resolves to |
| --- | --- | --- |
| POS only | `POS`, `ALL_ENABLED` | POS |
| WMS only | `WMS`, `ALL_ENABLED` | WMS |
| POS + WMS | `POS`, `WMS`, `ALL_ENABLED` | POS + WMS |
| Neither product | none | rejected |

An explicit disabled scope is rejected by forms, selectors, service entry
points, URLs, and platform/owner presentation. `ALL_ENABLED` is a request to
resolve the products currently enabled for that tenant; it is not a static
synonym for POS + WMS.

Tenant object selection must always include the active business. A public UUID
does not replace tenant filtering. A cross-tenant UUID should resolve as not
found and must not disclose whether the object exists.

## Explicit component registry

The registry is declarative, versioned, and fail closed. Every definition
declares:

- a stable component key and component version;
- product owner: `SHARED`, `POS`, or `WMS`;
- explicitly classified model labels;
- required component keys;
- export and import ordering metadata;
- restore behavior: `REPLACEABLE`, `REFERENCE_ONLY`, `DEPENDENCY_ONLY`, or
  `NON_RESTORABLE`;
- media-field metadata;
- validator hooks; and
- eligible backup scopes.

Phase 1 definitions classify metadata only. They are not exporters, importers,
serializers, or permission to inspect model rows.

Resolution rules are strict:

- unknown components are errors, never ignored;
- unknown tenant-related models are not implicitly included;
- POS resolution excludes WMS components;
- WMS resolution excludes POS components;
- shared components are included only when explicitly eligible/required;
- `ALL_ENABLED` requires the caller's resolved enabled products; and
- dependency errors and cycles fail closed.

Automatic discovery of all `TenantModel` subclasses is forbidden as an
inclusion mechanism. A completeness check may inspect the model graph only to
report unclassified tenant models; inclusion still requires an explicit
definition.

Control-plane and evidence models—including backup records, restore records,
locks, download grants, and backup/audit activity—must be explicitly
non-restorable and must never be part of a future mutable package.

## State machines

### Backup lifecycle

The declared states are:

`QUEUED`, `PREPARING`, `SNAPSHOTTING`, `PACKAGING`, `UPLOADING`, `VERIFYING`,
`SUCCEEDED`, `FAILED`, `CANCELLED`, `DELETION_PENDING`, and `DELETED`.

Only an allowlisted transition graph is legal. Terminal states do not
arbitrarily return to active execution states. Artifact deletion is represented
by the `DELETION_PENDING` to `DELETED` path; it does not erase the
`BackupRecord`.

### Integrity lifecycle

The declared states are:

`NOT_CHECKED`, `VERIFYING`, `VERIFIED`, `FAILED`, and `CORRUPTED`.

Lifecycle success and integrity success are separate. Only a future backup that
is both lifecycle `SUCCEEDED` and integrity `VERIFIED` may be treated as
restore-ready or retention-eligible.

### Restore lifecycle

The declared states are:

`QUEUED`, `AUTHORIZING`, `LOCKING`, `SAFETY_BACKUP`, `VALIDATING`, `RESTORING`,
`VERIFYING`, `SUCCEEDED`, `FAILED`, `ROLLING_BACK`, `ROLLED_BACK`, and
`INDETERMINATE`.

The state graph preserves evidence of failure and rollback. A future restore
must not pass `SAFETY_BACKUP` until a fresh safety backup for that operation has
deep verification success.

State mutation is performed through validation/services rather than arbitrary
field assignment in application code. Invalid transitions are rejected before
persistence.

## Idempotency and operation concurrency

Idempotency keys identify one logical request in a tenant context. Repeating the
same authorized request with the same key returns the existing metadata rather
than creating another operation. Keys must not deduplicate across tenants.

The Phase 1 lock is exclusive per tenant, even when requested scopes differ.
This conservative rule is required because POS and WMS depend on shared tenant
records. SQLite may serialize writers across the whole database, but the
application lock still prevents two logical tenant operations from racing.

## Version and compatibility metadata

The foundation has central sources for:

- backup format version;
- application version;
- minimum restore version;
- per-component version; and
- schema/migration fingerprint.

The schema fingerprint is derived deterministically from the applied Django
migration graph/state. It is not a hand-maintained integer and must not contain
credentials or environment secrets. Phase 2 will embed the values in a
versioned package manifest and define the supported upgrade/downgrade
compatibility matrix.

Public UUIDs, timezone-aware ISO timestamps, canonical decimal strings, and
explicit JSON schemas are the intended database-neutral interchange forms.
Package semantics must not embed SQLite SQL, row identifiers, or PRAGMA state,
so PostgreSQL adoption does not require a new tenant backup format.

## Audit and data handling

Summary events may also be written to the existing `AuditLog`; detailed evidence
belongs in `BackupActivity`. Actor snapshots must remain understandable after a
user changes their name or email. Structured metadata and error summaries must
be sanitized and bounded.

Never store raw download tokens, passwords, password hashes, encryption keys,
data-key plaintext, object-storage credentials, presigned URLs, secret settings,
stack traces, or unrestricted exception text in backup metadata or activity.

Storage object keys and encryption fields in Phase 1 are placeholders controlled
by trusted services. They are not accepted from tenant URL or form input.

## UI behavior in Phase 1

Owner-facing pages provide a dashboard, health/last-backup cards, storage and
schedule placeholders, an entitlement-filtered create form, history, detail,
activity timeline, and restore review placeholder. The interface must use the
same scope service as backend mutations.

Because the engine is unavailable, the UI must say so plainly. It must not show
a metadata request as a completed or verified backup. There is no owner delete
route and no restore-execution, download, storage, or scheduler action.

Platform pages are read-only metadata and activity/status views. They do not
implicitly grant tenant membership and expose no destructive operation.

## Celery boundary

`apps.backups.tasks` reserves the future queue names `nexa.backups`,
`nexa.restores`, and `nexa.backup_verification`, but registers no operational
Celery tasks. `ENGINE_ENABLED` remains false.

`assert_safe_async_execution_configuration()` rejects a missing broker and
rejects `CELERY_TASK_ALWAYS_EAGER`. The registered security system check is
non-blocking while the Phase 1 engine is disabled; if a future deployment turns
the engine on, it fails closed with `backups.E010`/`backups.E011` until a
dedicated non-eager worker configuration exists. This permits the repository's
current local eager convention without creating a path that could execute
backup work in Gunicorn.

## SQLite constraints

SQLite is the approved current production database and PostgreSQL migration is
not a Phase 1 or v1 blocker. The design nevertheless accounts for:

- `select_for_update()` not being sufficient for tenant lock acquisition;
- database-wide write contention during future large restores;
- the need for explicit disk-headroom and busy-lock handling;
- consistent future snapshots through SQLite's online backup API, never a
  naive live-file copy;
- restrictive handling of a temporary full-database snapshot, because it
  contains all tenants; and
- dedicated future worker execution, maintenance state, and production-size
  load testing.

None of those operational mechanisms is implemented in Phase 1.

## Deliberately excluded from Phase 1

The following are not implemented:

- SQLite snapshots or logical export;
- package/manifest serialization;
- compression or encryption;
- DigitalOcean Spaces, S3, or other object storage;
- upload, download, or presigned URLs;
- media discovery/capture;
- deep integrity verification or scratch restore;
- operational Celery tasks, Beat, or a scheduler dispatcher;
- scheduled backup creation;
- retention selection or artifact deletion;
- restore, safety-backup, dependency mutation, rollback, or maintenance mode;
- notifications, restore estimates, or artifact preview;
- cross-region replication; and
- any production deployment.

The locked future retention rule is documented but inactive: retain the latest
five eligible backups where eligibility means scheduled daily, full
`ALL_ENABLED`, lifecycle `SUCCEEDED`, and integrity `VERIFIED`. Manual, pinned,
and pre-restore safety backups never qualify for automatic deletion.

## Phase 2 integration points

Phase 2 must build on these contracts rather than bypass them:

1. dedicated non-eager Celery queues and production configuration guards;
2. consistent database snapshot and explicitly scoped logical exporters;
3. canonical manifest/package serialization and envelope encryption;
4. private off-server object storage and two-phase object finalization;
5. referenced-media capture;
6. deep verification and scratch import;
7. verified-success-only scheduling and retention;
8. short-lived, audited secure downloads;
9. maintenance mode, dependency dry run, fresh verified safety backup, restore,
   post-restore checks, and rollback; and
10. full-platform disaster-recovery procedures separate from tenant packages.

## Known documentation and commercial conflicts

These older assumptions remain in the repository and require a later,
coordinated documentation update:

- `README.md`, `DEPLOYMENT.md`, and `SECURITY.md` describe PostgreSQL as the
  production database, while the approved current production context is SQLite.
- `SECURITY.md` and `DEPLOYMENT.md` prohibit in-application restore, while the
  approved product direction includes a future guarded Business Owner restore
  flow.
- `DEPLOYMENT.md` documents 7 daily, 4 weekly, and 12 monthly platform backups,
  while tenant-backup v1 locks retention to the latest five eligible scheduled
  daily full backups, with protected exceptions.
- `feature_daily_backup`, `feature_weekly_backup`, and
  `feature_priority_restore` exist on plans but are intentionally inactive
  future feature flags. Product scope continues to derive from POS/WMS
  entitlements, not those flags.

Those conflicts do not authorize rewriting all deployment/security material in
Phase 1. They must be reconciled before the corresponding production capability
is enabled or commercially represented.

## Security assumptions and release boundary

- Every selector and mutation is tenant-scoped and fail closed.
- Product entitlement is re-evaluated at service execution, not trusted from
  submitted form choices.
- Public UUIDs reduce guessability but never replace authorization.
- Business Owner permission does not imply artifact existence or restore
  readiness.
- Platform staff access is separately gated and audited.
- Activity and backup history are append-only/tombstoned evidence.
- The foundation is database-neutral, but future SQLite operations require
  explicit operational safeguards.
- No Phase 1 screen or service is a production backup guarantee.

Phase 1 is ready for review only after its focused tests, related entitlement
and isolation regressions, and `manage.py check` pass. A full test-suite run is
recommended before any commit or later deployment.
