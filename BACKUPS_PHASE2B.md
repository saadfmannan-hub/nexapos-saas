# Nexa Backup & Restore — Phase 2B

## Purpose and boundary

Phase 2B adds one internal provider: a consistent SQLite snapshot created with
SQLite's online backup API. The snapshot is temporary input for future logical
exporters.

The snapshot is a copy of the entire shared platform database. It can contain
data for every tenant, POS and WMS data, and platform-global records. It is not
tenant-scoped, not product-scoped, not owner-facing, not downloadable, and not
the final commercial backup artifact. `consistent=True` does not mean that a
recoverable backup package exists.

Phase 2B adds no model, migration, form, view, URL, admin action, Celery task,
schedule, upload, download, retention job, restore path, or production
deployment change.

## Starting architecture

Phase 2A supplied:

- frozen execution contexts and opaque workspace references;
- engine-neutral provider contracts;
- deterministic component and pipeline planning;
- a private staging-root and workspace manager;
- sanitized engine exceptions and event vocabulary;
- a code-owned full-provider-stack guard;
- a plain, disabled `execute_backup()` integration boundary.

Phase 2B reuses those boundaries. `SnapshotRequest` contains only a frozen
`BackupExecutionContext`; neither the request nor the result can carry a
database path, destination path, filename, SQL statement, credential, key, or
storage object identifier.

## Provider architecture

`SQLiteSnapshotProvider` is in
`apps/backups/engine/sqlite_snapshot.py`. Its internal flow is:

1. validate the immutable request and opaque workspace reference;
2. resolve the trusted Django database alias through
   `django.db.connections[using].settings_dict`;
3. reject non-SQLite, in-memory, URI, missing, non-regular, or link-like
   sources;
4. open a dedicated read-only SQLite source connection;
5. assess WAL, synchronous, foreign-key, schema, page, storage, and capacity
   policy;
6. reserve an engine-generated destination exclusively under the workspace's
   snapshot area;
7. call `source_connection.backup(destination_connection, ...)`;
8. close the destination after applying destination-only normalization;
9. reopen through the controlled read-only reader and perform structural
   validation;
10. recheck source schema and file identities;
11. return only an opaque UUID reference and safe bounded metadata.

The provider identifier is `sqlite-online-backup-v1`.

## Why plain file copying is forbidden

A live SQLite database can have committed pages in its WAL that are not yet in
the main database file. Copying `db.sqlite3`, copying the WAL/SHM files, or
manually combining them can produce an inconsistent or unusable result.

The provider does not use `shutil.copy*`, direct byte copying, shell copy
commands, `VACUUM INTO`, SQL text dumps, or Django `dumpdata`. It uses
`sqlite3.Connection.backup()` exclusively for database acquisition.

## Source connection and WAL policy

The source is opened through a private SQLite URI with `mode=ro` and
`cache=private`. It is not a request-thread Django connection. The provider
enables and verifies:

- `PRAGMA query_only=ON`;
- `PRAGMA foreign_keys=ON`;
- the configured approved `journal_mode`, which defaults to `WAL`;
- the configured minimum `synchronous` level, which defaults to `FULL`;
- a bounded SQLite busy timeout.

The provider reads schema version, page count, page size, and the single
attached `main` database identity. It rejects extra attached databases or a
connection that does not identify the configured source file.

It never changes the live journal mode, forces a checkpoint, truncates the
WAL, runs `VACUUM`, opens an immediate/exclusive transaction, or changes source
permissions. WAL size is read only for conservative capacity estimation.

`PRAGMA synchronous` is connection-scoped. Phase 2B verifies the value observed
by its dedicated provider connection. It does not claim to prove or configure
the value used by unrelated Django, worker, or request connections.

## Online backup and destination behavior

The destination is a separately opened SQLite connection to an exclusively
reserved `0600` file. Backup runs in bounded page steps with SQLite's busy
sleep and a monotonic deadline. A constant-size progress callback enforces the
deadline without storing row data, paths, or an unbounded progress history.

SQLite can carry WAL journal metadata into the destination and can invalidate
the destination schema cookie when an online backup replaces an already opened
destination. After backup, the provider performs two controlled,
destination-only operations:

- normalize the temporary destination to `journal_mode=DELETE`, so a completed
  snapshot does not depend on WAL/SHM sidecars;
- restore the trusted source schema version, then read it back.

Neither operation touches the live source. The provider closes the destination
and rejects any remaining `-wal`, `-shm`, or `-journal` sidecar.

## Concurrent-writer guarantees

SQLite's online backup API takes a consistent source view while allowing normal
WAL-mode writers. A committed transaction is observed atomically: related
changes are either all before or all after the snapshot boundary. Uncommitted
rows are not included. A concurrent commit may or may not be in a particular
snapshot depending on the boundary, but it cannot appear half-applied.

Phase 2B tests hold an uncommitted write transaction during acquisition and
commit a related multi-row update from a synchronized writer during backup.
The resulting snapshot remains structurally valid, and the source remains
writable afterward.

## Private workspace and path security

Snapshots use this engine-controlled form:

```text
<staging-root>/ws-<workspace-uuid-hex>/snapshot/<snapshot-uuid-hex>/snapshot.sqlite3
```

No tenant name, business name, email, label, source filename, or request value
becomes a path segment. The public contract returns only `SnapshotReference`.

The provider and workspace manager:

- reject relative and publicly served staging roots;
- reject symlinks, Windows junction/reparse points, lexical traversal, and
  resolved-path escape;
- recheck containment and file/directory identity before access and deletion;
- reject a nested storage-device boundary in the snapshot path;
- create UUID directories without overwriting an existing target;
- reserve the file with `O_CREAT | O_EXCL` and `O_NOFOLLOW` where available;
- apply `0700` to staging/workspace/snapshot directories and `0600` to the
  snapshot file where the platform supports those mode semantics;
- prevalidate exact cleanup targets and never recursively delete snapshot
  content.

On Windows, POSIX mode bits do not represent a complete ACL policy. Phase 2B
applies the strongest standard-library mode operation available and relies on
the staging root inheriting an appropriately private service-account ACL. That
deployment assumption must be verified operationally before the provider is
used.

`BACKUP_STAGING_ROOT` remains outside `MEDIA_ROOT` and `STATIC_ROOT`. No route
serves it.

## Local storage and disk headroom

The exact `snapshot` directory, not merely its parent configuration, is
assessed before destination-file creation.

The standard-library storage inspector:

- rejects UNC and confirmed remote Windows drives;
- accepts confirmed fixed Windows drives;
- reads `/proc/self/mountinfo` on Linux and rejects known NFS, CIFS/SMB, SSHFS,
  Ceph, GlusterFS, and similar network filesystems;
- accepts a defined set of local block filesystem types;
- treats unknown platforms and unknown filesystem types as unconfirmed.

When local storage is required, unconfirmed storage fails closed at runtime.
Default Django checks remain usable because they validate settings only and do
not probe or mutate the development database.

Required free capacity is:

```text
max(
    BACKUP_SQLITE_MIN_FREE_BYTES,
    ceil((page_count * page_size + current_wal_bytes)
         * BACKUP_SQLITE_HEADROOM_MULTIPLIER),
)
```

The default minimum is 1 GiB and the default multiplier is 3.0. This reserves
conservative headroom for later export, packaging, validation, and cleanup; it
does not mean those later allocations or artifacts exist.

## Structural validation

Before returning `consistent=True`, Phase 2B verifies:

- destination existence, regular-file type, identity, containment, private
  mode, and nonzero size;
- no destination WAL, SHM, or rollback-journal sidecar;
- a read-only connection using `mode=ro`;
- `query_only=1` and `foreign_keys=1`;
- destination `journal_mode=delete`;
- `PRAGMA quick_check(1)` returns exactly `ok`;
- `PRAGMA foreign_key_check` returns no violation;
- SQLite schema metadata is readable;
- page count is positive;
- page size is a valid SQLite power of two from 512 through 65536;
- file size equals `page_count * page_size`;
- snapshot schema version equals the captured source baseline;
- the live source schema version and source file identity did not change.

The same deadline covers backup and structural validation. SQLite's progress
handler cooperatively interrupts long validation statements.

`consistent=True` means only that this private, full-database temporary
snapshot passed those acquisition checks. It does not mean tenant isolation,
logical completeness, business-invariant correctness, encryption, packaging,
artifact hashing, upload, retention eligibility, restore readiness, or final
integrity verification.

## Read-only access

`open_snapshot(context=..., reference=...)` is a bounded context manager for
future trusted component exporters. It reconstructs the path from the trusted
workspace and opaque UUID. Another workspace cannot open the snapshot merely
by knowing its UUID.

The returned provider-specific reader does not expose a raw
`sqlite3.Connection` or filesystem path. It:

- opens only in SQLite read-only mode;
- enables query-only and foreign-key behavior;
- disables extension loading;
- installs an authorizer that denies write/schema actions, attach/detach, all
  setting pragmas, non-allowlisted pragmas, and path-bearing pragma virtual
  tables;
- allows only the small validation pragma vocabulary needed by this phase;
- closes strictly when the context exits.

Write attempts, writable pragmas, `ATTACH`, `PRAGMA database_list`, and
`pragma_database_list` access fail with a sanitized snapshot error.

## Cleanup and failure behavior

`cleanup_snapshot()` accepts a context and opaque reference, never a path. It
deletes only the exact engine-owned primary and exact sidecars after
containment, type, link, identity, and device checks. It removes the UUID
directory only when empty and preserves unrelated workspace content. Repeated
cleanup is idempotent.

Any ordinary creation or validation failure closes both connections and
attempts exact cleanup. A cleanup problem never converts failure into success
and does not replace the original error category; the sanitized exception
contains only a boolean `cleanup_incomplete` operator signal. Python-level
abort exceptions also trigger cleanup before being re-raised. Hard process or
host termination cannot run in-process cleanup and remains an orphan-workspace
risk for a later operational lifecycle coordinator.

Sanitized categories distinguish:

- unsupported backend;
- unsafe source;
- SQLite policy mismatch;
- unavailable workspace;
- unsafe staging filesystem;
- insufficient capacity;
- busy/locked (retryable);
- deadline timeout (retryable);
- creation failure;
- structural validation failure;
- missing opaque reference;
- explicit cleanup failure.

Raw SQLite and operating-system messages, paths, SQL, credentials, tenant
data, and stack traces do not enter result metadata or activity vocabulary.

## Settings and checks

Phase 2B adds:

| Setting | Default |
| --- | ---: |
| `BACKUP_SQLITE_REQUIRED_JOURNAL_MODE` | `WAL` |
| `BACKUP_SQLITE_REQUIRED_SYNCHRONOUS` | `FULL` |
| `BACKUP_SQLITE_BUSY_TIMEOUT_SECONDS` | `5.0` |
| `BACKUP_SQLITE_BACKUP_PAGES_PER_STEP` | `256` |
| `BACKUP_SQLITE_BACKUP_SLEEP_SECONDS` | `0.05` |
| `BACKUP_SQLITE_SNAPSHOT_TIMEOUT_SECONDS` | `300.0` |
| `BACKUP_SQLITE_MIN_FREE_BYTES` | `1073741824` |
| `BACKUP_SQLITE_HEADROOM_MULTIPLIER` | `3.0` |
| `BACKUP_SQLITE_REQUIRE_LOCAL_STAGING` | `True` |

All numeric settings are positive and bounded. Busy timeout cannot exceed the
overall deadline, and backup sleep must remain below it. `backups.E021`
reports invalid policy settings without touching SQLite.

`backups.E012` continues to report that the complete provider stack is
unavailable whenever full execution is enabled prematurely. It can appear
alongside the broker and eager-mode checks `backups.E010` and `backups.E011`.

## Capability and pipeline state

Phase 2B reports:

```text
SQLITE_SNAPSHOT_PROVIDER_READY = True
OPERATIONAL_PROVIDER_STACK_READY = False
real_execution_available() = False
```

`PREPARE_SNAPSHOT` is `PLANNED` in a normal execution plan. Planning does not
create a workspace, open SQLite, create a snapshot, transition a record, or
change integrity state. Every later operational stage remains `NOT_STARTED`.

`execute_backup()` remains an undecorated disabled function with no `.delay`,
`.apply_async`, retry policy, beat registration, or automatic caller. No HTTP
view, model save, signal, middleware, form, template, owner UI, or platform
admin UI calls the provider.

The reserved sanitized event vocabulary includes:

- `backup.snapshot_started`;
- `backup.snapshot_created`;
- `backup.snapshot_failed`;
- `backup.snapshot_validation_failed`;
- `backup.snapshot_cleaned`.

The provider does not persist those events itself and never records completion,
upload, verification, or recoverability.

## Deferred work and Phase 2C handoff

Phase 2C must implement explicitly registered, tenant-filtered logical
component exporters. Exporters may read only through the bounded snapshot
reader and must:

- enforce tenant ownership on every exported row;
- keep POS, WMS, shared, reference-only, and dependency-only semantics
  explicit;
- define deterministic ordering and component versions;
- discover media without exposing the full snapshot;
- ensure cleanup runs after every export outcome;
- never package or expose the full shared SQLite snapshot.

Still deferred are logical export, media copying, manifest serialization,
record counts, hashes, archive construction, compression, encryption, private
artifact storage, uploads/downloads, final verification, retention, scheduling,
operational worker orchestration, and all restore behavior.

Engine-neutral contracts retain opaque references rather than paths, so a
future PostgreSQL snapshot provider can use a different acquisition and reader
mechanism without changing tenant exporter contracts.

## Security assumptions and remaining risks

- The temporary snapshot is unencrypted and contains the full shared database.
  Its host, service account, staging volume, inherited Windows ACLs, and backup
  worker must be trusted before operational use.
- Local-filesystem classification is deliberately conservative but cannot
  prove every exotic storage topology. Unknown storage fails closed.
- Same-device and link/reparse checks reduce nested-mount and path-swap risk;
  filesystem checks cannot make an untrusted same-account process harmless.
- SQLite `FULL` is verified on the dedicated provider connection only.
- A hard crash can leave an opaque private workspace. Automated orphan cleanup
  is deferred and must be added before operational scheduling.
- Structural checks are not product-level or restore-level verification.
- The full provider stack and all owner-facing execution remain disabled until
  later phases close these risks.
