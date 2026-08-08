# Backup Engine Phase 2H: Retention and Durable Lifecycle

Phase 2H adds an internal, provider-neutral retention planner and executor for
verified durable backup objects. It is deliberately not connected to HTTP,
admin, Celery, scheduling, signals, management commands, or any automatic
runtime path.

## Locked v1 policy

The code-owned policy is `nexa.daily-full-retention.v1`, version `1.0.0`:

- retain the latest five successful `DAILY_FULL` backups for each tenant;
- delete only older `DAILY_FULL` objects that satisfy every eligibility rule;
- never dynamically lower the keep count, including under storage pressure;
- limit one execution to at most 100 deletes; and
- bound planning and execution to 300 seconds by default.

The settings are
`BACKUP_RETENTION_DAILY_FULL_KEEP_COUNT=5`,
`BACKUP_RETENTION_MAX_DELETE_BATCH=100`, and
`BACKUP_RETENTION_TIMEOUT_SECONDS=300`. Values are strictly typed and bounded.
In particular, a Boolean is not accepted as an integer keep count. The keep
count must be between 1 and 3650.

## Exact eligibility

A candidate counts toward the latest five only when all of the following are
proven from exact internal evidence:

- its tenant, backup UUID, workspace reference, and stored-object result bind
  to one another;
- its identity and stored-object reference are unique in the supplied evidence;
- it is explicitly classified `DAILY_FULL`;
- package verification, encrypted-artifact validation, and durable verification
  succeeded;
- the Phase 2G provider owns the exact stored result and independently validates
  the current bytes and hash;
- the object is stored and verified, with an aware timestamp;
- it is not failed, incomplete, cleanup-incomplete, corrupt, deleting, deleted,
  pinned, protected, or involved in an active operation.

Failed, incomplete, corrupt, unverified, abandoned, and cleanup-incomplete
objects neither count toward five nor become normal retention delete candidates.
They therefore cannot displace an older known-good backup. A newly started or
failed attempt has no effect; a new backup participates only after package,
encryption, durable storage, and independent durable validation all succeed.

Phase 2H uses immutable transient retention descriptors. It does not alter
`BackupRecord`, claim commercial success, or require a migration. Operational
orchestration may persist the classification and exact evidence in a later
phase.

## Deterministic planning

`RetentionEngine.build_retention_plan()` is non-mutating. Eligible daily-full
objects are ordered by:

1. aware stored timestamp, converted to UTC, descending; and
2. backup public UUID, descending, as the deterministic tie-breaker.

Database primary keys, filesystem timestamps, directory enumeration, and object
filenames are not retention ordering inputs. Canonical candidate metadata is
hashed into an evidence fingerprint and deterministic plan UUID. Repeating the
same request in an engine returns the same immutable plan. A controlled clock is
injectable for deterministic tests.

The plan contains only tenant and backup UUIDs, counts, policy identifiers,
timestamps, outcome codes, and a canonical evidence digest. It contains no
durable path, raw object key, tenant name, ciphertext, secret, or raw error.

If more candidates exist than the configured batch limit, only the oldest
eligible candidates are included in that batch. The five newest are never
eligible for deletion.

## Optimistic execution and exact deletion

Planning never deletes. `RetentionEngine.execute_retention_plan()` accepts only
a plan originally issued by that exact engine. Immediately before each delete,
it recomputes the current keep and delete sets and requires:

- the exact current tenant, backup UUID, context, retention class, stored result,
  and opaque reference from the plan evidence;
- no new pin, protection, failure, incompleteness, corruption, deletion state,
  cleanup failure, or active operation;
- that the object remains outside the current latest-five set;
- exact provider ownership; and
- successful Phase 2G byte/hash/encryption validation.

Deletion then uses only the Phase 2G tenant-scoped opaque-reference API. No raw
path is accepted, and no recursive durable-root deletion exists. Completion is
recorded only after the provider independently confirms both its exact deletion
tombstone and absence of the object directory. Replacement, alias, link,
mutation, missing-object, or forged-evidence ambiguity fails closed.

Retained objects are never passed to the delete API. Unknown/orphan files and
objects without exact provider-held evidence are never enumerated or touched.
Orphan reaping and failed-artifact garbage collection are separate future
responsibilities. Corrupt objects remain available for future quarantine or
repair investigation and are not silently erased by daily retention.

## Partial failure, retry, and audit evidence

The executor stops destructive work after the first ambiguous delete failure.
If A was confirmed deleted but B fails, A remains recorded, C is not attempted,
and the result is `PARTIALLY_COMPLETED`. A failure before any confirmed deletion
is `FAILED_SAFE`. No compensating restoration is attempted.

Progress is retained in-memory for the exact plan. A retry recognizes exact
already-deleted candidates, does not delete them twice, revalidates all remaining
candidates, and cannot expand beyond the original plan's delete set. A completed
or no-action execution is idempotently returned without another provider call.

Immutable audit-ready evidence uses these typed events:

- `RETENTION_PLAN_CREATED`
- `RETENTION_OBJECT_DELETE_STARTED`
- `RETENTION_OBJECT_DELETED`
- `RETENTION_OBJECT_DELETE_FAILED`
- `RETENTION_COMPLETED`
- `RETENTION_PARTIAL`

The evidence is suitable for Phase 2I to publish later and includes only safe
UUIDs, UTC timestamps, and fixed outcome codes.

## Protected classes and future policy

`BackupRetentionClass` defines `DAILY_FULL`, `MANUAL`, `WEEKLY`, `MONTHLY`, and
`PINNED`. Only `DAILY_FULL` is pruned in v1. Manual, weekly, monthly, pinned, and
otherwise protected candidates are classified and returned as protected but
never deleted. Weekly/monthly schedule logic and long-retention rules remain
deferred without forcing a future incompatible representation.

## Concurrency boundary

The existing `TenantOperationLock` is a persistent operational service that
mutates the database. Phase 2H is intentionally internal and non-operational, so
it does not acquire that service. The engine provides a non-blocking,
tenant-scoped in-process guard: a duplicate concurrent execution fails with a
retryable `RetentionConcurrencyError`, while Phase 2G exact deletion remains
idempotent and ownership scoped.

This guard is not a cross-process lease. Phase 2I must acquire the existing
tenant operation lock before backup finalization, restore, retention, durable
deletion, or future download and hold it across fresh evidence loading, plan
creation, and execution. Phase 2H's optimistic revalidation remains required
inside that lease.

## Capability and operational status

The nine internal foundations now report ready:

- SQLite snapshot
- tenant logical export
- media capture
- canonical manifest
- deterministic package
- independent package verification
- encrypted artifact
- durable storage
- retention engine

`RETENTION_ENGINE_READY=True` does not authorize automatic deletion.
`OPERATIONAL_PROVIDER_STACK_READY=False` and
`real_execution_available()=False` remain locked. System checks validate only
bounded settings and capability consistency; they do not enumerate durable
objects, build plans from real data, mutate the database, or touch ciphertext.

There is no scheduling, operational execution, data migration, production data
mutation, or deployment in Phase 2H.
