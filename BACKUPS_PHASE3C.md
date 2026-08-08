# Backup & Restore Phase 3C

Phase 3C exposes a tenant-owner interface over the existing backup metadata,
manual backup task, and restore-preflight engine. It does not activate the
production provider stack or connect restore mutation.

## Owner UI routes

- `GET /backups/` — landing page and recent recovery points.
- `GET /backups/history/` — paginated, filterable tenant history.
- `POST /backups/manual/` — guarded manual backup request.
- `GET /backups/<backup-public-uuid>/` — safe backup details.
- `GET|POST /backups/<backup-public-uuid>/preflight/` — restore selection and
  Phase 3A readiness check. GET is informational; only POST runs the check.
- `GET|POST /backups/<backup-public-uuid>/restore/` — readiness result and
  strong final confirmation. No restore mutation is connected in Phase 3C.
- `GET /backups/activity/` — sanitized tenant activity evidence.

Every object route resolves a public UUID through the active business's
entitlement-filtered queryset. A UUID never grants access by itself.

## Permissions and navigation

The UI reuses the existing business permission registry:

- `backups.view` for navigation, landing, history, detail, and activity.
- `backups.create` for manual requests.
- `backups.restore` for preflight and final confirmation.

The navigation link is shown only when the active membership has
`backups.view` and the tenant has an enabled POS or WMS product. Owner routes
do not expose or reuse Platform Admin controls.

## Landing-page data

Tenant-scoped selectors provide the latest attempt, latest verified success,
recent history, active-backup state, and read-only schedule metadata. The
dashboard shows status and time, latest successful time, human-readable size
and duration, and automatic schedule/next-run status when persisted.

Missing schedules, missing backups, disabled runtime configuration, and an
active backup all render safe product-language states without inspecting or
constructing providers.

## History and detail

History is ordered newest first, paginated, and restricted to currently
entitled product scopes. Manual, Automatic, and Safety Backup types are
distinct. Restore availability requires successful lifecycle status,
verification, durable storage evidence, no deletion tombstone, and no known
compatibility refusal.

Detail pages expose only safe metadata: date, status, type, scope, size,
duration, verification indicators, summary counts, and a sanitized failure
summary. Provider names, object keys, hashes, paths, failure codes, and
encryption metadata are never rendered.

## Manual backup flow and async boundary

`POST /backups/manual/` validates membership, permission, tenant, and an
entitlement-derived scope. The owner service rechecks runtime capability,
serializes rapid requests with the tenant row, refuses an already-active
backup, creates a queued `BackupRecord`, emits safe activity, and enqueues
`apps.backups.tasks.execute_backup` on `nexa.backups` using only the business
and backup public UUIDs.

No snapshot, export, package, upload, verification, or other heavy backup work
runs in the HTTP request. Disabled engine/provider/broker/eager/routing safety
states prevent enqueue. Delivery failure is recorded as a safe failed request.

## Restore preflight

Only an eligible tenant-scoped backup can enter preflight. The owner supplies
a restore reason, after which the service creates tenant restore-request
metadata and runs Phase 3A only. The result shown to the owner contains
compatibility, component count, record count, media count, and sanitized
messages.

Phase 3A readiness evidence is deliberately process-local. Because Phase 3C
does not have a restart-safe restore task handoff, the UI copies only a safe
result summary to the session and immediately cleans the transient plaintext
preflight workspace. No tenant business records or live media are mutated.

## Final restore confirmation

A ready result shows the replacement warning, mandatory fresh safety-backup
policy, expected operational pause, a required acknowledgement checkbox, and
an exact typed `RESTORE` confirmation. The server re-resolves the session's
public references against the active tenant, source backup, and requesting
owner; hidden identifiers are not treated as authorization.

## Restore mutation guard and task state

`BACKUP_RESTORE_MUTATION_ENABLED` remains `False` by default. With the flag
false, the owner sees: “Restore is currently disabled by the system
administrator.”

Phase 3B has no dedicated Celery restore task and Phase 3A evidence is not
restart-persistent. Therefore Phase 3C never invokes the mutation engine in an
HTTP request and does not enqueue a fake restore. Even if the mutation flag is
manually toggled, the UI refuses execution and reports that the dedicated
secure restore worker is not ready. A later phase must add a restart-safe
`nexa.restores` task that revalidates every state and creates the mandatory
safety backup before mutation.

## Tenant isolation

List, detail, preflight, confirmation, restore-operation lookup, schedule, and
activity selectors are business-scoped. Cross-tenant backup UUIDs return 404
and cannot be used to infer existence. Entitlement filtering is repeated at
read and service boundaries.

## Owner and Platform Admin separation

The owner pages contain no global tenant search, retention controls, durable
cleanup, restore approval, or provider operations. Those capabilities remain
under the separate Platform Admin surface for Phase 3D.

## Disabled states

Manual backup controls are disabled for an inactive execution engine,
incomplete provider stack, unsafe async configuration, or an active backup.
Restore controls are absent for failed, unverified, deleted, non-durable, or
known-incompatible records. Preflight failures show sanitized messages and do
not expose exception types or infrastructure configuration.

## Audit activity

Owner actions use the existing append-only evidence infrastructure. Manual
requests emit `backup.manual_requested`; readiness requests emit
`restore.preflight_requested`; persisted restore request metadata continues to
emit the established `restore.requested` event. No path, object key, key
material, or provider context is included.

## Explicit exclusions

There is no manual durable-delete action and no backup download action in the
owner UI. Retention remains responsible for cleanup.

No migration was required: all UI state is derived from existing
`BackupRecord`, `BackupSchedule`, `RestoreOperation`, and activity fields.

No deployment, worker start, Beat start, production backup, production
restore, engine activation, provider activation, or mutation-flag change was
performed in Phase 3C.
