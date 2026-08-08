# Backup Engine Phase 2J: Scheduled Worker Boundary

Phase 2J registers the real Celery task boundary and a tenant-local daily
schedule dispatcher. It does not enable the backup engine, start workers or
Beat, deploy configuration, expose an HTTP action, or add restore behavior.

## Celery execution architecture

The heavy task has the stable name `apps.backups.tasks.execute_backup` and is
routed only to `nexa.backups`. Its two arguments are canonical public UUID
strings:

- `backup_public_id`
- `business_public_id`

The task rejects primary keys, ORM objects, paths, provider contexts, storage
references, encryption identifiers, and non-canonical UUID values. It resolves
the durable tenant and backup record again inside the worker, then reaches the
Phase 2I coordinator only through `request_backup_execution()`, the approved
composition root.

Execution requires all of the following:

- `BACKUP_EXECUTION_ENGINE_ENABLED=True`;
- `OPERATIONAL_PROVIDER_STACK_READY=True`;
- a non-empty Celery broker URL;
- `CELERY_TASK_ALWAYS_EAGER=False`;
- the exact `nexa.backups` route;
- a genuine non-eager Celery request delivered with the `nexa.backups` routing
  key; and
- valid non-mutating runtime/provider composition checks.

Direct calls, eager calls, request-thread calls, and incorrectly routed calls
fail before the runtime coordinator. There is no synchronous or management
shell fallback.

## Dedicated queues and fixed Beat entry

The queues are deliberately separated:

| Work | Task | Queue |
| --- | --- | --- |
| Long-running backup | `apps.backups.tasks.execute_backup` | `nexa.backups` |
| Lightweight due scan | `apps.backups.tasks.dispatch_due_backup_schedules` | `nexa.backup_scheduling` |

Settings contain one fixed Beat-compatible dispatcher entry with a five-minute
cadence. The dispatcher never constructs providers or invokes the backup
pipeline. It claims due database rows and publishes only the two public UUIDs
to the heavy queue.

The configuration does not start an external Beat process. Example future
commands, after operational approval, are:

```text
celery -A config worker -Q nexa.backups --concurrency=1 -l info
celery -A config worker -Q nexa.backup_scheduling -l info
celery -A config beat -l info
```

These commands are documentation only; Phase 2J performs no deployment or
process activation.

## Existing schedule schema and migration decision

No migration is required. The Phase 1 `BackupSchedule` already persists:

- one schedule per business;
- enabled/disabled state;
- IANA timezone name;
- tenant-local execution time;
- aware `next_run` cursor;
- `last_claimed_run` occurrence;
- last successful and failed scheduled backup links; and
- fixed `ALL_ENABLED` scope.

`BackupRecord` already persists the scheduled local date, trigger, system actor
state, requested/queued timestamp, and a tenant-local unique idempotency key.

When an enabled schedule has no cursor, the dispatcher initializes its next
future occurrence and does not invent a historical run. Schedule updates also
calculate that future cursor when an enabled configuration omits `next_run`.

## Daily and timezone semantics

Version 1 supports one daily full `ALL_ENABLED` backup per enabled tenant. The
schedule's stored IANA timezone and wall-clock time are authoritative. Every
comparison uses aware UTC values.

The resolver handles timezone transitions deterministically:

- a normal wall time maps to its unique UTC instant;
- an ambiguous fall-back time uses the first chronological instant; and
- a nonexistent spring-forward time advances to the first valid local minute.

`Asia/Muscat` therefore remains UTC+04 without DST, while other supported IANA
zones retain deterministic DST behavior. The local date associated with the
resolved occurrence is stored on the backup record.

## Eligibility and scope

The dispatcher creates no backup when the business is inactive, the schedule
is disabled, the assigned plan is inactive, the subscription is not
operational, the schedule scope is not `ALL_ENABLED`, or no currently entitled
POS/WMS product exists.

Entitlements are resolved at dispatch and again by Phase 2I at execution. The
scheduled record therefore contains only currently enabled products and cannot
silently add POS or WMS components that the tenant does not own. Scheduled
requests use an immutable `SYSTEM` actor; manual requests retain their human
actor. Both share the same runtime, tenant lease, durable success boundary,
cleanup rules, and retention handoff.

## Occurrence idempotency and concurrency

For each claimed run, the dispatcher derives an idempotency key from the
business public UUID, scheduled local date, and exact scheduled UTC instant.
The existing unique `(business, idempotency_key)` constraint is the durable
at-most-one-record authority.

Each due row is selected under `transaction.atomic()` and
`select_for_update()`. The dispatcher checks for active tenant backups, creates
or validates the idempotent record, advances `next_run`, persists
`last_claimed_run`, and registers broker publication with
`transaction.on_commit()`. Two dispatcher scans cannot intentionally create a
second backup record for the same occurrence.

There is no database outbox/delivery receipt in the current schema. A process
loss after database commit but before broker acceptance can leave one durable
`QUEUED` record without a delivered message. Phase 2J does not fabricate a
delivery marker or silently add a migration. Broker publication itself uses
three bounded connection retries. A future outbox/reconciliation phase is
required to eliminate this final commit-to-broker window.

## Missed and overlapping schedules

If Beat was offline across multiple daily occurrences, the dispatcher selects
only the latest due local occurrence and advances the cursor beyond the current
time. It creates one catch-up backup, never one per missed day.

If any manual or scheduled backup for the tenant is `QUEUED`, `PREPARING`,
`SNAPSHOTTING`, `PACKAGING`, `UPLOADING`, or `VERIFYING`, the due occurrence is
deferred. Its cursor is left unchanged so a later dispatcher cycle can retry
after the tenant is free. This prevents unbounded queued records. The Phase 2I
database tenant operation lease remains the final cross-worker concurrency
authority.

## Retry and failure policy

The execution task permits at most three retries with bounded exponential
backoff plus jitter. A retry is allowed only when the runtime exception is
explicitly marked retryable and the durable record is still `QUEUED`. This is
currently the safe lock-contention window. Verification, manifest, encryption,
configuration, tenant, state, and post-transition failures do not retry.

When the retry limit is exhausted, a still-queued record is marked `FAILED`
with the fixed `task_retry_exhausted` code. Phase 2I remains the owner of
provider-stage failure status and sanitized evidence; the task does not replace
those details with raw Celery/provider exceptions. Scheduled terminal results
update the existing success/failure links.

## Acknowledgements, worker loss, and limits

`acks_late=False` and `reject_on_worker_lost=False` are intentional. Phase 2I
rejects replay of transitional records and still has a narrow durable-provider
journal gap, so automatic redelivery after worker loss is not yet safe. Early
acknowledgement prevents a second worker from blindly replaying an ambiguous
long-running operation. The durable record and workspace evidence remain for
operator reconciliation.

The heavy task has a 21,600-second soft limit and a 21,900-second hard limit.
These exceed the individual bounded provider timeouts. The dispatcher has a
240-second soft and 270-second hard limit. System checks reject invalid cadence,
limits, queue routes, missing broker, eager mode, and inconsistent capability
state.

## Capability state and activation prerequisites

Phase 2J sets these internal code capabilities to true:

- `ASYNC_EXECUTION_BOUNDARY_READY=True`
- `SCHEDULE_DISPATCHER_READY=True`
- `RUNTIME_COMPOSITION_READY=True`

`OPERATIONAL_PROVIDER_STACK_READY=False` remains mandatory. The local KEK and
local durable provider are development foundations, not an approved production
KMS/object-storage stack, and historical Phase 2G objects still lack
restart-persistent ownership/file-identity re-attestation. Production worker
activation cannot make those trust gaps disappear.

Consequently `real_execution_available()` remains false by default and remains
false even if the engine setting is accidentally enabled while the operational
flag is false. Production activation requires all of the following to be
approved and proven:

- restart-safe durable-object attestation/journaling;
- production KEK/KMS integration and rotation policy;
- production durable storage and root/provider validation;
- dedicated broker, workers, queue monitoring, and Beat ownership;
- recovery handling for worker-loss and commit-to-broker gaps;
- download authorization and later restore controls; and
- an explicit deployment/change-management decision.

Phase 2J adds no UI, owner/admin control, HTTP execution endpoint, signal-based
execution, restore mutation, production schedule activation, persistent data
mutation, or deployment.
