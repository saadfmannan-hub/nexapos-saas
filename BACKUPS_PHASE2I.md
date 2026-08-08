# Backup Engine Phase 2I: Operational Runtime Core

Phase 2I adds the internal end-to-end coordinator that composes every trusted
provider from snapshot through durable encrypted storage. It establishes the
durable database success boundary, persistent tenant lease, safe activity
evidence, cleanup ownership, retention handoff, and a guarded plain task
function. It does not activate execution in production.

## End-to-end sequence and status map

`BackupExecutionCoordinator.execute()` accepts an immutable
`BackupExecutionRequest` containing public UUIDs, the durable request scope and
trigger, an immutable actor snapshot, the exact idempotency key, and an optional
bounded worker identifier. It accepts no database primary key, filesystem path,
SQLite path, object path, KEK, or DEK.

The coordinator resolves the `Business` and `BackupRecord` through their public
UUIDs and requires exact tenant, scope, trigger, idempotency, and actor bindings.
Only `QUEUED` may start. Transitional, failed, deleted, or successful records
cannot restart. A repeated successful request raises the fixed
`backup_already_completed` error and never creates another object.

The runtime sequence is:

1. resolve and validate the durable request;
2. acquire the persistent tenant `BACKUP` operation lease;
3. derive the authoritative immutable context and component plan;
4. create the exact private workspace;
5. create and validate the SQLite snapshot;
6. export registered logical components and clean the snapshot;
7. capture local media and build the canonical manifest;
8. build the deterministic plaintext package and clean Phase 2D-1 staging;
9. independently verify package and restore readiness;
10. encrypt with AES-256-GCM and clean the plaintext package;
11. store and independently revalidate the durable encrypted object, then clean
    local encrypted staging;
12. persist its opaque reference and safe metadata;
13. transition integrity to verified and the durable record to success;
14. invoke retention with exact live provider evidence;
15. clean exact verification/workspace staging; and
16. release the tenant lease in `finally`.

The existing state machine is preserved:

| Durable status | Runtime responsibility |
| --- | --- |
| `QUEUED` | Only permitted starting state |
| `PREPARING` | Context, plan, and private workspace |
| `SNAPSHOTTING` | Snapshot, logical export, media, and manifest |
| `PACKAGING` | Package, independent verification, and encryption |
| `UPLOADING` | Durable publication, revalidation, and metadata persistence |
| `VERIFYING` | Final integrity transition after durable proof |
| `SUCCEEDED` | Durable success boundary reached |
| `FAILED` | Sanitized ordinary failure before success |

## Durable success definition

`BackupRecord.status = SUCCEEDED` occurs only after all of these are true:

- a consistent snapshot was created;
- all registered tenant components were exported;
- media reconciliation and the canonical manifest completed;
- the deterministic package completed;
- independent verification reports both `verified=True` and
  `restore_ready=True`;
- the authenticated encrypted artifact was independently validated and its
  plaintext cleanup is complete;
- Phase 2G published the exact tenant/backup object, independently revalidated
  its bytes, hash, encrypted framing, and ownership, and completed encrypted
  staging cleanup;
- the opaque durable reference and safe result metadata were persisted; and
- integrity transitioned to `VERIFIED`.

The reserved Phase 1 schema already provides the required fields. Phase 2I
persists the opaque object UUID, backend identifier, ciphertext hash and size,
safe KEK identifiers, component/media/row counts, compatibility state,
verification/completion time, and duration. It never persists a workspace,
private root, raw provider path, SQLite path, raw key, ciphertext, or manifest.
No migration is required.

The encrypted object is never deleted because a later database, retention,
activity, or cleanup step fails. If durable publication returned before ordinary
finalization failure, the coordinator first attempts to persist its recovery
identity, marks the record failed rather than successful, and records that the
durable object was preserved.

## Retention boundary

Success is finalized before lifecycle retention, following the Phase 2I
recommended ordering. A retention error therefore cannot invalidate an already
proven durable backup. It produces `retention.failed` plus a `FAILED_SAFE`
retention outcome in the immutable runtime result. Partial maintenance is also
explicit; it is never hidden inside a successful-looking retention result.

The current Phase 2G local provider keeps exact ownership/file-identity evidence
in its worker process. `BackupRecord` safely persists an opaque object reference,
but a newly constructed provider cannot yet re-attest the complete Phase 2G
ownership evidence for historical records. Phase 2I therefore:

- executes Phase 2H with the newly stored object because its exact evidence is
  still live;
- never fabricates descriptors for historical records;
- never enumerates or deletes historical/orphan files; and
- emits `retention.historical_evidence_deferred` if historical successful daily
  records exist.

This is the exact non-destructive boundary required by Phase 2I. A future phase
must add restart-safe provider re-attestation or an approved durable journal
before historical automated pruning can become operational.

## Locking and concurrency

The process-local Phase 2H guard is not the production lock. Phase 2I acquires
the existing database-backed `TenantOperationLock` with `OperationKind.BACKUP`.
The conditional unique constraint permits one active tenant operation across
workers and process boundaries. The lease is bounded by
`BACKUP_EXECUTION_LOCK_LEASE_SECONDS` (default 21,600; allowed 300 through
86,400 seconds), heartbeated after major stages, and always released in
`finally`.

The lease spans context creation, provider execution, durable finalization, and
retention. It blocks another backup, restore, retention, future download, or
durable delete using the same tenant lock. Contention leaves a `QUEUED` request
retryable and emits only sanitized lock evidence.

## Idempotency and crash windows

The durable `(business, idempotency_key)` uniqueness constraint remains the
request idempotency authority. `BackupExecutionContext.operation_correlation_id`
and workspace UUID are deterministically derived from the backup UUID and key.
An existing workspace therefore blocks a blind restart instead of being
overwritten.

Phase 2I intentionally does not retry `FAILED` or transitional records. This
prevents duplicate publication when the previous worker's final state is
ambiguous.

| Crash window | Restart behavior |
| --- | --- |
| After snapshot | Exact transient cleanup runs; non-`QUEUED` restart is rejected |
| After package | Exact package/input cleanup runs; restart is rejected |
| After encryption | Exact encrypted staging is cleaned only before a durable attempt |
| During durable publication | Source staging is preserved when publication is ambiguous |
| After durable provider return | Opaque recovery identity is persisted where execution can continue |
| After durable verification, before success | Durable object remains; record fails safely or remains transitional on an interrupt |
| After database success | Repeat execution is rejected; no duplicate upload occurs |
| During retention | New success remains valid; explicit lifecycle warning supports a later retry |

There remains a narrow process-kill window between durable provider return and
the first database metadata update. Eliminating it requires a restart-persistent
provider journal/re-attestation mechanism. Phase 2I does not silently add that
schema and does not claim the residual window is solved.

## Cleanup ownership

Predecessor providers retain their exact cleanup responsibilities:

- export batch cleanup owns the SQLite snapshot;
- Phase 2D-2 owns component, media, and manifest inputs;
- encryption owns the plaintext package;
- durable storage owns local encrypted staging;
- Phase 2I owns exact verification evidence and the final workspace shell.

The coordinator retries only opaque, provider-owned cleanup calls. It removes
only empty, named `WorkspaceArea` directories and then an empty exact workspace.
Unknown contents, links, replacement files, and nested artifacts are left in
place with a cleanup warning. It never recursively deletes an unknown workspace
tree. A durable object is outside transient cleanup ownership.

## Failure and activity evidence

Provider and raw exceptions are mapped by current stage to bounded codes such as
`snapshot_failure`, `logical_export_failure`, `media_manifest_failure`,
`package_failure`, `package_verification_failure`, `encryption_failure`,
`durable_storage_failure`, `durable_finalization_failure`, `lock_unavailable`,
and `execution_state_invalid`. Only fixed summaries reach `BackupRecord`.

Activities include execution started, snapshot/export/manifest/package
completed, verified, encrypted, durable stored, completed, failed, retention
completed/partial/failed/deferred, and cleanup deferred. Structured metadata is
limited to fixed stage/status/error codes, counts, durations, byte counts, and
safe provider/backend identifiers. Paths, object keys, SQL, media names, raw
errors, manifests, ciphertext, and crypto material are excluded.

Human requests use the durable creator snapshot. Scheduled requests use an
immutable `SYSTEM` actor snapshot. Phase 2I supports both without adding a
scheduler.

## Composition, capability, and production limits

`build_runtime_provider_stack()` is the sole production composition root. It
constructs exact shared instances for the workspace manager, snapshot provider,
logical exporter, media and manifest providers, package coordinator, verifier,
local KEK provider, encryption provider, durable storage provider, and retention
engine. `RuntimeProviderStack.validated()` rejects incompatible roots or
provider identities without creating a backup.

`RUNTIME_ORCHESTRATOR_READY=True` means this internal coordinator is implemented.
`OPERATIONAL_PROVIDER_STACK_READY=False` remains mandatory because historical
restart-safe durable attestation is incomplete. Consequently,
`real_execution_available()` is false by default and remains false even if the
engine setting is mistakenly enabled. Non-mutating system checks validate the
lease, policies, roots, encryption configuration, provider composition,
capability consistency, broker presence, and non-eager execution.

`execute_backup()` remains a plain function with no Celery decoration, `.delay`,
beat schedule, eager fallback, HTTP invocation, or request-thread execution. It
can reach the coordinator only after both the operational capability guard and
`assert_safe_async_execution_configuration()` pass. Phase 2J owns dedicated
worker registration and activation.

The configured local KEK provider is for internal development only. Production
enablement still requires an approved KEK/KMS, production durable provider and
root validation, restart-safe durable evidence, a dedicated broker/worker,
deployment-specific checks, and explicit operational authorization.

Phase 2I adds no restore mutation, restore decryption, UI action, HTTP endpoint,
admin action, schedule dispatcher, Celery task registration, migration,
production data mutation, or deployment.
