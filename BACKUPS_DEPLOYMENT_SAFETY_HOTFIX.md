# Backup deployment-safety hotfix

## Why deployment was blocked

Django system checks previously treated the local media capture path, local durable
backup root, and restore-preflight provider composition as universal application
startup requirements. Render therefore failed during `manage.py migrate` even though
backup execution and restore mutation were intentionally disabled.

## Code readiness versus operational activation

Backup and restore components can be present in the codebase without being
operationally activated. Structural checks for bounded policies, schemas, registries,
and capability consistency still run at startup. Concrete provider-environment checks
run only when backup execution, restore mutation, or the broader operational provider
stack is explicitly enabled.

With both execution settings disabled and
`OPERATIONAL_PROVIDER_STACK_READY=False`, E025, E028, E030, and E034 do not require
production provider infrastructure merely to start the unrelated POS/WMS application.
`real_execution_available()` and `restore_execution_available()` remain false, and
manual backup and destructive restore actions remain unavailable.

## Strict activation behavior

Explicit backup activation restores strict media, key, durable-storage, runtime, and
worker checks. Explicit restore-mutation activation restores the provider checks needed
for preflight, the mandatory safety backup, and the restore worker. Unsafe activated
configuration remains a Django system-check error; there is no warning downgrade or
local fallback.

Restore preflight is handled separately. Its service boundary performs a non-mutating
provider-composition readiness check before creating a restore operation or activity.
If providers are unavailable, the action returns a safe unavailable result and does not
change business data.

## Render storage policy

This hotfix does not create directories and does not classify Render's ephemeral local
filesystem as durable backup storage. A future operational storage phase must provide
an approved durable provider before backup or restore execution can be activated.

No production backup execution, restore mutation, migration, manual deployment, or
provider credential was introduced by this hotfix.
