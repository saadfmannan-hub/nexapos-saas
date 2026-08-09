# Nexa Backup & Restore Phase 3D

Phase 3D upgrades the existing Platform Administration metadata pages into a
tenant-wide Backup & Restore control center. It adds operational visibility and
guarded administrative request boundaries without enabling the production
provider stack or restore mutation.

## 1. Platform Admin routes

- `/platform/backups/` — health dashboard, filters, and cross-tenant history.
- `/platform/backups/business/<business_uuid>/` — one tenant's overview.
- `/platform/backups/<backup_uuid>/` — sanitized backup detail.
- `/platform/backups/business/<business_uuid>/manual/` — POST-only manual request.
- `/platform/backups/business/<business_uuid>/<backup_uuid>/preflight/` — GET/POST
  restore preflight.
- `/platform/backups/business/<business_uuid>/<backup_uuid>/restore/` — final
  guarded confirmation.
- `/platform/backups/operations/` — restore-operation and active-lock metadata.
- `/platform/backups/activity/` — sanitized append-only activity.

Every URL uses public UUIDs. Write routes bind both the business and backup and
revalidate that the selected backup belongs to the selected tenant.

## 2. Access control

The routes use the existing Platform Backup capability layer. Authenticated
platform staff can inspect metadata. Manual requests require
`backups.platform_manage_backups`; restore preflight and confirmation require
`backups.platform_approve_restore`. Superusers satisfy operational capabilities.
Tenant owners and ordinary tenant staff receive no Platform Admin access.

## 3. Dashboard KPIs

The dashboard reports businesses with backups, successful backups, failures,
active backups, verified durable byte count, and entitled tenants without a
successful verified backup. It also reports safe capability states for backup
execution, restore mutation, and scheduling.

## 4. Tenant backup overview

The tenant page shows public identity, current product entitlement, last attempt,
last success, latest size and duration, active state, allowed manual scopes,
schedule metadata, retention counts and warnings, protected safety backups, and
restore-preflight availability. Internal business primary keys are never shown.

## 5. Manual backup flow

Manual backup is POST-only. `platform_request_manual_backup()` locks the exact
tenant, rejects conflicts, resolves the scope against current entitlements,
creates a `BackupRecord` through the existing service layer, records the Platform
Admin actor and `platform.manual_backup_requested` evidence, and hands public
identifiers to the dedicated Celery queue. Heavy backup work is never run in the
web request. Current runtime capability is unavailable, so the UI disables the
action and the service refuses to create or enqueue a request.

## 6. Restore preflight flow

The GET page presents the exact tenant and backup. POST requires a reason and
runs the existing Phase 3A coordinator through
`platform_run_restore_preflight()`. It records
`platform.restore_preflight_requested`, returns compatibility and content counts,
and cleans transient plaintext preflight evidence. It does not mutate tenant
business data.

## 7. Restore guard

A ready preflight unlocks the final warning and explicit acknowledgement form,
including typed `RESTORE`. `BACKUP_RESTORE_MUTATION_ENABLED` remains false. A
confirmed attempt records `platform.restore_requested` but does not call restore
mutation or execute work inline. The UI reports that restore execution is not yet
enabled. A dedicated guarded asynchronous worker remains a prerequisite.

## 8. Schedule visibility

Schedules are read-only. The tenant page shows enabled state, timezone, local
execution time, next run, last claim, last successful scheduled backup, and last
failed scheduled backup. No persistence change or schedule-editing migration was
introduced.

## 9. Retention and storage status

The UI states the immutable policy of the latest five successful daily full
backups, current retained count, protected safety count, and evidence-backed
retention warnings. Storage is reduced to a safe provider label, verified state,
and byte count. Raw paths, object keys, hashes, and key-management data are not
rendered. Retention remains engine-owned.

## 10. Activity

The activity page covers manual, scheduled, success/failure, preflight, safety,
restore, and retention evidence already written by the backup subsystem. It can
filter by tenant name/public UUID, event, severity, and date. Structured metadata,
request telemetry, and internal storage identifiers are withheld.

## 11. Owner and Platform UI separation

Platform templates and routes are independent from the owner UI. Shared status
partials and formatting filters are presentation-only. Owner routes remain
tenant-scoped and never render Platform Admin controls.

## 12. Support-session behavior

During owner impersonation, owner pages continue using the impersonated owner and
tenant membership. Backup Platform Admin routes authenticate separately against
`request.support_admin`, set that account as the platform actor, and keep all
administrative controls inside `/platform/`.

## 13. No destructive artifact controls

Phase 3D adds no public artifact URL and no manual artifact removal action.
Retention deletion remains private and automatic. Generic retry is also omitted
because no safe record retry boundary exists.

## 14. No production activation

`OPERATIONAL_PROVIDER_STACK_READY` remains false and
`BACKUP_RESTORE_MUTATION_ENABLED` remains false. Phase 3D does not change runtime
activation flags, production credentials, storage providers, worker configuration,
or persistent tenant data.

## 15. Deployment

No deployment is part of Phase 3D. The phase introduces no model change and no
migration.
