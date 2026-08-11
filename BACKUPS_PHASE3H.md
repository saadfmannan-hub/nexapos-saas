# Nexa Backup & Restore — Phase 3H

Phase 3H hardens production activation boundaries without deploying, starting
workers or Beat, contacting live KMS/S3 services, or enabling backup/restore
execution. `OPERATIONAL_PROVIDER_STACK_READY` remains `False`,
`BACKUP_EXECUTION_ENGINE_ENABLED` defaults to `False`, and
`BACKUP_RESTORE_MUTATION_ENABLED` defaults to `False`.

## Async reliability model

The database is authoritative. Celery delivery and its result backend are not.
Every new Phase 3H handoff records durable queue intent before broker
publication. `BackupActivity` is the append-only dispatch journal, so no schema
migration or historical rewrite is required.

The journal records:

- `backup.dispatch_requested` before a backup publish is allowed;
- `backup.dispatch_attempted` / `restore.dispatch_attempted` for each bounded
  broker call;
- `backup.dispatch_confirmed` / `restore.dispatch_confirmed` only after
  `apply_async` returns successfully;
- sanitized dispatch-failure evidence while the operation stays `QUEUED`; and
- a redispatch event only after an eligible reconciliation publish succeeds.

The HTTP and scheduled paths make at most three immediate publish attempts.
They never run backup or restore work inline. A failed publish does not turn a
durable queue request into a fabricated execution failure. It remains visible
and eligible for bounded reconciliation.

## Commit-to-broker gap closure

The Phase 2J database-commit-to-broker window is closed for new Phase 3H queue
intents as follows:

| Failure window | Durable evidence | Recovery |
| --- | --- | --- |
| Process exits after commit, before first publish | queue intent, no confirmation | eligible queued record may be redispatched after the grace interval |
| Broker call fails | attempts plus sanitized failure | queued record may be redispatched within the total attempt budget |
| Broker accepts, process exits before confirmation write | attempt without confirmation | safe duplicate publish; worker claim remains authoritative |
| Broker accepts and confirmation persists | confirmation | never automatically redispatched |
| Worker claims before a duplicate arrives | lifecycle/claim or lease evidence | duplicate execution is rejected |

Legacy rows are not reinterpreted. Reconciliation requires the Phase 3H
`backup.dispatch_requested` marker for backups and the explicit
`restore.queued` confirmation event for restores. This prevents historical
schedule reconstruction and prevents preflight-only restore rows from being
treated as destructive queue requests.

## Exact reconciliation rules

`reconcile_queued_backup_dispatches()` may republish only a `QUEUED`, non-safety
backup with Phase 3H queue intent, no broker confirmation, no execution event,
no start timestamp, no active-marked tenant lease, no provider/object/hash
metadata, no terminal evidence, and remaining bounded attempt budget.

`reconcile_queued_restore_dispatches()` is stricter. It requires exact
`QUEUED` state, an explicit `restore.queued` event, no broker confirmation, no
worker-start/claim evidence, no start timestamp, no safety backup, no rollback
attempt, no mutation/recovery/terminal event, no active-marked tenant lease, and
remaining attempt budget.

A restore in `AUTHORIZING`, `LOCKING`, `SAFETY_BACKUP`, `VALIDATING`,
`RESTORING`, `VERIFYING`, `ROLLING_BACK`, `FAILED`, `ROLLED_BACK`,
`INDETERMINATE`, or `SUCCEEDED` is never automatically republished. An expired
but still active-marked lease blocks restore redispatch until an operator
classifies the associated operation. Reconciliation does not release locks,
reset state, mutate tenant business data, call a provider, delete an object, run
retention, or execute backup/restore work.

The control task `apps.backups.tasks.reconcile_backup_control_plane` runs only
on `nexa.backup_scheduling`. It is disabled when both mutation settings are
disabled. Even when delivered, backup and restore publication are independently
gated by the existing execution-availability guards.

## Duplicate-message safety

The backup runtime accepts only `QUEUED` records and obtains the tenant lease
before transitioning to `PREPARING`. A second message cannot re-enter a
transitional or successful backup. A worker loss before the first transition is
safe to retry only after lease/dispatch classification.

The restore task atomically claims `QUEUED` as `AUTHORIZING`. A second message
observes active/ambiguous state and cannot begin mutation. `SUCCEEDED` is a safe
read-only result; mutation and recovery-required states reject replay.

Both tasks intentionally retain `acks_late=False` and
`reject_on_worker_lost=False`. Early acknowledgement avoids broker-driven replay
after an unknown provider or destructive stage. Database reconciliation, not
late acknowledgement or Celery result state, is the recovery authority.

## Worker-loss and stale backup classification

`reconcile_stale_backup_operations()` is classification-only:

| Durable status/evidence | Category | Operator behavior |
| --- | --- | --- |
| `QUEUED` beyond threshold | `STALE_QUEUED` | inspect dispatch/lease journal; only the exact republish rules may publish |
| `PREPARING`, `SNAPSHOTTING`, `PACKAGING` | `STALE_PRE_DURABLE` | inspect worker and workspace evidence; do not blindly reset |
| `UPLOADING`, or `VERIFYING` without exact metadata | `AMBIGUOUS_PROVIDER_STAGE` | inspect provider evidence; do not upload, delete, or replay automatically |
| `VERIFYING` with persisted backend/bucket/key/version/hash | `DURABLE_OBJECT_VERIFIED_PENDING_DB` | preserve object and perform operator reconciliation; do not fabricate success |
| terminal lifecycle | `TERMINAL` | no stale action |

This covers loss before runtime claim, during snapshot/package, during upload,
after durable verification, and before terminal database transition without
inventing certainty.

Phase 3E stale restore classification remains non-destructive:
`STALE_QUEUED`, `STALE_PRE_MUTATION`, `RETRYABLE_PRE_MUTATION_FAILURE`, and
`AMBIGUOUS_MUTATION`. Mutation-related ambiguity always requires operator
review; automatic replay is forbidden.

## Provider journal and restart attestation

For S3/Spaces, Phase 3G persists exact backend, bucket, opaque key, version ID,
artifact hash, encryption-key reference, and wrapped DEK envelope. Historical
retrieval and version-aware retention use that persisted identity instead of
current bucket settings. This closes restart-persistent cloud-object identity
attestation for rows with complete metadata.

The local private-filesystem provider intentionally keeps bucket/version fields
blank and preserves UUID object-key semantics. It is a development provider and
is rejected for production activation. No legacy local row is reinterpreted as
S3.

A worker loss after an S3 upload but before exact metadata persistence remains
an `AMBIGUOUS_PROVIDER_STAGE`; Phase 3H does not scan the bucket, rewrite
history, or delete possible orphan objects.

## Readiness and attestation

`assess_operational_readiness()` returns secret-free checks for:

- key management;
- durable storage;
- broker;
- backup worker route;
- restore worker route;
- scheduler;
- reconciliation;
- retention; and
- database/alert policy.

Each result is `READY`, `WARNING`, or `NOT_READY`. Configuration-only readiness
is the default and performs no network calls. Explicit provider attestation may
call only KMS `DescribeKey` and S3 `HeadBucket`; it never uploads, downloads,
encrypts, decrypts, or deletes a backup object. These calls prove reachability
and limited permissions, not the complete future production permission set.
Controlled activation rehearsal is still mandatory.

Local KMS and local durable storage are `NOT_READY` for production. Raw provider
exceptions, credentials, endpoints with authorization data, broker URLs, and
secret values are absent from readiness results and audit descriptions.

## Platform monitoring and alert thresholds

The Platform Admin health page is read-only and unavailable to tenant Owners.
It derives its signals from database state and performs no provider health call.
It shows:

- backup/restore activation state;
- production KMS/storage configuration state;
- broker, scheduler, and reconciliation readiness;
- queued, active, failed, stale, and recovery-required counts;
- oldest queued ages;
- last scheduler claim and last successful scheduled backup; and
- active-marked/expired tenant leases with public operation IDs, kind, and age.

The configurable DB-based thresholds are:

- `BACKUP_QUEUED_AGE_WARNING_SECONDS` (default 900);
- `BACKUP_RESTORE_QUEUED_AGE_WARNING_SECONDS` (default 900);
- `BACKUP_FAILED_COUNT_WARNING` (default 1); and
- `BACKUP_STALE_OPERATION_SECONDS` (default 21600).

No external notifications or Flower dependency are added.

## Production process ownership

Run these as separate production processes only after activation approval:

1. backup worker: queue `nexa.backups`, concurrency 1 initially;
2. restore worker: queue `nexa.restores`, concurrency 1;
3. scheduler/control worker: queue `nexa.backup_scheduling`; and
4. exactly one Celery Beat scheduler instance.

Never run Beat on every web or worker node. Duplicate Beat owners create
unnecessary claims and control traffic even though occurrence idempotency still
protects backup identity.

## Future backup activation checklist

Before setting `BACKUP_EXECUTION_ENGINE_ENABLED=True`:

1. provision and approve production AWS KMS configuration;
2. provision a private S3/Spaces bucket with public access disabled;
3. grant and verify the least-privilege KMS and exact object permissions;
4. configure a non-eager production broker;
5. deploy one backup worker and one scheduler/control worker;
6. assign exactly one Beat owner;
7. verify both Beat entries and the three isolated routes;
8. enable queue/stale/lease monitoring and alert ownership;
9. approve retention policy and exact version-aware deletion permissions;
10. select a non-critical first test tenant;
11. run a controlled backup and independent retrieval/verification rehearsal;
12. confirm the rollback/deactivation owner and change window; and
13. explicitly approve the code-capability/activation flag change.

## Additional future restore activation checklist

Before setting `BACKUP_RESTORE_MUTATION_ENABLED=True`:

1. complete every backup activation prerequisite;
2. deploy the isolated restore worker at concurrency 1;
3. prove the protected safety-backup path;
4. perform a controlled restore rehearsal on approved non-production data;
5. approve recovery-required escalation and operator ownership;
6. verify no automatic replay after mutation begins; and
7. explicitly approve destructive restore activation.

## Deactivation and rollback

If activation health degrades, set both mutation settings to `False`, stop Beat,
stop the control worker, stop restore then backup workers after their supervised
shutdown window, and preserve every database row, activity, lock, and provider
object. Do not reset transitional statuses, release ambiguous locks, delete
provider objects, or replay destructive restore messages. Capture the health
snapshot and activity trail, classify stale operations, and escalate ambiguous
provider/mutation states before any later restart.

## Phase 3H activation status

No production activation occurred. No worker or Beat process was started. No
live KMS/S3 call, backup, restore, provider scan, historical rewrite, or
deployment occurred. Remaining blockers include production credential
provisioning, live least-privilege attestation, live worker/Beat ownership and
monitoring, controlled backup/restore rehearsals, orphan-provider operational
procedure, download authorization, and explicit change approval. Therefore
`OPERATIONAL_PROVIDER_STACK_READY=False` remains correct.
