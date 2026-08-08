# Backup Engine Phase 3B

## Scope and activation

Phase 3B adds an internal, guarded tenant restore-mutation engine. It does not add an
HTTP endpoint, admin action, task, worker, one-click UI, deployment, or production
activation. `RESTORE_PREFLIGHT_ENGINE_READY` and `RESTORE_MUTATION_ENGINE_READY` are
code-capability flags and are both `True` after this phase. Actual mutation separately
requires the exact boolean setting `BACKUP_RESTORE_MUTATION_ENABLED=True`; its default is
`False`.

The existing Phase 1 schema already supplies the required durable control plane, so no
migration is needed. `RestoreOperation` persists the public operation identity, source
backup, protected safety backup, tenant-scoped idempotency key, lifecycle state, failure
evidence, and rollback evidence. `INDETERMINATE` is the durable recovery-required state.
`BackupRecord` already has `PRE_RESTORE_SAFETY`, `protected`,
`retention_eligible`, and `parent_restore_operation`, backed by the
`safety_backup_is_protected` database constraint.

## Mandatory safety-backup boundary

No registered tenant row is deleted, updated, or imported before a new full
`ALL_ENABLED` safety backup of the current tenant state reaches all of these boundaries:

1. consistent SQLite snapshot;
2. authoritative logical export and media capture;
3. canonical manifest and deterministic package;
4. independent package verification;
5. authenticated encryption;
6. durable private publication and validation;
7. persisted `SUCCEEDED` plus `VERIFIED` metadata; and
8. exact attachment to the current restore operation.

The safety backup uses `PRE_RESTORE_SAFETY`, is a system-initiated record with the
initiating actor retained for audit, is always `protected=True`, and is always
`retention_eligible=False`. Normal daily retention therefore cannot select it. It is
preserved after success and after every failure. The selected source backup is never
mutated or deleted by restore.

If safety backup snapshotting, export, verification, encryption, storage, persistence,
or cleanup cannot prove the durable success boundary, the restore becomes a sanitized
pre-mutation failure and performs zero operational tenant mutations.

## Locking and deadlock prevention

The restore holds one exclusive `RESTORE` tenant lease through final preflight
revalidation, safety backup creation, mutation, post-verification, and transient cleanup.
The backup runtime accepts an internal `InheritedTenantOperationLease` containing only
public tenant/restore identities and the opaque lock token. It re-resolves and validates
the active database lease, heartbeats that same row, and does not acquire or release a
second lease. This prevents the mandatory safety backup from deadlocking with its parent
restore without weakening tenant concurrency.

Backup, restore, and retention operations continue to share the conditional unique
active-lock constraint, so concurrent operations cannot race the restore or delete its
source/safety evidence.

## Fresh Phase 3A handoff

Phase 3B consumes a Phase 3A workspace only through the exact opaque preflight reference
and the immutable approved result. It rejects replaced, forged, cross-tenant, or
context-mismatched results. Under the restore lease it revalidates:

- the complete in-memory preflight binding;
- the private package, extraction, and preflight-evidence identities;
- independent verification evidence;
- current application, schema, and package compatibility policy; and
- the selected historical durable object by full size/hash re-attestation.

The same checks run again immediately before mutation. This mandatory revalidation is
the freshness rule; Phase 3B does not trust elapsed time alone.

## Logical restore architecture

`logical_restore.py` is the only logical import boundary. It reads identity-bound
extracted component streams through the Phase 3A provider and never accepts a path or raw
ZIP from a caller. Every NDJSON record must remain canonical and must match the exact
registered schema, component/version, tenant UUID, model, stable identity, and allowed
field set.

The engine follows the Phase 2C component registry and logical export registry. It never
uses model auto-discovery as restore authority and never restores a raw SQLite file.

Component behavior is deliberately narrow:

- `REFERENCE_ONLY` tenant identity and access-control records are required to match the
  current destination exactly and are never mutated.
- `DEPENDENCY_ONLY` locations and tenant settings are required to match the current
  destination exactly and are never reclassified as replaceable data.
- `REPLACEABLE` POS/WMS operational models are deleted and imported deterministically.
- `NON_RESTORABLE` subscription, entitlement, audit, backup, notification, and transient
  components cannot enter the plan.

This means platform-controlled subscription/plan state, Platform Admin users, passwords,
sessions, tokens, roles, memberships, tenant identity, and tenant settings are not
overwritten. A historical backup whose reference/dependency evidence no longer agrees
with those current authorities fails closed before mutation.

## Tenant-scoped replacement and identity

Destructive work runs in reverse component dependency/import order and reverse registered
model order. Every deletion is an ORM queryset explicitly filtered by the target
`business`; table truncation, unrestricted `.all().delete()`, raw broad DELETE, and raw
database replacement are prohibited.

Import runs in forward dependency/import order. Source database primary keys are omitted
and never used as identity authority. Public UUIDs and registered tenant-singleton
identities drive the operation-scoped identity map. Foreign keys and M2M links resolve by
registered logical public identity, with exact tenant ownership checks. Global User
references must already resolve uniquely by public UUID; User rows and security fields
are never imported. Missing, ambiguous, or cross-tenant references abort the transaction.

Only explicitly exported scalar, canonical JSON, relation, M2M, and media fields are
accepted. Extra or missing fields, internal primary-key leakage, unknown models or
components, unsupported versions/behaviors, invalid identities, and tenant mismatches
are rejected. Destination-local omitted fields, such as checkout idempotency tokens, use
destination defaults rather than historical values.

## Two-phase media restore

`media_restore.py` privately stages every manifest-declared media object and verifies its
full byte count and SHA-256 before database mutation. Logical storage names pass the
existing strict storage-name policy and resolve beneath the configured `MEDIA_ROOT`
without links or traversal.

The collision policy is no-clobber:

- an existing target may be reused only when its full size/hash is identical to the
  verified source object;
- a different existing object aborts before mutation; and
- a missing target is published with an exclusive same-directory temporary file and
  no-overwrite hard-link finalization.

Only files and directories created by the current provider are rollback candidates.
Their filesystem identities are checked before removal. Existing identical files and all
unrelated media are never overwritten or deleted.

## Transaction and atomicity limits

Registered database replacement/import, relation binding, media publication, and
post-restore verification are coordinated inside `transaction.atomic()`. Media
publication is reversible and occurs only after every media object has been privately
staged and collision-checked. An ordinary database, import, media, or verification
exception rolls back the SQLite transaction and removes only identity-proven media
created by this operation.

SQLite and the filesystem do not provide one shared atomic commit. A process or machine
crash after media publication but before database commit can leave provider-created
media while the restore operation remains `RESTORING` or `VERIFYING`. The safety backup,
durable operation state, and expired tenant lease provide the recovery boundary, but the
engine does not claim cross-resource atomicity. If media rollback or final state cannot
be proven, the operation becomes `INDETERMINATE` and surfaces recovery-required evidence.

## Post-restore verification and recovery

The independent post-restore verifier rereads every registered source logical identity
from the live tenant and compares exact normalized fields, record counts, relation
ownership, M2M identities, and media size/hash. `SUCCESS` is impossible without a
`VERIFIED` post-restore result.

Ordinary failures after mutation begins produce `ROLLED_BACK` only when database rollback
and provider-owned media rollback are both proven. Otherwise the operation becomes
`INDETERMINATE` (recovery required). Phase 3B does not recursively invoke restore on its
own safety backup. That backup is retained for a later explicit, controlled recovery
decision.

## Idempotency and durable audit

The database unique constraint on `(business, idempotency_key)` remains authoritative.
A completed request reuses its exact durable result evidence and does not mutate twice.
Only failures explicitly recorded with a `pre_mutation_` failure code may be retried,
and they require a newly revalidated Phase 3A handoff. Rolled-back or indeterminate
operations cannot be automatically retried.

Append-only bounded activities record:

- `restore.started`;
- `restore.preflight_validated`;
- `restore.safety_backup_started` / `restore.safety_backup_completed`;
- `restore.mutation_started`;
- `restore.component_completed`;
- `restore.media_completed`;
- `restore.post_verification_completed`;
- `restore.completed`;
- `restore.failed`; and
- `restore.recovery_required`.

Metadata is limited to public UUIDs, counts, component keys, provider states, and
sanitized issue codes. Record contents, paths, SQL, internal primary keys, encryption
material, manifests, plaintext, and ciphertext are not logged or returned.

## Operational status

Phase 3B is code-complete and testable only with isolated disposable databases,
workspaces, durable roots, and media roots. Restore mutation remains disabled by default.
There is no user-facing execution endpoint or UI, no production configuration change,
no worker activation, no deployment, and no real tenant restore performed by this phase.
