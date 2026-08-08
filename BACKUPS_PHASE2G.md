# Backup Engine Phase 2G: Durable Encrypted Storage

Phase 2G adds an internal, private durable-storage boundary for a Phase 2F
encrypted artifact. It copies ciphertext out of the ephemeral backup workspace,
independently verifies the durable bytes, and removes local encrypted staging
only after that verification succeeds. It adds no deployment, migration,
download route, retention job, schedule, or restore mutation.

## Trust boundary

The local durable provider accepts only an exact immutable
`EncryptedArtifactResult` owned by its configured Phase 2F provider. Phase 2F
first revalidates its local provider evidence, encrypted file identity, complete
artifact hash, canonical authenticated header, wrapped DEK, GCM tag, and
plaintext digest. Callers cannot supply a source path or bypass opaque provider
references.

The durable root is separate from `BACKUP_STAGING_ROOT`, `MEDIA_ROOT`,
`STATIC_ROOT`, and the repository default. It must be absolute, private,
non-overlapping, free of symlink/junction/reparse components, and confirmed as a
local filesystem when the local requirement is enabled. The local provider
stores ciphertext only. It is not public media and never generates a public URL.

## Provider and object naming

`DurableBackupStorageProvider` defines the storage lifecycle independently of a
filesystem implementation. Phase 2G implements
`LocalPrivateDurableStorageProvider`, identified by
`local-private-durable-storage-v1` with the local private filesystem backend.

The engine alone generates object names:

```text
<durable-root>/
  objects/
    <tenant-public-uuid-hex>/
      <backup-public-uuid-hex>/
        <opaque-storage-uuid-hex>/
          artifact.nxb
```

Every segment is a validated, lowercase engine literal or UUID encoding. Names
contain no business name, email, database ID, media name, local source path, or
caller-controlled text. The extra per-object directory gives the provider exact
directory ownership without preventing multiple future objects for a backup.

## Durable write and integrity verification

Writes use bounded streaming from Phase 2F's opaque encrypted reader. The
provider enforces maximum object size, chunk size, elapsed time, minimum free
capacity, and capacity headroom. It creates an exclusive private temporary file,
checks descriptor and device identity, streams without append or overwrite,
calculates byte count and SHA-256, flushes and `fsync`s the file, and performs an
atomic no-clobber hard-link publication. The temporary link is removed and the
owned directory is synchronized where the platform supports directory `fsync`.

The write-time byte count and SHA-256 must exactly match Phase 2F metadata.
After publication, the provider independently reopens and hashes the final file.
It then opens the file again through an opaque reader and asks the authoritative
Phase 2F provider to:

- parse the exact binary framing and canonical header;
- verify schema, version, algorithm, tenant UUID, and backup UUID;
- unwrap the DEK through the configured KEK provider boundary;
- authenticate the header and ciphertext/tag;
- decrypt in bounded chunks to a digest sink only; and
- match the original plaintext package byte count and SHA-256.

The durable provider never receives a raw KEK or DEK and does not duplicate the
cryptographic parser. It never persists decrypted bytes.

## Stored evidence and opaque access

`StoredBackupObjectResult` is immutable and contains only safe metadata:
opaque reference, backend/schema/provider identifiers, byte count and SHA-256,
source artifact hash, tenant and backup public UUIDs, storage timestamp,
encryption/key identifiers, local `STORED` durability state, verification state,
and encrypted-staging cleanup state. It contains no path, device, inode, file
descriptor, ciphertext, or key material.

Phase 2G keeps exact object identity and ownership evidence in provider memory,
matching the current pre-operational pipeline. A later orchestration/persistence
phase must durably record the safe result before relying on objects across
process restarts. The durable ciphertext itself survives workspace cleanup, but
this phase does not claim replicated, multi-node, or cloud durability.

`open_stored_object` returns an opaque bounded reader with `read`, `seek`, and
`tell` only. It exposes neither `.name` nor a file descriptor. Object identity,
link count, size, hash, and authenticated format are checked before and after
access.

## Cleanup, failure, and crash semantics

The encrypted staging artifact is deleted only after durable publication,
independent reopen hashing, and authenticated encrypted-format validation all
succeed. The durable object and Phase 2E evidence remain. If encrypted staging
cleanup fails, the result reports
`encrypted_staging_cleanup_incomplete=True`; the verified durable object is
kept, and an exact request/result retry can finish staging cleanup.

Before durable verification, ordinary failures preserve encrypted staging and
remove only temporary/final durable files whose identity proves they belong to
the current attempt. An unknown replacement or extra hard link prevents cleanup
and sets `cleanup_incomplete=True`; unowned content is never deleted. Errors are
sanitized and omit paths, OS details, keys, and object bytes. `KeyboardInterrupt`,
`SystemExit`, and `GeneratorExit` are preserved after best-effort exact cleanup.

Explicit `delete_stored_object` exists as an exact provider primitive for future
authorized retention work. It is not scheduled or invoked automatically in
Phase 2G. It validates ownership and content, refuses links/replacements, and is
idempotent for an exact context/reference. Historical retention policy remains
deferred.

## Idempotency

The local provider chooses recognition semantics: within a provider instance,
the same exact completed context and source artifact returns the existing
verified stored result after revalidation. It does not create a second copy.
Changed or copied evidence is rejected, and existing object paths are never
overwritten. Incomplete encrypted-staging cleanup uses the explicit retry method
rather than an ambiguous second upload.

## Future object-storage provider

The provider contract is intended for a later private AWS S3 or DigitalOcean
Spaces implementation. That provider must preserve opaque references, tenant
binding, immutable/no-clobber writes, independent checksum verification,
versioning where configured, private ACL/bucket policy, and Phase 2F
authenticated format validation. Phase 2G deliberately adds no `boto3`, remote
upload, cloud durability claim, signed URL, or public object URL.

## Download and retention boundaries

There is no download capability in Phase 2G. A future download flow requires
explicit tenant-bound authorization, controlled time-limited access, audit
logging, safe content disposition, and no storage-key or path disclosure. Public
storage URLs remain forbidden, and signed URLs are not created yet.

There is also no automatic retention deletion. Safe stored metadata provides
the tenant public UUID, backup public UUID, stored timestamp, verification state,
byte count, and opaque reference needed by a future retention policy. No five-day
or other historical deletion rule is active.

## Configuration and checks

The local provider uses:

- `BACKUP_DURABLE_STORAGE_ROOT`
- `BACKUP_DURABLE_STORAGE_CHUNK_BYTES`
- `BACKUP_DURABLE_STORAGE_MAX_OBJECT_BYTES`
- `BACKUP_DURABLE_STORAGE_TIMEOUT_SECONDS`
- `BACKUP_DURABLE_STORAGE_MIN_FREE_BYTES`
- `BACKUP_DURABLE_STORAGE_HEADROOM_MULTIPLIER`
- `BACKUP_DURABLE_STORAGE_REQUIRE_LOCAL`

Django checks validate policy bounds, absolute/root safety, local filesystem
classification, and overlap with staging/media/static roots without creating a
directory, reading ciphertext, or touching the database.

## Capability and tests

All eight internal flags through `DURABLE_STORAGE_PROVIDER_READY` are true.
`OPERATIONAL_PROVIDER_STACK_READY` and `real_execution_available()` remain
false because production KMS/object storage, orchestration, retention,
scheduling, download authorization, and restore mutation are incomplete.

Focused tests cover the real 2D-1 through 2G chain, independent durable hashing,
authenticated revalidation, opaque access, idempotency, mutation/truncation,
forgery, capacity/time bounds, unsafe roots, partial writes, no-clobber
publication, link/replacement safety, cleanup retry, exact deletion, aborts,
sanitization, capability state, and absence of runtime routes. Phase 2F, 2E, and
2D-2 regressions are run separately. No deployment is performed.
