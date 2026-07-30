# Nexa Backup & Restore — Phase 2E

## Scope

Phase 2E adds an internal, independent verifier for the deterministic private
plaintext package produced by Phase 2D-2. It proves package integrity,
archive/layout validity, canonical manifest and payload consistency, and the
current code-owned restore-compatibility policy. It also publishes a small,
private, canonical verification-evidence document.

Phase 2E does not:

- expose verification through HTTP, owner or Platform Admin actions, signals,
  Celery, schedules, or management commands;
- mutate a `BackupRecord` or any tenant/application database;
- run migrations;
- encrypt, upload, or durably store a package;
- restore data;
- delete the plaintext package on success or ordinary verification failure;
- enable operational backup execution; or
- deploy any code.

## Independent trust boundary

`IndependentPackageVerifier` requires the exact
`DeterministicPackageProvider` type. Its input is the immutable
`PackageVerificationRequest`, containing only:

- the complete `BackupExecutionContext`; and
- the complete `PackageBuildResult`.

No caller-supplied path is accepted. The verifier requires the package
provider to prove that the context, complete build result, and opaque
`PackageReference` exactly match provider-held publication evidence. A copied
UUID, forged context, changed result field, wrong provider identifier, or
malformed metadata fails before package bytes are trusted.

The verifier opens package bytes only through the provider's opaque API. It
does not use or recreate Phase 2D-1 component, media, manifest, or snapshot
staging.

Ordinary untrusted-input failures return a `PackageVerificationResult` with
`verified=False`, `restore_ready=False`, no verification reference, and one
bounded sanitized issue. Publication, cleanup, and verifier-state failures
raise sanitized Phase 2E engine exceptions. `KeyboardInterrupt`, `SystemExit`,
and `GeneratorExit` are preserved.

## Verification algorithm

The verifier performs these steps:

1. Validate the exact immutable request and provider-held evidence.
2. Reopen the package through its opaque reference.
3. Copy it into a bounded spooled file using 1 MiB reads while independently
   calculating the exact byte count and plaintext SHA-256.
4. Require equality with `PackageBuildResult.byte_count` and
   `PackageBuildResult.plaintext_sha256`.
5. Parse and validate the raw ZIP end records, central directory, local
   headers, entry bounds, and metadata before using Python's ZIP reader.
6. Read `manifest.json` first, parse it as strict canonical UTF-8 JSON, and
   derive the only accepted layout from that manifest.
7. Stream and independently hash every record stream, media-index stream, and
   media object.
8. Recalculate component-content, payload-set, and external manifest hashes.
9. Apply the immutable compatibility policy.
10. Close the opaque package reader and require the package provider to prove
    the exact package identity/hash evidence again.
11. Atomically publish canonical verification evidence only for a structurally
    verified package, including valid packages whose compatibility is
    `INCOMPATIBLE` or `NOT_PROVEN`.

The pre-open, open/close, independent hash, and post-close checks detect
replacement or mutation before, during, or after verification.

## ZIP safety contract

The accepted format is exactly `nexa.zip-store.v1`. Phase 2D-2 intentionally
uses `ZIP_STORED` and forced ZIP64 local headers for every entry, even for
small payloads. Phase 2E therefore requires that exact local-header form:

- extract version 4.5;
- no flags and no data descriptor;
- stored data only;
- deterministic DOS timestamp `1980-01-01 00:00:00`;
- final CRC in the local and central headers;
- `0xffffffff` local size markers;
- one exact ZIP64 local extra containing the true uncompressed and compressed
  sizes; and
- no other local extra data.

Central ZIP64 fields and the ZIP64 end record are accepted only when the
archive's size, offset, or entry count requires them. Their values, creator,
disk numbers, record sizes, and locator must match the deterministic Python
writer contract exactly. Multi-disk ZIPs are rejected.

The raw parser additionally requires:

- one end record ending at the final package byte and no archive comment;
- no trailing data;
- a central directory that ends exactly where the end-record region starts;
- sequential, non-overlapping local entries ending exactly at the central
  directory;
- no duplicate, normalized-colliding, or case-fold-colliding names;
- ASCII engine paths with no NUL, absolute prefix, drive/colon, UNC form,
  backslash, empty segment, `.` segment, `..` segment, or directory suffix;
- no entry comment;
- no unsupported flag, encryption, compression, or extra field;
- exact UNIX creator metadata and a regular file mode of `0600`;
- no symlink, directory, device, FIFO, or socket metadata;
- bounded entry count and package size; and
- compressed and uncompressed sizes equal for every entry.

After those bounds are established, Python's ZIP reader independently checks
entry metadata and CRC while each entry is streamed. Stored entries cannot
expand during decompression.

## Manifest and exact layout

`manifest.json` must:

- be UTF-8 without a BOM;
- contain exactly one final LF and no CRLF;
- reject duplicate JSON keys;
- reject floats, `NaN`, `Infinity`, and noncanonical values;
- round-trip exactly through `nexa.canonical-json.v1`;
- use the exact manifest schema/version, hash algorithm, package format, and
  payload-set schema;
- contain no whole-package hash or manifest self-hash field;
- keep source restore verification state at `NOT_VERIFIED`; and
- exactly bind the backup/tenant public UUIDs, scope, products, application
  and backup versions, migration fingerprint, minimum restore version, and
  aware created timestamp to the execution/build evidence.

The authoritative logical export registry resolves the complete component
plan for the context. Manifest component keys and model lists must exactly
match that plan.

The current Phase 2D-2 byte contract orders entries as:

1. `manifest.json`;
2. each component's `records.ndjson` followed by its
   `media-index.ndjson`, in component ordinal order; and
3. every media payload in media ordinal order.

Missing, extra, reordered, duplicated, normalized-colliding, or
case-fold-colliding paths fail verification.

## Independent payload checks

Every component record stream is read as bounded, canonical NDJSON. Phase 2E
checks:

- exact LF/count semantics;
- exact byte count and SHA-256;
- record schema, component/version, tenant public UUID, and registered model;
- canonical logical identity shape;
- registered model ownership; and
- per-model and component totals.

Every media-index stream is read as bounded canonical NDJSON. Phase 2E checks:

- exact byte count, reference count, and SHA-256;
- schema, component, model, tenant, field, identity, and storage name;
- authoritative component/model/field ownership;
- canonical public-UUID or tenant-singleton identity;
- no duplicate logical media source; and
- safe storage names with no portable collision ambiguity.

The independently parsed media-index source set must exactly equal the
manifest's ordered media source set. Distinct logical storage names remain
distinct even when their content SHA-256 values are equal.

Media payload bytes are streamed and checked against exact manifest size/hash
metadata. Phase 2E then independently recalculates:

- every component-content SHA-256;
- every manifest total;
- the ordered `nexa.backup-payload-set.v1` SHA-256;
- `PackageBuildResult.payload_set_sha256`; and
- the external manifest SHA-256.

## Compatibility and restore readiness

The code-owned `PackageCompatibilityPolicy` recognizes only the current:

- manifest schema and version;
- canonical JSON version;
- package format;
- logical record schema;
- logical media-reference schema;
- deterministic logical ordering version;
- payload-set schema;
- SHA-256 algorithm;
- authoritative component keys, component versions, dependency/order
  metadata, product owners, and restore behavior; and
- minimum restore-version comparison policy.

The outcomes are:

- `COMPATIBLE`: all mandatory package and current same-release compatibility
  checks are proven;
- `INCOMPATIBLE`: a recognized compatibility dimension conflicts with the
  authoritative registry/policy, or a numeric minimum restore version is
  newer than the captured application version; and
- `NOT_PROVEN`: the package is valid, but an ordered application/minimum
  version comparison cannot be proven from nonnumeric version tokens.

`restore_ready=True` only when package verification succeeds and compatibility
is `COMPATIBLE`.

The schema migration fingerprint must be canonical lowercase SHA-256 and must
exactly match the immutable source execution context. It is retained as
restore evidence, not treated as proof that an arbitrary future migration
graph can restore the package. Future cross-version migration adapters and
restore dry-runs remain deferred. The current `COMPATIBLE` state means the
package is ready for the next protected backup phase under the captured
same-release contract; it is not a claim that every future application schema
is compatible.

## Verification evidence

Successful structural verification publishes:

```text
<workspace>/verification/<opaque-verification-uuid>/verification.json
```

The file uses `nexa.package-verification-evidence.v1`, canonical JSON, and
exactly one final LF. It contains only:

- verifier schema/version/provider;
- verified and restore-ready booleans;
- aware verification timestamp;
- safe package format, byte count, plaintext SHA-256, and entry count;
- external manifest SHA-256;
- payload-set SHA-256;
- compatibility status; and
- bounded sanitized issue codes/messages.

It contains no filesystem path, private database ID, SQLite path, table/SQL
detail, media source path, inode/device value, internal model primary key,
stack trace, or raw exception text.

Publication creates a private `0700` directory and `0600` file where supported,
uses an exclusive temporary file plus a no-clobber hardlink publication step,
and records exact identity, size, and SHA-256 evidence behind an opaque
`VerificationReference`. Open and cleanup operations require exact
context/reference ownership. Cleanup is idempotent. A replacement, unexpected
entry, link/reparse target, or hardlink-count ambiguity is never deleted.

## Package lifecycle

On verification success, including `INCOMPATIBLE` and `NOT_PROVEN` results:

- keep the plaintext package;
- keep the verification evidence;
- do not upload;
- do not mark a durable backup record successful; and
- do not recreate Phase 2D-1 staging.

On ordinary verification failure:

- keep the original package for controlled evidence/retry;
- publish no verification evidence;
- clean only partial verification output owned by the failed publication
  attempt; and
- do not mutate durable records.

## Capability state

After Phase 2E:

```text
SQLITE_SNAPSHOT_PROVIDER_READY = True
TENANT_LOGICAL_EXPORT_PROVIDER_READY = True
MEDIA_CAPTURE_PROVIDER_READY = True
CANONICAL_MANIFEST_PROVIDER_READY = True
DETERMINISTIC_PACKAGE_PROVIDER_READY = True
INDEPENDENT_PACKAGE_VERIFIER_READY = True
OPERATIONAL_PROVIDER_STACK_READY = False
real_execution_available() = False
```

`BackupEngineCapability` and the capability consistency system check expose
and enforce this state. Operational execution remains impossible because
encryption, key management, durable private storage, and full orchestration
are not complete.

## Tests

`tests/test_backups_phase2e_verification.py` covers:

- a real Phase 2D-1 → Phase 2D-2 → Phase 2E path;
- independent package size/hash and payload verification;
- canonical opaque evidence, validation, cleanup, and retry;
- forged contexts, package references, results, and verification references;
- byte mutation, truncation, and trailing data;
- malformed/reordered/missing/extra/duplicate archives and unsafe paths;
- compression, encryption flags, timestamps, central/local metadata, and
  collision cases;
- BOM, CRLF, duplicate-key, float, schema, forbidden hash, metadata, totals,
  component digest, payload-set digest, and media-source failures;
- component-version incompatibility;
- unsupported and not-provable minimum restore versions;
- package retention on failure;
- publication abort cleanup and unowned hardlink behavior;
- preservation of process abort signals;
- exact provider types; and
- fail-closed capability/runtime surfaces.

The focused Phase 2D-1 and Phase 2D-2 suites remain regression gates.

## Residual risks and deferred work

The package and verification evidence are private local plaintext staging.
Confidentiality, authenticated encryption, key creation/rotation/recovery,
durable private object storage, retention, upload retry, disaster recovery,
restore planning, migration adapters, restore dry-runs, and end-to-end
orchestration remain future phases.

No deployment is part of Phase 2E.
