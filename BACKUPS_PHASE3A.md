# Backup & Restore Phase 3A

Phase 3A adds a non-mutating restore-preflight engine. It can prove that one
selected encrypted durable backup is authentic, current-code compatible, and
structurally ready for a later restore. It does not import records, replace
tenant state, or write media to live storage.

## Selection trust boundary

Callers provide only an operation UUID, business public UUID, backup public
UUID, actor snapshot, and idempotency key. They cannot provide a filesystem
path, object path, database primary key, encryption key, manifest, or archive.
The engine resolves `Business` and `BackupRecord` internally and accepts only a
tenant-bound `SUCCEEDED`/`VERIFIED` record with no deletion timestamp and with
complete opaque durable metadata.

`BackupRecord` already persists the safe local-provider lookup tuple:

- `tenant_public_id_snapshot` and `public_id`;
- `storage_backend_identifier` and `opaque_object_key`;
- `whole_artifact_hash` and `backup_size_bytes`; and
- `encryption_key_identifier`.

No migration was required. The object UUID is not interpreted as a path by the
coordinator.

## Tenant binding and locking

The requested business is resolved first. Selection occurs through the
business-scoped manager and requires the record's tenant snapshot to equal the
current business public UUID. A platform actor cannot use a different tenant's
backup UUID to cross this boundary.

Preflight uses the existing tenant-exclusive lease with `OperationKind.RESTORE`.
The lease is bounded, heartbeated between expensive stages, and released in a
`finally` path. Backup, retention, deletion, download, and another restore are
therefore expected to use the same exclusion boundary.

## Restart-safe durable retrieval

The Phase 2G local provider now has a restart re-attestation API. It receives a
typed persisted descriptor, derives exactly one contained provider-owned path
from tenant UUID, backup UUID, and opaque object UUID, and never scans or
enumerates the durable root.

Re-attestation verifies:

- backend, tenant, backup, and reference types;
- private root and ancestry, with no link/reparse traversal;
- a single exact object filename and single-link regular file;
- root, ancestry, directory, and file identities on one filesystem; and
- exact persisted ciphertext byte count and SHA-256.

The resulting handle is process-local and short-lived. Every validation and
open rechecks identities, size, and hash. Releasing it removes only memory
evidence; it never deletes the durable backup.

## Cryptographic validation and decryption

Phase 2F remains the authoritative cryptographic boundary. Its restore stream
API re-hashes the complete durable ciphertext and strictly parses canonical
framing/header data. It validates the encrypted schema/version, AES-256-GCM,
tenant and backup UUIDs, verified package format, verification provider/schema,
KEK provider/key/version, wrapped DEK metadata, exact framed size, and canonical
header hash.

The KEK provider unwraps the DEK. The authenticated header is GCM AAD, and the
tag is finalized while streaming. Wrong KEKs, modified headers, ciphertext,
tags, truncation, and appended bytes fail closed. Raw DEKs, KEKs, wrapped key
bytes, plaintext, paths, and raw headers never appear in results or errors.

## Private plaintext reconstruction

Plaintext is streamed into the operation's private
`WorkspaceArea.RESTORE_PREFLIGHT` area. `package.zip` is created exclusively,
written with bounded chunks, hashed while writing, fsynced, and published by a
no-clobber hard-link transition. Directories and files are private (`0700` and
`0600` where the platform supports POSIX modes).

The authenticated Phase 2F plaintext byte count and SHA-256 must match exactly.
The restored package provider performs only a bounded preliminary canonical
manifest read to construct opaque package evidence; it does not claim restore
readiness.

## Independent package re-verification

The authoritative Phase 2E `IndependentPackageVerifier` now accepts explicitly
marked opaque package-access providers. Both the original deterministic package
provider and the restored package provider identify their exact result source.
The verifier still rejects generic duck-typed or path-based inputs.

Every restore preflight reruns Phase 2E. Historical `VERIFIED` database state is
selection evidence only. Restore-time verification rechecks raw ZIP structure,
deterministic metadata and ordering, manifest canonicality, package and payload
digests, tenant/backup identity, components, record streams, media indexes,
media payloads, totals, schemas, and registry-owned logical definitions.

## Restore-time compatibility

The manifest continues to be validated against its historical backup context,
while the restore compatibility policy receives current application version,
backup format version, migration fingerprint, component registry, component
versions, restore behaviors, logical schemas, and ordering rules.

The result is exactly `COMPATIBLE`, `INCOMPATIBLE`, or `NOT_PROVEN`. A different
migration fingerprint is `NOT_PROVEN`; an unsupported format, component
contract, or newer minimum restore version is incompatible. Phase 3B may consume
only an explicitly `COMPATIBLE` preflight.

## Safe extraction and integrity

Only after successful independent verification does the provider create an
`extracted/` tree. It does not call `ZipFile.extractall()`. Every archive entry
must equal an engine-generated expected path:

```text
extracted/
  manifest.json
  components/0001/records.ndjson
  components/0001/media-index.ndjson
  media/00000001.bin
```

Traversal, absolute paths, backslashes, empty/dot segments, unexpected names,
duplicates, encrypted ZIP members, directories, and symlink-like members are
rejected. Each file is created exclusively and streamed with an exact manifest
byte-count and SHA-256 comparison. The final file and directory identities are
retained for cleanup.

## Component, record, and media preflight

The immutable component plan is rebuilt from the current authoritative registry
and the verified manifest. It contains component key/version, restore behavior,
dependency-first import order, model sequence, record count, and media-reference
count. Unknown, duplicate, version-mismatched, dependency-incomplete, cyclic, or
unsupported definitions fail closed.

Phase 2E's restore-time pass validates canonical NDJSON, allowed model sequence,
line and total bounds, logical identities and relation references, duplicate
ordering/identity constraints, allowed field/type schemas, media source
references, storage-name safety/collisions, and exact media payload hashes. No
Django model is instantiated or saved from restored records.

## Result and evidence

`RestorePreflightResult` is immutable and contains only public UUIDs, readiness,
compatibility, safe counts, plaintext package byte count, timestamps, provider
identifiers, safe issue codes, and an immutable component plan. It excludes
paths, object keys, raw keys, database IDs, manifests, record bytes, media names,
and ciphertext/plaintext content.

A canonical private `preflight.json` uses schema
`nexa.restore-preflight.v1` and contains the same safe summary. It contains no
filesystem path.

## Failure semantics

Selection, durable retrieval, decryption, verification, compatibility,
extraction, planning, and cleanup use a sanitized `RestoreEngineError`
hierarchy. Failures do not mutate tenant data, delete the durable object, or
change a backup to corrupted merely because a preflight failed. Only the
current operation's exactly owned plaintext workspace is cleaned. Abort signals
are preserved after safe lock/evidence cleanup.

## Success lifecycle and cleanup

Phase 3A uses the short-lived retained-workspace option. A successful result
contains an opaque preflight reference held by the coordinator for Phase 3B.
`cleanup_restore_preflight()` requires the exact operation, tenant, backup, and
preflight references. It validates the complete private tree, exact file and
directory identities, single-link ownership, and the absence of unexpected
entries before deletion. Cleanup is idempotent and refuses forged context,
replacement, symlink, and hardlink ambiguity.

This reference is intentionally process-local and short-lived. Phase 3B must
acquire the same tenant lock and revalidate freshness before mutation. A future
operational release needs a bounded expiration/reaper policy for abandoned
successful preflights; Phase 3A does not add a background reaper. Long-lived or
restart-crossing consumption must retrieve, decrypt, and verify again.

## No-mutation guarantee

Phase 3A never saves imported models, bulk-creates restored rows, deletes tenant
records, replaces SQLite, writes to live `MEDIA_ROOT`, changes users,
memberships, subscriptions, or business state, or exposes an HTTP/admin restore
action. Database writes are limited to the existing safety lease rows required
for exclusive coordination.

## Capability state and checks

- `RESTORE_PREFLIGHT_ENGINE_READY=True`
- `RESTORE_MUTATION_ENGINE_READY=False`
- `OPERATIONAL_PROVIDER_STACK_READY=False`

System checks validate capability consistency plus private staging, durable
retrieval, encryption policy, and conditional KEK configuration without
creating a workspace, retrieving an object, decrypting data, or writing the
database. Backup execution remains operationally disabled pending production
KMS/object storage, destructive historical-retention approval, worker
activation, download authorization, and Phase 3B restore mutation.

## Phase 3B handoff

Phase 3B must consume only a live opaque Phase 3A reference, reacquire the
tenant-exclusive lock, revalidate the selected backup and preflight ownership,
create a fresh protected safety backup, and then implement transactional record
and media mutation plus rollback. Phase 3A grants no permission to mutate.

No deployment was performed for Phase 3A.
