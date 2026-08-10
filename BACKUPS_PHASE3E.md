# Nexa Backup & Restore — Phase 3E

Phase 3E adds a restart-safe asynchronous execution boundary for destructive restore. It does not enable restore mutation in production, start a worker, deploy code, or perform a real restore.

## 1. Restore task architecture

The registered Celery task is `apps.backups.tasks.execute_restore`. Its only arguments are canonical string forms of `restore_public_id` and `business_public_id`. The task refetches the business, restore request, source backup, actor snapshot, and runtime configuration from durable state. Paths, ORM objects, preflight results, provider context, storage keys, and encryption keys never cross the broker.

The task refuses direct calls, eager execution, missing brokers, the wrong delivery queue, invalid routes, disabled mutation, an incomplete mutation boundary, and invalid restore provider composition. The restore runtime is constructed and validated before the durable claim. There is no synchronous HTTP, management-command, signal, scheduler, or shell fallback.

## 2. Dedicated queue

`BACKUP_RESTORE_QUEUE_NAME` defaults to `nexa.restores`, and `CELERY_TASK_ROUTES` maps only `apps.backups.tasks.execute_restore` to that queue. Heavy backup work remains on `nexa.backups` and scheduling remains on `nexa.backup_scheduling`.

The future production worker command is documentation only:

```shell
celery -A config worker -Q nexa.restores --concurrency=1 -l info
```

Phase 3E does not start this worker. Concurrency one reduces operational overlap, but the persistent tenant operation lock remains the final tenant-isolation authority.

## 3. Durable state machine

Phase 3E reuses `RestoreOperation.status`; it does not add an in-memory or parallel state system.

| Persistent state | UI meaning | Replay behavior |
| --- | --- | --- |
| `QUEUED` | Queued | May be claimed once |
| `AUTHORIZING` | Checking restore readiness | Active/pre-mutation; automatic redelivery blocked |
| `LOCKING` | Checking restore readiness | Active/pre-mutation; automatic redelivery blocked |
| `SAFETY_BACKUP` | Creating safety backup | Active/pre-mutation; automatic redelivery blocked |
| `VALIDATING` | Safety backup secured | Active/pre-mutation; automatic redelivery blocked |
| `RESTORING` | Restoring business data/files | Mutation is ambiguous after worker loss; replay blocked |
| `VERIFYING` | Verifying restored data | Mutation is ambiguous after worker loss; replay blocked |
| `SUCCEEDED` | Completed | Returned safely without mutation |
| `FAILED` with `pre_mutation_…` code | Failed safely | Eligible only for the explicit bounded pre-mutation retry policy |
| other `FAILED` | Failed safely | Automatic retry forbidden |
| `ROLLING_BACK` | Recovery required | Automatic retry forbidden |
| `ROLLED_BACK` | Failed safely | Automatic retry forbidden in Phase 3E |
| `INDETERMINATE` | Recovery required | Automatic retry forbidden |

Existing activity events provide finer progress between durable status writes, including media completion, without changing the schema.

## 4. Durable claim

`claim_restore_operation()` uses `transaction.atomic()`, `select_for_update()`, and a compare-and-swap status update. Only exact `QUEUED` rows and explicit `FAILED` rows with a `pre_mutation_` failure code and no rollback attempt can enter `AUTHORIZING`. The business, restore, source-backup tenant snapshot, and public identifiers are rebound inside the transaction.

Two workers cannot both obtain a valid claim. A second claimant observes an active or ambiguous state and exits without entering Phase 3B. The existing `TenantOperationLock` still serializes restore, safety backup, normal backup, and retention per tenant.

## 5. Idempotency

The restore public UUID identifies the operation; the existing unique `(business, idempotency_key)` constraint protects request creation. The durable status claim protects destructive execution. The append-only `restore.queued` activity event makes confirmation handoff idempotent while the status is still `QUEUED`, preventing repeated browser submissions from publishing duplicate messages.

`SUCCEEDED` is returned without execution. `INDETERMINATE`, `ROLLING_BACK`, `ROLLED_BACK`, and active/ambiguous states never reset to `QUEUED`. A source backup remains immutable and protected by its `PROTECT` relations.

## 6. Worker-loss behavior

| Crash window | Durable evidence | Restart behavior |
| --- | --- | --- |
| After broker receipt, before claim | `QUEUED` | Early acknowledgement may leave a stale queued request; reconciliation reports it, but does not rerun it |
| After claim, before/during preflight | `AUTHORIZING` | Redelivery blocked; classified as stale pre-mutation for operator review |
| While acquiring tenant lock | `AUTHORIZING` or `LOCKING` | A caught lock failure is persisted as pre-mutation and may use the bounded retry policy; hard loss is not replayed |
| During safety backup | `SAFETY_BACKUP` | Redelivery blocked; safety artifact metadata remains linked if it reached durable completion |
| After safety backup verification | `VALIDATING` with `safety_backup` | Redelivery blocked; protected safety backup remains visible and retention-ineligible |
| After first destructive database write | `RESTORING` | Ambiguous; automatic replay forbidden |
| During media publication | `RESTORING` | Ambiguous; automatic replay forbidden |
| During post-restore verification | `VERIFYING` | Ambiguous; automatic replay forbidden |
| After `SUCCEEDED` persistence | `SUCCEEDED` | Any later delivery returns safe public status and does not mutate |

The non-destructive `reconcile_stale_restore_operations()` helper classifies stale queued, pre-mutation, explicitly retryable failure, and ambiguous mutation rows. It reports whether an unexpired tenant lease exists. It never writes status, releases locks, enqueues work, or runs restore. No periodic production task is installed.

## 7. Acknowledgement choice

`acks_late=False` and `reject_on_worker_lost=False` are deliberate. Early acknowledgement prefers a visible stale job over broker redelivery after an unknown destructive point. Phase 3E can prevent replay but does not implement resumable checkpoints for every database/media mutation. Therefore late acknowledgement would create unnecessary redelivery pressure against ambiguous work.

## 8. Retry policy

Celery retries are bounded to three. Only `RestoreLockUnavailable` is automatically retried, and only after the operation has been persisted as a safe `pre_mutation_…` failure. The retry reacquires the durable claim through the explicit failed-before-mutation policy.

Compatibility failures, selection/binding failures, source corruption, invalid preflight, wrong encryption configuration, relation restore errors, rollback results, post-mutation ambiguity, and `RECOVERY_REQUIRED` are not retried. Exhaustion persists `pre_mutation_task_retry_exhausted` with a fixed safe summary.

## 9. Time limits

The restore task defaults to a 43,200-second soft limit and a 43,500-second hard limit. Both exceed the 21,900-second backup worker hard limit used by the mandatory safety backup. System checks reject missing, reversed, unbounded, or provider-shorter restore limits.

Hard worker loss is handled by durable status and early acknowledgement. It is not treated as permission to replay.

## 10. Safety backup lock inheritance

Phase 3B still owns one tenant restore lease. Its safety-backup runtime receives `InheritedTenantOperationLease`, so it does not acquire a competing tenant lock. The protected safety backup remains `retention_eligible=False`, linked through `parent_restore_operation` and `RestoreOperation.safety_backup`, and cannot be the source backup.

This preserves the no-deadlock design across restore, safety backup, normal backup, and retention.

## 11. Owner UI handoff

When mutation is disabled, Owner confirmation keeps the administrator-disabled message. When mutation is enabled but broker, routing, limits, or runtime composition is unsafe, the page reports that the dedicated secure restore worker is unavailable. When the capability is safe, the final POST validates the existing preflight-bound durable restore request, records `restore.queued`, enqueues only public UUIDs, clears the preflight session, and redirects to the tenant-scoped restore status page.

The status page uses a lightweight ten-second GET refresh while active. GET performs no restore work. It shows queued, readiness, safety-backup, data, file, verification, completed, safe-failure, and recovery-required language without provider details.

## 12. Platform Admin handoff

Platform Admin uses the same task, route, runtime checks, durable claim, and replay rules. `APPROVE_RESTORE` remains necessary for confirmation, and status reads use the existing Platform metadata capability with an exact business/restore binding. There is no force-inline or platform bypass. Support-session Owner pages still use Owner permissions and gain no Platform override.

## 13. Activity and failure persistence

Async execution emits or preserves these bounded events:

- `restore.queued`
- `restore.worker_started`
- `restore.preflight_validated`
- `restore.safety_backup_completed`
- `restore.mutation_started`
- `restore.media_completed`
- `restore.post_verification_completed`
- `restore.completed`
- `restore.failed`
- `restore.recovery_required`

Terminal failures store a bounded code, fixed sanitized summary, final status, and completion timestamp. Raw exceptions, SQL, record contents, provider paths, storage keys, encryption material, and internal primary keys are never written to task results or activity metadata.

## 14. `RECOVERY_REQUIRED`

Phase 3B maps unproven rollback or post-mutation ambiguity to `INDETERMINATE`. Phase 3E renders this as “Restore requires administrator recovery,” never calls Celery retry, and never performs an automatic rollback beyond the rollback guarantees already owned by Phase 3B.

## 15. Availability and checks

`restore_execution_available()` requires all of the following:

- `BACKUP_RESTORE_MUTATION_ENABLED=True`
- `RESTORE_MUTATION_ENGINE_READY=True`
- restore async-boundary code readiness
- a configured Celery broker
- `CELERY_TASK_ALWAYS_EAGER=False`
- exact `BACKUP_RESTORE_QUEUE_NAME=nexa.restores`
- exact restore task route to `nexa.restores`
- valid restore soft/hard limits above the safety-backup worker limit
- successful non-mutating restore runtime composition

Security checks remain clean while mutation is disabled. If mutation is enabled with unsafe async or runtime configuration, Django system checks fail.

## 16. Activation state

`RESTORE_MUTATION_ENGINE_READY=True` describes code readiness only. `BACKUP_RESTORE_MUTATION_ENABLED` still defaults to `False`, so `restore_execution_available()` defaults to `False`. Phase 3E does not change `OPERATIONAL_PROVIDER_STACK_READY=False` or `real_execution_available()=False` for the broader production backup stack.

No migration was required: the existing restore public ID, business/source/safety bindings, status, idempotency key, actor snapshot, timestamps, sanitized failure fields, rollback evidence, append-only activities, and tenant operation lease already provide the necessary persistent evidence.

No deployment, worker start, Beat start, production activation, or real restore was performed.
