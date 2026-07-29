# Nexa Backup & Restore System v1.0 — Phase 2D-1

## 1. Scope and operational boundary

Phase 2D-1 adds an internal secure-local-media capture boundary and a canonical
serialized manifest boundary. It consumes only:

- an immutable `BackupExecutionContext`;
- validated `SnapshotResult` evidence;
- the complete authoritative `ComponentPlanItem` sequence;
- opaque `ComponentExportReference` values;
- validated `ComponentExportResult` metadata; and
- component bytes opened through the Phase 2C opaque reader.

It does not accept a SQLite path, reopen the deleted shared snapshot, or query live
tenant ORM data. The raw database snapshot cannot become media, a manifest field, a
planned package entry, returned metadata, an event, or an exception.

The outputs remain private plaintext staging inputs for a later phase. Phase 2D-1 does
not build an archive, compress or encrypt data, generate or wrap keys, upload data,
create download grants, verify a restore, mutate a restore target, apply retention,
schedule work, enable a Celery execution route, expose an owner or Platform Admin
action, deploy anything, or change production or persistent development data. It adds
no model and requires no migration.

This phase is not a commercial backup-completion boundary. A `BackupRecord` cannot be
marked successful on the strength of these internal outputs.

## 2. Repository findings and preserved trust boundaries

The Phase 2C logical registry explicitly classifies the five concrete media fields
eligible for backup:

- `tenants.Business.logo`;
- `catalog.Product.image`;
- `catalog.ProductVariant.image`;
- `purchases.Purchase.attachment`; and
- `expenses.Expense.attachment`.

Registry completeness compares those declarations with concrete Django
`FileField`/`ImageField` metadata. A newly added field is not automatically included; it
makes the registry check fail until it receives an explicit reviewed classification.

The existing component registry remains the authority for scope, dependencies,
component versions, restore behavior, model order, and exported fields. Phase 2D-1 does
not use model discovery as an inclusion mechanism, caller SQL, `dumpdata`, Django
serializers, or raw database identities. Component and workspace references remain
opaque UUID values, and paths remain provider-private.

The Phase 2C record and media-index streams remain canonical NDJSON. Phase 2D-1 reads
both only through `SQLiteLogicalComponentExporter.open_component_export()`, hashes their
exact bytes, and independently reconciles their counts and metadata before trusting
them.

## 3. Snapshot cutoff semantics

`SnapshotResult.consistency_cutoff_at` is an aware UTC timestamp sampled immediately
after `sqlite3.Connection.backup()` returns successfully and before destination
normalization and structural validation. `SnapshotResult.created_at` remains the later
snapshot-result creation timestamp. Phase 2D-1 requires:

```text
consistency_cutoff_at is not null
consistency_cutoff_at is timezone-aware
created_at is timezone-aware
consistency_cutoff_at <= created_at
```

The cutoff is media-consistency evidence, not a source path. A referenced media file
whose modification time is later than the cutoff is rejected. The provider also
requires a stable bounded read. This couples accepted media to the completed database
copy more accurately than the later result timestamp, but it is not a transactional
filesystem snapshot.

The snapshot provider retains exact in-process context/result evidence after raw
snapshot cleanup. Phase 2D-1 validates that evidence without reopening the deleted
SQLite file, so copying an opaque UUID onto forged timestamp metadata is rejected.

The manifest uses `SnapshotResult.created_at` as its stable
`backup.created_timestamp`; it does not sample a new wall-clock value for serialized
manifest creation.

## 4. Cross-tenant media-name isolation

For every nonempty validated media storage name found during logical export, Phase 2C
now checks every explicitly registered media field in the controlled read-only
snapshot. The check spans the whole authoritative registry, including components
outside the selected scope. If any row owned by a different tenant references the exact
name, export fails with a sanitized `CrossTenantMediaReference`.

Multiple same-tenant references are valid. Cross-tenant name sharing is never treated
as deduplication, and no row, identity, storage name, table name, SQL text, tenant name,
or internal ID is exposed in the failure.

## 5. Local-storage-only provider

Phase 2D-1 supports only the configured Django `FileSystemStorage`. The configured
backend class must be exactly `django.core.files.storage.FileSystemStorage`, and its
location must resolve exactly to the configured `MEDIA_ROOT`.

`MEDIA_ROOT` must be absolute, present, and a real directory. It and every existing
root path component must be free of symlinks, junctions, and Windows reparse points.
The provider requires local filesystem operations such as `lstat`, descriptor-based
identity checks, no-follow opens, bounded reads, and atomic private publication. It
never falls back to generic `Storage.open()`.

S3, Azure, GCS, and other object-storage backends are unsupported in this phase. A
future provider must use immutable object-version evidence appropriate to that backend;
local inode and timestamp checks must not be presented as object-storage support.

## 6. Media capture policy

The immutable policy has these defaults and fail-closed bounds:

| Setting | Default | Accepted bound |
| --- | ---: | ---: |
| `BACKUP_MEDIA_CAPTURE_CHUNK_BYTES` | `1048576` | 4 KiB–8 MiB |
| `BACKUP_MEDIA_CAPTURE_MAX_FILE_BYTES` | `67108864` | 1 byte–10 GiB |
| `BACKUP_MEDIA_CAPTURE_MAX_TOTAL_BYTES` | `4294967296` | 1 byte–10 TiB |
| `BACKUP_MEDIA_CAPTURE_MAX_OBJECTS` | `100000` | 1–1,000,000 |
| `BACKUP_MEDIA_CAPTURE_TIMEOUT_SECONDS` | `1800` | 1–86,400 seconds |
| `BACKUP_MEDIA_CAPTURE_MIN_FREE_BYTES` | `1073741824` | 1 byte–10 TiB |
| `BACKUP_MEDIA_CAPTURE_HEADROOM_MULTIPLIER` | `1.25` | 1.0–20.0 |
| `BACKUP_MEDIA_CAPTURE_REQUIRE_LOCAL_STAGING` | `true` | strict boolean |
| `BACKUP_MEDIA_INDEX_MAX_LINE_BYTES` | `65536` | 128 bytes–1 MiB |

The total-byte ceiling must be at least the per-file ceiling. Booleans are not accepted
as integers, and non-finite timeout or headroom values are rejected. Required staging
capacity is the larger of the minimum-free-byte floor and the declared capture bytes
multiplied by the headroom factor.

## 7. Strict media-index validation

`media-index.ndjson` is streamed through the opaque component API. Every record must:

- end in exactly one LF and stay within the configured line bound;
- be strict UTF-8 with no BOM or CRLF;
- be JSON with no duplicate keys and no floating-point values;
- contain exactly the v1 media-reference keys;
- use `nexa.logical-media-reference.v1`;
- match the expected component and context tenant public UUID;
- name an explicitly registered model and media field;
- use the registered logical identity shape;
- contain an independently valid logical storage name; and
- reproduce its original bytes when canonically encoded with one LF.

Blank lines, missing or extra keys, wrong identities, unknown fields, noncanonical JSON,
and duplicate exact source references fail before manifest publication.

Distinct storage names are compared using a path-segment-preserving Unicode NFKC plus
case-fold collision key with `/` as the only separator. Two different names with one
collision key are rejected; the accepted logical name itself is never rewritten.

## 8. Physical media validation and stable capture

Every unique storage name is independently revalidated and resolved only inside the
local provider. Lexical and resolved containment under the validated media root are
both required. The provider rejects:

- missing objects;
- symlinks, junctions, or reparse points in any path component;
- mount or device changes inside the media root;
- directories, FIFOs, sockets, devices, and other non-regular objects;
- link counts other than one; and
- two different logical names resolving to one physical identity.

The source is opened read-only with close-on-exec and no-follow flags where available.
Pre-open path identity must equal opened-descriptor identity, and the file must remain
on the media-root device. Before the stream, the provider records device/file identity,
size, nanosecond modification and change times, and link count. It rejects files newer
than the snapshot cutoff or over the per-file, total, object-count, capacity, or
deadline limits.

Bytes are copied and SHA-256 hashed in bounded chunks. An unbounded `read()` is not
used. The byte total must equal the initial size. After streaming, descriptor and path
identity, size, modification time, change time, and link count must all remain
unchanged. Replacement, truncation, extension, mutation, or aliasing aborts publication.
A zero-byte regular file is valid when every other rule passes.

Private media staging uses:

```text
<workspace>/media/<opaque-media-uuid>/content.bin
```

Directories use `0700` and files use `0600` where POSIX mode enforcement is available.
Temporary output uses exclusive creation, identity checks, flush and `fsync`, followed
by no-clobber atomic publication under the fixed engine-generated name.

## 9. Missing media and deduplication

The v1 policy is exact:

```text
blank or null logical field -> no media reference, valid
nonempty logical reference  -> physical object required
missing referenced object   -> entire Phase 2D-1 operation fails
```

A successful manifest always contains:

```json
{
  "missing_media_policy": "FAIL_BACKUP",
  "missing_media_count": 0
}
```

Exact repeated storage names are captured once, while every distinct logical source
reference remains in the manifest. Different names containing identical bytes remain
different media entries and future package entries. Phase 2D-1 does not use
content-hash deduplication, hardlink deduplication, or hash-only restore aliasing.

## 10. Component reconciliation

For each component in authoritative plan order, both component streams are opened and
streamed. Phase 2D-1 independently calculates exact byte counts and SHA-256 hashes.

For `records.ndjson`, LF count must equal `row_count`; zero records require zero bytes,
and a nonempty stream must end with LF. The byte total must match `byte_count`.

For `media-index.ndjson`, validated line count must match `media_count`, and exact bytes
must match `media_index_byte_count`. Model labels must exactly preserve registered model
order, model counts must total the row count, and component key/version, provider,
record schema, media-reference schema, and deterministic-ordering version must match
authoritative values. References must be unique, timestamps aware, and numeric metadata
nonnegative and bounded.

Each component has a `nexa.component-content-digest.v1` digest over canonical descriptor
bytes without a trailing LF. The descriptor domain-separates the component identity and
version, both stream schemas/counts/sizes/hashes, ordering version, and ordered model
counts. It is not an ambiguous concatenation of strings.

## 11. Canonical manifest schema

The single manifest is canonical UTF-8 JSON with sorted keys, compact separators, no
BOM, no NaN or Infinity, no generic string fallback, and exactly one final LF. Its
top-level identifiers are:

```text
schema                  = nexa.backup-manifest.v1
manifest_version        = 1.0.0
canonical_json_version  = nexa.canonical-json.v1
hash_algorithm          = sha256
package_format          = nexa.zip-store.v1
payload_set_schema      = nexa.backup-payload-set.v1
```

The exact top-level members are:

```json
{
  "schema": "nexa.backup-manifest.v1",
  "manifest_version": "1.0.0",
  "canonical_json_version": "nexa.canonical-json.v1",
  "hash_algorithm": "sha256",
  "package_format": "nexa.zip-store.v1",
  "backup": {},
  "compatibility": {},
  "source_consistency": {},
  "components": [],
  "media": [],
  "totals": {},
  "payload_set_schema": "nexa.backup-payload-set.v1",
  "payload_set_sha256": "",
  "missing_media_policy": "FAIL_BACKUP",
  "missing_media_count": 0,
  "restore_verification_state": "NOT_VERIFIED"
}
```

`backup` carries the backup and tenant public UUIDs, scope, trigger, included products
and component keys, application and format versions, migration fingerprint, minimum
restore version, and stable snapshot-created timestamp. `compatibility` repeats the
minimum restore version and declares `status=NOT_CHECKED` and
`database_engine_neutral=true`.

`source_consistency` declares a consistent database snapshot, its creation and cutoff
timestamps, the logical-export provider, record/media schemas, ordering version, and
`media_capture_policy=SNAPSHOT_CUTOFF_AND_STABLE_READ_V1`. It contains no database path,
table name, PRAGMA result, schema SQL, or primary key.

Each component entry preserves authoritative component metadata and model order. Its
record and media-index objects contain package path, count, byte count, and exact stream
SHA-256. It also carries the component-content digest schema/hash and
`restore_verification_state=NOT_VERIFIED`.

Each media entry contains its ordinal, unchanged storage name, generated package path,
byte count, SHA-256, source-reference count, ordered logical sources,
`capture_state=CAPTURED_AND_HASHED`, and
`restore_verification_state=NOT_VERIFIED`. A source contains exactly `component`,
`model`, `identity`, and `field`. Media entries do not expose opaque capture UUIDs,
paths, timestamps, modes, device/inode identities, or other operating-system metadata.

Totals cover component/model/record counts, media-reference and unique-media counts,
both component-stream byte totals, media bytes, and planned payload bytes. Planned
payload bytes exclude manifest bytes and future ZIP framing.

The private serialized layout is:

```text
<workspace>/manifest/<opaque-manifest-uuid>/manifest.json
```

The provider publishes atomically, verifies exact directory contents, identity, size,
and hash, returns only an opaque reference and bounded safe metadata, and supports exact
idempotent cleanup.

## 12. Hash domains and self-hash prohibition

SHA-256 domains are deliberately separate:

- each record stream hashes its exact NDJSON bytes;
- each media-index stream hashes its exact NDJSON bytes;
- each captured object hashes its exact source bytes;
- each component-content hash covers its canonical
  `nexa.component-content-digest.v1` descriptor without LF;
- `payload_set_sha256` covers the canonical
  `nexa.backup-payload-set.v1` descriptor without LF; and
- the externally returned manifest hash covers exact `manifest.json` bytes, including
  its final LF.

The payload-set descriptor lists, in deterministic order, every component record
stream, component media-index stream, and media payload with its generated package
path, byte count, and SHA-256; media descriptors also retain the storage name. It does
not cover `manifest.json`.

`manifest_sha256` is not serialized inside the manifest. A future whole-package hash is
also prohibited inside the manifest because either would create a circular self-hash.
The package hash is Phase 2D-2 metadata returned outside the future package.

## 13. Future package layout

Phase 2D-1 plans but does not create the `nexa.zip-store.v1` package:

```text
manifest.json
components/{component_ordinal:04d}/records.ndjson
components/{component_ordinal:04d}/media-index.ndjson
media/{media_ordinal:08d}.bin
```

Component ordinals start at one in authoritative plan order. Media ordinals start at
one in exact storage-name lexical order. Only engine-generated ASCII paths are used;
component keys and storage names never become path segments. Exact, normalized, or
case-folded package-path collisions are rejected.

## 14. Coordination and cleanup ownership

The internal coordinator validates component evidence, hashes and reconciles streams,
strictly parses media indexes, captures media, and builds the canonical manifest. It
requires authoritative provider types and the complete validated component plan, and
rejects duplicate or forged references and metadata.

On failure or abort, it attempts cleanup in reverse ownership order: partial or
published manifest output, newly captured media, then all supplied component exports
owned by the operation. Component cleanup uses provider-held context, directory, file
identity, size, and partial-cleanup evidence; unexpected replacements remain untouched
and set the incomplete-cleanup state. It preserves `KeyboardInterrupt`, `SystemExit`, and
`GeneratorExit` after cleanup. Ordinary errors cross the engine boundary only as
sanitized exception categories, with `cleanup_incomplete=True` when exact cleanup
cannot be proven.

On success, component exports, captured media, and the canonical manifest remain in
private plaintext staging because Phase 2D-2 needs them. Phase 2D-2 owns their cleanup
after successful atomic package publication. A hard process or host termination can
still leave an orphan workspace; Phase 2D-1 does not claim that plaintext or orphan
staging risks are resolved.

## 15. Capability state and non-mutating checks

The required code-owned state is:

```text
SQLITE_SNAPSHOT_PROVIDER_READY=True
TENANT_LOGICAL_EXPORT_PROVIDER_READY=True
MEDIA_CAPTURE_PROVIDER_READY=True
CANONICAL_MANIFEST_PROVIDER_READY=True
OPERATIONAL_PROVIDER_STACK_READY=False
real_execution_available()=False
```

No setting can make the incomplete operational stack available. Snapshot, logical
export, secure local capture, and canonical manifest providers are internal
capabilities only. Deterministic package construction, independent verification,
encryption, durable private storage, and operational orchestration remain incomplete.

Phase 2D-1 adds these checks:

- `backups.E024`: invalid bounded media-capture policy;
- `backups.E025`: unsupported backend or unsafe/missing local media root; and
- `backups.E026`: inconsistent provider-capability state.

`backups.E020` also requires complete separation in both directions between private
staging and configured public media/static roots. The checks inspect only configuration
and root metadata. They do not enumerate or open tenant media, create a workspace or
snapshot, publish a manifest, query tenant records, or mutate data.

## 16. Residual risks and deferred work

- Captured media, components, and the manifest are plaintext until a later phase
  packages and encrypts them.
- Ordinary path, descriptor, link, identity, size, and timestamp checks narrow
  filesystem races but cannot defeat a privileged malicious actor running as the same
  operating-system account. That actor may still win a TOCTOU race or preserve
  misleading timestamps.
- On Windows, POSIX `0700`/`0600` modes do not establish the full privacy boundary.
  Privacy still depends on a correctly restricted inherited service-account ACL.
- A filesystem modification time is evidence, not a transactional cross-resource
  commit protocol. Media newer than the cutoff fails, while older media is accepted only
  after a stable read.
- Durable private object storage requires a separate version-aware provider.
- Component/media/manifest integrity is not independent verification of a final
  artifact or a restore.
- Restore compatibility remains `NOT_CHECKED`, and restore verification remains
  `NOT_VERIFIED`.
- Archive construction, final package hashing, verification, compression, encryption,
  key management, upload, retention, download authorization, restore, reaping,
  scheduling, and operational worker lifecycle are deferred.

No deployment or operational execution occurs in Phase 2D-1.
