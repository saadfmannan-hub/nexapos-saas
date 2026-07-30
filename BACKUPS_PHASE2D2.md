# Nexa Backup Engine — Phase 2D-2

## Deterministic package construction, plaintext package hash, and successful staging cleanup

Phase 2D-2 converts the complete, validated Phase 2D-1 staging set into one
private deterministic plaintext package. It also transfers cleanup ownership:
only after the final package is atomically published and revalidated may the
Phase 2D-1 component exports, media captures, and canonical manifest be removed.

This remains an internal engine capability. It does not enable manual backups,
schedules, downloads, upload, retention, restore, or any HTTP/Celery execution
surface.

## 1. Scope

Implemented:

- deterministic `nexa.zip-store.v1` package construction;
- exact package entry order and engine-generated package paths;
- `ZIP_STORED` payloads with fixed ZIP metadata;
- stream-by-stream size and SHA-256 reconciliation against `manifest.json`;
- post-write archive verification before publication;
- exact whole-package plaintext SHA-256 returned outside the manifest;
- private atomic no-clobber publication using a hard link;
- opaque package references, open, evidence validation, and exact cleanup;
- successful Phase 2D-1 plaintext staging cleanup after confirmed publication;
- exhaustive reverse-order cleanup with incomplete-cleanup evidence; and
- fail-closed capability reporting while operational execution remains disabled.

Deferred:

- independent verification as a separate trust boundary;
- encryption and key management;
- durable private object storage and upload;
- retention and reaping;
- authorized download;
- restore compatibility and restore verification;
- scheduled execution and dedicated worker lifecycle; and
- production deployment or operational execution.

## 2. Input boundary

`DeterministicPackageProvider` accepts only:

- an exact `BackupExecutionContext` with its opaque workspace reference; and
- an exact `Phase2D1Result` containing authoritative component-export, media-
  capture, and canonical-manifest references.

Caller-supplied filesystem paths are never accepted. The provider opens source
content only through the authoritative Phase 2C and Phase 2D-1 provider APIs.
The deleted SQLite snapshot is never reopened.

The provider constructor requires the exact authoritative provider types and a
single shared private workspace root. Forged provider stacks, references,
metadata, context binding, duplicate references, or mismatched roots fail
closed.

## 3. Package layout and order

The exact entry order is:

```text
manifest.json
components/0001/records.ndjson
components/0001/media-index.ndjson
components/0002/records.ndjson
components/0002/media-index.ndjson
...
media/00000001.bin
media/00000002.bin
...
```

Component ordinals are the authoritative manifest/component-plan order. Media
ordinals are the Phase 2D-1 lexical storage-name order. Component keys and
logical storage names never become path segments.

Only ASCII engine-generated paths are accepted. Traversal, absolute paths,
backslashes, empty segments, duplicate paths, and case-fold collisions are
rejected.

## 4. Deterministic ZIP profile

The package format identifier remains:

```text
nexa.zip-store.v1
```

Every entry uses:

- `ZIP_STORED` with no compression;
- fixed DOS timestamp `1980-01-01 00:00:00`;
- Unix creator metadata;
- regular-file mode `0600`;
- no entry comment;
- no archive comment;
- deterministic entry order; and
- ZIP64-capable streaming writes.

No package UUID, local path, runtime clock, process identifier, or random
publication name is serialized inside the ZIP. Therefore two builds from the
same immutable Phase 2D-1 inputs produce identical package bytes even though
they receive different opaque package references.

## 5. Manifest and payload reconciliation

Before writing, the provider:

1. reads the canonical manifest through its opaque provider reference;
2. verifies its exact byte count and SHA-256;
3. strictly parses canonical UTF-8 JSON with duplicate-key and non-integer
   number rejection;
4. requires canonical re-encoding with one final LF to reproduce the exact
   bytes;
5. validates backup identity, tenant identity, scope, products, versions,
   timestamp, format, payload-set hash, totals, entry paths, ordinals, and
   provider metadata; and
6. binds every manifest payload descriptor to the supplied authoritative
   component or media result.

During package construction, every source is streamed and independently
reconciled against its manifest byte count and SHA-256. A missing, replaced,
truncated, extended, or changed source aborts package construction.

After writing, the provider reopens the temporary archive and verifies:

- exact entry names and order;
- no duplicates;
- fixed ZIP metadata;
- stored sizes;
- no encryption flag;
- exact content byte counts; and
- exact content SHA-256 for every entry.

## 6. Plaintext package hash domain

The package SHA-256 covers the exact final plaintext ZIP bytes:

```text
plaintext_sha256 = SHA256(exact package.zip bytes)
```

It is returned in `PackageBuildResult` with the package byte count, entry count,
payload-set SHA-256, format identifier, creation timestamp, and provider
identifier.

The whole-package hash is not serialized into `manifest.json`. This preserves
the Phase 2D-1 self-hash prohibition and avoids a circular hash domain.
Encryption will create a later, separate ciphertext/artifact hash domain.

## 7. Private atomic publication

A package is built in:

```text
<workspace>/package/<opaque-package-uuid>/.package.zip.<random>.part
```

The provider creates private `0700` directories and a private `0600` regular
file, writes and fsyncs the archive, verifies it, then publishes without
clobbering by hard-linking the owned inode to:

```text
<workspace>/package/<opaque-package-uuid>/package.zip
```

The temporary link is removed only after both names are proven to reference the
same owned inode with the expected link count. The final package is then
rehashed and enrolled as provider-held evidence.

Publication never uses caller-selected names and never overwrites an existing
object.

## 8. Failure rollback ownership

Before a result is returned, any package failure attempts exact cleanup of only
the package provider's owned inode links and empty package directory.

Rollback validates:

- file type;
- no link/reparse object;
- exact device/inode identity; and
- exact hard-link count matching all known provider-owned aliases.

If an unexpected external hard link or replacement exists, rollback refuses to
unlink any ambiguous name, preserves the data, and returns a sanitized error
with `cleanup_incomplete=True`. Unowned files and directories are never swept.

`KeyboardInterrupt`, `SystemExit`, and `GeneratorExit` are preserved after
rollback attempts. Ordinary failures cross the engine boundary only through
sanitized package exception categories.

## 9. Successful Phase 2D-1 staging cleanup

`Phase2D2Coordinator` cleans upstream plaintext only after:

1. `build_package` returns an exact `PackageBuildResult`; and
2. the package provider independently validates its enrolled package evidence.

A failed build, forged result, or failed package validation leaves all Phase
2D-1 staging intact.

After confirmed publication, cleanup is attempted exhaustively in reverse
ownership order:

1. canonical manifest;
2. media captures in reverse order; and
3. component exports in reverse order with exact evidence required.

One cleanup failure does not prevent attempts for the remaining inputs. If any
cleanup cannot be proven complete, the package remains available and the
coordinator raises `SuccessfulStagingCleanupError` with
`cleanup_incomplete=True`.

The package is revalidated after upstream cleanup and immediately before the
Phase 2D-2 result is returned.

## 10. Package lifecycle

The package provider supports:

- `validate_package_evidence` — exact context/result/provider-state binding and
  whole-package SHA-256 validation;
- `open_package` — opaque bounded reader without exposing a path or file
  descriptor; and
- `cleanup_package` — exact, idempotent deletion with context-bound tombstone
  evidence.

Package cleanup refuses forged context, replaced files, extra directory
contents, links/reparse points, inode changes, size changes, or hash changes.

## 11. Bounded resource policy

Phase 2D-2 uses code-owned fail-closed bounds:

- 1 MiB streaming chunks;
- 10 TiB maximum plaintext package bytes;
- 200,000 maximum ZIP entries;
- 1 GiB maximum canonical manifest bytes;
- 1,800-second package deadline;
- 1 GiB minimum free space; and
- 1.25 capacity headroom multiplier.

The upper-bound capacity estimate includes payload bytes, manifest bytes, and
bounded ZIP metadata overhead. Package construction remains private local
staging work and is not operationally callable.

## 12. Capability state

The required state is:

```text
SQLITE_SNAPSHOT_PROVIDER_READY=True
TENANT_LOGICAL_EXPORT_PROVIDER_READY=True
MEDIA_CAPTURE_PROVIDER_READY=True
CANONICAL_MANIFEST_PROVIDER_READY=True
DETERMINISTIC_PACKAGE_PROVIDER_READY=True
OPERATIONAL_PROVIDER_STACK_READY=False
real_execution_available()=False
```

`backups.E026` now includes deterministic-package readiness in the internal
capability consistency check. No setting can make the incomplete operational
stack available.

## 13. Residual risks

- `package.zip` is plaintext until encryption is implemented.
- A privileged malicious actor running as the same operating-system account may
  still win filesystem races despite descriptor, identity, link-count, mode,
  and hash checks.
- On Windows, POSIX modes do not establish the full privacy boundary; inherited
  service-account ACLs remain required.
- Whole-package SHA-256 validation is not the later independent verification
  trust boundary.
- The package is not durable until private object storage and upload exist.
- Restore compatibility remains `NOT_CHECKED`; restore verification remains
  `NOT_VERIFIED`.
- Hard process or host termination may still leave orphan package or upstream
  staging that a later safe reaper must reconcile.

No deployment or operational execution occurs in Phase 2D-2.
