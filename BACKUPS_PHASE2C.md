# Nexa Backup & Restore System v1.0 — Phase 2C

## 1. Purpose and strict boundary

Phase 2C implements an internal tenant-scoped logical export engine. It consumes an
opaque, structurally validated Phase 2B SQLite snapshot reference and streams only
explicitly registered tenant records into private, canonical NDJSON component files.

Phase 2C output is internal staging data. It is not:

- a final backup artifact;
- downloadable;
- restore-ready;
- compressed or encrypted;
- uploaded or retained;
- verified for restore;
- a reason to mark a `BackupRecord` successful; or
- permission to enable backup execution.

Operational row values come only from the immutable SQLite snapshot reader. Django
model metadata is used only as trusted schema metadata. The exporter does not use live
operational ORM querysets, `dumpdata`, Django serializers, caller-supplied SQL, or
automatic model discovery as an inclusion mechanism.

No owner-facing or Platform Admin action, HTTP view, model signal, schedule, or Celery
task invokes the exporter. Phase 2C adds no database model and requires no migration.

## 2. Starting Phase 2B architecture

Phase 2C starts from the committed Phase 2B foundation at
`8e422de642ca921d684b6295a2bdbc05852e3cc7`.

Phase 2B already provides:

- immutable `BackupExecutionContext` values with the authoritative internal tenant ID
  and immutable tenant public UUID;
- opaque workspace and snapshot references;
- a private staging root and engine-owned workspace areas;
- an online `sqlite3.Connection.backup()` provider;
- WAL and durability-policy validation;
- local-storage, capacity, containment, link/reparse, file-identity, and private-mode
  checks;
- a read-only snapshot facade with `query_only`, extension loading disabled, and a
  restrictive SQLite authorizer;
- bounded snapshot deadlines and sanitized failure categories;
- exact, idempotent snapshot cleanup; and
- an immutable component plan while the operational provider stack remains disabled.

Phase 2C extends the read-only facade with bounded `fetchmany()` streaming and builds a
logical exporter on that existing trust boundary. It does not weaken or bypass the
Phase 2B snapshot policy.

## 3. Why the full SQLite snapshot is never a tenant artifact

The SQLite source is a shared platform database. A complete snapshot can contain data
for other tenants, platform-global users, subscriptions, audit evidence, backup
control-plane records, and authentication metadata. It is therefore only a short-lived
internal source for tenant filtering.

The raw snapshot:

- is never returned as a component result;
- is never written to a logical record or media index;
- is never added to a manifest or package;
- is never exposed through a path;
- is never downloadable or stored as a tenant object; and
- must be removed by the batch coordinator before a successful logical-export result
  can be returned.

Only the tenant-filtered logical component outputs may survive a successful Phase 2C
batch for later internal phases.

## 4. Explicit logical-export registry

`apps/backups/engine/logical_export_registry.py` owns an immutable, fail-closed registry.
The reviewed specifications are split by domain:

- `logical_export_specs_shared.py`;
- `logical_export_specs_pos.py`; and
- `logical_export_specs_wms.py`.

Each `ModelExportSpec` explicitly declares:

- canonical model label and component key;
- identity kind and identity field;
- direct ownership field;
- scalar fields and any field-specific scalar policy;
- foreign-key and one-to-one reference policies;
- many-to-many policies;
- JSON transformation policies;
- media fields;
- reviewed omissions;
- optional cross-record validators;
- model schema version; and
- export eligibility.

`LOGICAL_MODEL_VERSION` is `1.0.0`.

Registry validation fails closed unless:

- every model in every scope-eligible, non-`NON_RESTORABLE` component has exactly one
  logical specification;
- no unknown, duplicate, or ineligible model specification exists;
- every concrete field is classified exactly once;
- every local many-to-many field is classified exactly once;
- all declared relations match Django metadata, nullability, and target model;
- all media fields are real `FileField`/`ImageField` values;
- logical media declarations exactly match component-registry media metadata;
- suspicious field names are explicitly omitted;
- identities and ownership fields have the required types; and
- JSON, scalar-policy, validator, component-version, dependency, and model-order
  metadata are valid.

The component registry also rejects duplicate dependencies, non-canonical labels,
invalid versions, unknown dependencies, self-dependencies, and dependency cycles. A
supplied `ComponentPlanItem` must exactly match the registered key, product owner,
version, restore behavior, dependencies, and export/import order.

Completeness inspection reports mistakes; it never turns model discovery into automatic
export inclusion.

## 5. Exact logical record and media schemas

The record schema identifier is `nexa.logical-record.v1`.

Each line in `records.ndjson` is equivalent to:

```json
{
  "schema": "nexa.logical-record.v1",
  "component": "pos.sales",
  "component_version": "1.0.0",
  "model": "sales.Sale",
  "tenant_public_id": "00000000-0000-0000-0000-000000000000",
  "identity": {
    "public_id": "00000000-0000-0000-0000-000000000000"
  },
  "fields": {}
}
```

Tenant-singleton identity, currently used by `tenants.BusinessSettings`, is:

```json
{
  "singleton_model": "tenants.BusinessSettings",
  "tenant_public_id": "00000000-0000-0000-0000-000000000000"
}
```

The media-reference schema identifier is `nexa.logical-media-reference.v1`.
Each line in `media-index.ndjson` is equivalent to:

```json
{
  "schema": "nexa.logical-media-reference.v1",
  "component": "shared.tenant_identity",
  "model": "tenants.Business",
  "tenant_public_id": "00000000-0000-0000-0000-000000000000",
  "identity": {
    "public_id": "00000000-0000-0000-0000-000000000000"
  },
  "field": "logo",
  "storage_name": "business_logos/example.png"
}
```

Both streams use canonical UTF-8, no BOM, compact separators, lexicographically sorted
JSON keys, no NaN or Infinity, LF line endings, no trailing whitespace, and exactly one
LF after every record. An empty component stream is a zero-byte file. Neither schema
contains database table names, SQL, PRAGMA data, internal primary keys, raw foreign-key
IDs, paths, Python representations, or SQLite-specific types.

The deterministic ordering identifier is `nexa.logical-order.v1`.

## 6. Canonical scalar and JSON rules

The serializer has no generic `str()`, `repr()`, pickle, `model_to_dict`, Django
serializer, or `json.dumps(default=str)` fallback.

Supported values are encoded as follows:

- UUID: parsed and emitted as lowercase canonical hyphenated text.
- Decimal: emitted as a JSON string in fixed-point notation at the Django field's
  declared decimal scale; float input, exponents in output, rounding, NaN, and Infinity
  are rejected. Exact negative zero is normalized to positive zero, and the canonical
  decimal representation is bounded to 64 digits.
- Date: `YYYY-MM-DD`.
- DateTime: UTC RFC 3339 with six microsecond digits and a trailing `Z`. A naive value
  read from Django's SQLite representation is treated as UTC; aware values are converted
  to UTC.
- Time: deterministic six-digit microsecond ISO text. Timezone-aware time values are
  rejected.
- Boolean: JSON `true` or `false`; non-boolean integer values other than `0` and `1` are
  rejected for Boolean fields, and floats or strings that merely compare equal to `0`
  or `1` are rejected.
- Integer: JSON integer only for reviewed business scalar fields; booleans, floats,
  numeric strings, and values that would require coercion or truncation are rejected.
- Character, text, email, IP, slug, and URL-derived character fields: strict JSON strings.
  Valid Unicode code points are preserved byte-for-byte rather than NFC/NFKC-normalized;
  lone surrogates are rejected and JSON control characters are escaped canonically.
- Null: JSON `null` only for nullable fields.
- Blank non-null strings: preserved as `""`.

Canonical JSON recursively:

- sorts dictionary keys lexicographically;
- requires string dictionary keys;
- preserves list order unless the field explicitly uses `SORTED_STRING_SET`;
- rejects unsupported Python values;
- rejects NaN and Infinity;
- parses JSON decimal literals without binary floating-point loss and emits their exact,
  normalized JSON number value without a generic fallback;
- rejects duplicate object keys and direct binary-float values;
- rejects raw-database-identity key forms across snake case, camel case, and
  punctuation-separated forms unless a future explicit transformer handles them;
- enforces the configured maximum depth; and
- remains subject to component byte and deadline limits.

Canonical JSON also fails closed above 64 KiB of encoded input, 4,096 total nodes,
1,024 members in one container, 16 KiB in one string, or 64 KiB of aggregate string
data. These are format-compatibility limits, not tunable discovery behavior.

`SORTED_STRING_SET` requires a duplicate-free list of nonempty strings, verifies any
declared allowlist, and emits the values in lexical order. It is used for permission
collections. JSON fields that could hide raw database IDs are not accepted unless their
model specification gives them an explicit reviewed policy.

The four domain JSON shapes are locked explicitly:

- `catalog.ProductVariant.attributes` is a flat string-to-string attribute map;
- `customers.Customer.more_options` is a string map limited to keys `1` through `20`;
- `registers.Shift.denominations` is null or a map of positive canonical denomination
  names to nonnegative integer/exact-decimal counts; and
- `sales.SaleItem.tailoring_details` is a flat string map limited to
  `design_type`, `daraz_details`, `vip_3d_design`, `computer_design`,
  `customer_notes`, and `workshop_notes`.

Hidden-ID detection is recursive and case/separator/camel-case aware, including `id`,
`_id`, `pk`, `primary_key`, and reviewed tenant-entity ID variants.

## 7. Tenant ownership enforcement

Before writing rows for every component, including a direct single-component call, the
exporter independently reads `tenants.Business` from the same snapshot and requires the
context's internal business ID to map to its public UUID. The tenant-identity component
then selects `tenants.Business` by both:

- `id == context.business_id`; and
- `public_id == context.business_public_id`.

It must produce exactly one row. A missing row or mismatch is a
`TenantIsolationViolation`, not an empty export.

Every other exported tenant model is queried with its explicit ownership column equal
to `context.business_id`. Tenant ownership is carried in the record-level
`tenant_public_id`; the internal ownership foreign key is classified as the ownership
selector and is not serialized into `fields`.

For every tenant-owned reference, the exporter separately reads the target from the same
snapshot and verifies the target's direct ownership column. It does not rely on a parent
relation, Django form validation, `clean()`, an ORM manager, or the source row's tenant
column to imply target ownership.

A missing or cross-tenant Branch, Warehouse, Role, Customer, Supplier, Product,
employee, order, assignment, or other tenant target aborts the component with a
sanitized isolation error. It is never skipped or repaired. The WMS salary piece-line
validator additionally verifies that the stored assignment public-UUID snapshot agrees
with the assignment reached through its production line.

`inventory.StockMovement.reference_id` remains a business document reference, but is
not trusted as arbitrary text. Its reviewed `reference_type` maps to the matching
same-tenant transfer, adjustment, count, sale/void, sale return, purchase, or purchase
return natural-number field and must resolve exactly once. Opening/import references
must be blank; unknown types and raw unmatched identifiers fail closed.

## 8. Foreign keys and global User references

Tenant-owned and global foreign keys are serialized as:

```json
{
  "model": "branches.Branch",
  "public_id": "00000000-0000-0000-0000-000000000000"
}
```

Nullable references are emitted as JSON `null`.

`accounts.User` is platform-global and has no logical model-export specification. A User
foreign key is resolved from the snapshot only far enough to obtain its immutable public
UUID. The exporter never emits or traverses User email, phone, full name, password hash,
login state, failed-login counters, lock state, platform/staff/superuser flags, groups,
permissions, sessions, or login history.

Tenant-owned relation targets must also be represented by an export specification in the
validated component plan. `accounts.User` and the selected `tenants.Business` are the
only allowed special reference targets outside normal tenant-component dependency
resolution.

## 9. Many-to-many behavior

Every exported many-to-many field must be explicitly registered. The current shared
example is `accounts.Membership.branches`.

Membership branches are read through the trusted automatic through-model metadata, but
the through-table primary key and raw source/target IDs are never serialized. Every
Branch target must belong directly to the selected tenant. References are deduplicated
and ordered by Branch public UUID:

```json
[
  {
    "model": "branches.Branch",
    "public_id": "00000000-0000-0000-0000-000000000001"
  }
]
```

An empty list remains `[]`; in the Membership domain it continues to mean access to all
branches. A missing or cross-tenant M2M target fails the export.

## 10. POS, WMS, and ALL_ENABLED isolation

The exporter accepts only the authoritative resolved component plan and revalidates it
against the current component registry and immutable execution context.

- POS scope contains eligible shared components and POS components only.
- WMS scope contains eligible shared components and WMS components only.
- `ALL_ENABLED` contains shared components plus only the products listed in the
  context's resolved entitlements.
- Combined plans retain deterministic shared, POS, then WMS ordering.
- Explicit component requests must remain within entitlement and dependency closure.
- Unknown components, missing dependencies, duplicates, forged plan metadata, invalid
  order, and circular dependencies fail closed.

Direct component export may accept a dependency-closed authoritative subset. Batch
orchestration requires the complete registry-resolved plan and rejects partial batches
before creating component output.

Referenced tenant model components must be present in the plan. Scope-excluded POS or WMS
models cannot be reached by automatic discovery because no such discovery drives export.

## 11. Restore-behavior semantics

Phase 2C preserves component behavior without flattening it:

- `REFERENCE_ONLY`: tenant identity and access-control records may be exported as
  dependency evidence, but are not authority for a future importer to mutate platform
  tenant or security objects.
- `DEPENDENCY_ONLY`: locations and tenant settings are exported so product data can be
  interpreted with its dependencies; they are not silently reclassified as normal
  replaceable product records.
- `REPLACEABLE`: eligible POS and WMS operational component records use the registered
  replaceable semantics for a future restore design.
- `NON_RESTORABLE`: subscription control, audit/backup evidence, notifications, and
  transient sales are scope-ineligible and receive no logical records.

These labels are compatibility and restore-policy metadata. Phase 2C itself performs no
restore.

## 12. HeldSale policy

`sales.HeldSale` is a parked-cart record whose `cart` JSON carries transient live
database IDs. It has been removed from `pos.sales` and explicitly classified in
`pos.transient_sales`, which is `NON_RESTORABLE` and has no eligible scope.

Phase 2C has no `HeldSale` logical specification, never selects a HeldSale row, and never
serializes its `cart` JSON.

## 13. Sale.checkout_token policy

`sales.Sale.checkout_token` is a destination-local idempotency/retry token. Its explicit
policy is `DESTINATION_LOCAL_TOKEN`.

The field is omitted from the logical query and record. A future importer must leave it
null or generate a new destination-local token. The raw value must not appear in record
bytes, media-index bytes, result metadata, events, or sanitized errors.

## 14. Media discovery only

Phase 2C classifies these eligible media fields:

- `tenants.Business.logo`;
- `catalog.Product.image`;
- `catalog.ProductVariant.image`;
- `purchases.Purchase.attachment`; and
- `expenses.Expense.attachment`.

For a nonempty value, the exporter validates only the storage-relative name, writes that
name to the logical field, and adds one deterministic media-index record. Exact repeated
references are deduplicated by component, model, record identity, field, and storage
name. Empty media values remain `""`, null media values remain JSON `null`, and neither
produces a media-index row.

Rejected names include:

- absolute, drive-qualified, UNC, or leading-slash paths;
- backslashes;
- percent signs and therefore percent-encoded traversal, separators, and schemes;
- NUL;
- URL schemes;
- Windows-forbidden punctuation, control/format/surrogate/line-separator characters,
  Windows device-name segments, and segments with trailing dots or spaces;
- query strings and fragments;
- empty, `.`, or `..` path segments;
- Unicode slash/reverse-slash lookalikes and blank or Unicode-whitespace-only names;
- names outside the configured character or UTF-8 byte limit; and
- names that are not strict UTF-8 strings.

Phase 2C never resolves `MEDIA_ROOT`, opens a storage backend or `FieldFile`, stats a
media file, follows a media symlink, reads or copies media bytes, detects content type,
or hashes media. Physical existence is deliberately deferred.

## 15. Deterministic ordering

For one immutable snapshot and one component/version:

1. components follow the validated topological export plan, with registered export order
   as the deterministic tie-breaker;
2. models follow `included_model_labels`;
3. public-UUID models are ordered by public UUID, never by internal primary key;
4. tenant singletons produce exactly one deterministic row;
5. M2M references are ordered by target public UUID;
6. media rows follow component, model, logical-record, declared-field, and validated-name
   discovery order, with exact duplicates removed; and
7. JSON object keys and final encoded object keys are lexicographically sorted.

Equivalent logical data with the same public identities and values therefore does not
depend on physical insertion order.

## 16. Snapshot streaming and resource limits

`SQLiteSnapshotReader.iter_query()` is the narrow streaming API. It:

- keeps the raw SQLite connection and cursor private;
- accepts only engine-built SQL and bound parameters;
- uses `fetchmany()` with a validated batch size;
- checks the shared monotonic deadline before, during, and after iteration;
- yields immutable row tuples; and
- closes its cursor on normal exhaustion, early generator close, timeout, SQLite error,
  `KeyboardInterrupt`, `SystemExit`, and `GeneratorExit`.

The existing read-only authorizer, `query_only`, disabled extension loading, progress
handler, ATTACH/write/schema-change denial, and connection identity checks remain active.
Operational model datasets do not use `fetchall()`.

Per-component limits are applied to bounded canonical-encoding chunks while bytes are
written, not after materializing a complete encoded record or result. M2M reference
accumulation also has a conservative bound derived from the records-file limit and a
fixed hard ceiling. A component exceeding its records limit, media-index limit, M2M
reference bound, or deadline fails and cleans its owned output.

Before SQLite returns selected values to Python, every row is measured in SQL and rows
over `BACKUP_LOGICAL_EXPORT_MAX_ROW_INPUT_BYTES` are detected by a `SELECT 1 ... LIMIT
1` preflight that does not project the oversized values. Only after that preflight
passes does the same read transaction run the normal ordered streaming query. The
validated fetch-batch and row-input limits may not exceed a combined 16 MiB decoded
payload-byte ceiling. Python tuple/object overhead is additional, which is why this
ceiling retains substantial headroom rather than claiming an exact process-memory cap.

## 17. Private component storage layout

The internal layout is:

```text
<private-staging-root>/
  ws-<workspace-uuid>/
    components/
      <component-export-uuid>/
        records.ndjson
        media-index.ndjson
```

Only engine-generated UUID directory names are used. Tenant names, component keys,
emails, model labels, business names, request values, and storage names do not become
path segments.

Directories receive mode `0700` and files mode `0600` where the platform enforces POSIX
modes. Windows still requires a correctly private inherited service-account ACL.

Creation checks lexical and resolved containment, symlink/junction/reparse status,
same-device placement, regular-file identity, and fixed filenames. Temporary files use
engine-generated hidden `.part` names and `O_CREAT | O_EXCL` with no-follow and
close-on-exec flags where available.

The logical exporter accepts only the exact controlled `SQLiteSnapshotProvider` type,
requires the batch snapshot and component provider identifiers to match the locked
versions, and validates every returned component key, version, schema, count, model
count, timestamp, provider identifier, and opaque reference before trusting it.

Finalization flushes and `fsync()`s the file, rechecks identity and containment, requires
the final name not to exist, atomically publishes the same inode at the fixed final name
with an exclusive hard-link operation, verifies the expected link-count transition
`1 -> 2 -> 1`, removes the temporary name, and rejects unexpected side files or aliases.
Private mode is applied through the open descriptor where supported, with identity
verification around the fallback. This is a
no-clobber atomic publication: an unexpected final name causes failure rather than being
overwritten. It does not claim directory-fsync durability or artifact integrity hashing.

## 18. Opaque component read and cleanup APIs

`ComponentExportReference` contains only a UUID. `ComponentExportResult` may expose only
safe bounded metadata:

- component key and version;
- opaque reference;
- total and per-model row counts;
- media count;
- records and media-index byte counts;
- record and ordering versions;
- creation time and bounded duration;
- provider identifier.

It contains no path, record content, tenant name, SQL, hash, encryption metadata, or
storage key.

`open_component_export()` accepts an immutable context, an opaque reference, and one of
the two fixed stream enums. It reconstructs the engine-owned path internally, validates
workspace ownership, containment, exact directory contents, file identity, and fixed
filename, and yields a narrow binary reader. It cannot select an arbitrary filename or
expose the path.

`cleanup_component_export()` reconstructs the same exact targets, removes only
`records.ndjson` and `media-index.ndjson`, removes the UUID directory only when empty,
preserves unrelated workspace content, rejects link/reparse or identity changes, and is
idempotent.

## 19. Batch snapshot cleanup guarantee

`export_snapshot_components()` is the required batch boundary. A direct single-component
call does not own the batch snapshot lifecycle.

The coordinator:

1. validates the context, consistent `SnapshotResult`, snapshot provider, exporter, and
   complete component plan;
2. exports components in resolved order;
3. removes previously completed component outputs if a later component fails or the
   operation aborts;
4. always attempts exact snapshot cleanup in `finally`; and
5. returns the component-result tuple only when snapshot cleanup returned exactly
   `True`.

On successful return, the full shared SQLite snapshot is gone and the opaque logical
component outputs remain for a later internal phase.

If snapshot cleanup fails or reports that it did not remove the snapshot, the coordinator
does not return success. It attempts to remove all completed component outputs and raises
`SnapshotCleanupAfterExportError` with only a boolean `cleanup_incomplete` signal.

## 20. Failure, abort, and sanitized error behavior

Component creation and export are all-or-nothing. Failure removes the owned temporary or
final files and removes the generated component directory when empty. Earlier batch
components are removed in reverse order if a later component fails.

`KeyboardInterrupt`, `SystemExit`, and `GeneratorExit` pass through cleanup and are then
re-raised with their original traceback. Normal engine failures are converted to stable
sanitized categories, including:

- registry and unsupported-field errors;
- tenant-isolation and reference-resolution errors;
- unsafe-media and invalid-policy errors;
- component byte-limit and timeout errors;
- component creation, validation, not-found, and cleanup errors; and
- snapshot-cleanup-after-export errors.

Sanitized failures do not contain SQL, SQLite error text, paths, tenant data, internal
IDs, record content, media names, credentials, or stack traces. Ordinary exception cause
and context chains are removed at the component boundary. Retryability is reserved for
the bounded component-timeout category; cleanup state is exposed only as a boolean.

Event names are reserved for future orchestration:

- `backup.component_export_started`;
- `backup.component_export_completed`;
- `backup.component_export_failed`;
- `backup.component_export_cleaned`;
- `backup.logical_export_batch_completed`;
- `backup.logical_export_batch_failed`;
- `backup.snapshot_cleaned_after_export`; and
- `backup.snapshot_cleanup_after_export_failed`.

The Phase 2C provider does not independently persist fake activity evidence.

## 21. Capability flags and disabled execution

Code-owned capabilities are:

```text
SQLITE_SNAPSHOT_PROVIDER_READY = True
TENANT_LOGICAL_EXPORT_PROVIDER_READY = True
OPERATIONAL_PROVIDER_STACK_READY = False
real_execution_available() = False
```

These flags describe internal implementation readiness, not permission to execute a
commercial backup workflow.

Planning reports:

- `PREPARE_SNAPSHOT = PLANNED`; and
- `EXPORT_COMPONENTS = PLANNED`.

Planning performs no SQLite query, workspace creation, snapshot creation, logical export,
snapshot cleanup, component-file creation, record transition, count persistence, event
persistence, or task enqueue. Later operational stages remain `NOT_STARTED`.

`execute_backup()` remains a plain, deliberately disabled function. It is not a Celery
task and exposes no `.delay` or `.apply_async` route. The engine setting must remain
false because media capture, packaging, verification, encryption, and storage are
incomplete.

## 22. Settings and system-check IDs

Phase 2C settings and defaults are:

| Setting | Default | Accepted bounds |
| --- | ---: | ---: |
| `BACKUP_LOGICAL_EXPORT_FETCH_BATCH_SIZE` | `200` | 1–10,000 |
| `BACKUP_LOGICAL_EXPORT_COMPONENT_TIMEOUT_SECONDS` | `120.0` | 1–3,600 |
| `BACKUP_LOGICAL_EXPORT_MAX_RECORDS_BYTES` | `536870912` | 1–10 GiB |
| `BACKUP_LOGICAL_EXPORT_MAX_MEDIA_INDEX_BYTES` | `33554432` | 1–1 GiB |
| `BACKUP_LOGICAL_EXPORT_MAX_ROW_INPUT_BYTES` | `65536` | 1–8 MiB |
| `BACKUP_LOGICAL_EXPORT_MAX_JSON_DEPTH` | `20` | 1–100 |
| `BACKUP_LOGICAL_EXPORT_MAX_MEDIA_NAME_LENGTH` | `1024` | 1–4,096 |

Booleans are rejected where a numeric value is required. Non-finite, fractional integer,
nonpositive, and above-bound values fail closed. Fetch batch size multiplied by maximum
row-input bytes must not exceed the 16 MiB decoded payload ceiling.

Backup checks are:

- `backups.E001`: tenant model missing component classification;
- `backups.E010`: enabled engine lacks a dedicated Celery broker;
- `backups.E011`: enabled engine uses forbidden eager Celery execution;
- `backups.E012`: operational provider stack remains incomplete;
- `backups.E020`: unsafe private staging-root configuration;
- `backups.E021`: invalid SQLite snapshot policy;
- `backups.E022`: invalid logical-export limits; and
- `backups.E023`: incomplete or invalid logical-export registry.

The settings and registry checks do not create a workspace, open or mutate SQLite, query
tenant rows, resolve `MEDIA_ROOT`, or read media.

## 23. Final QA results

**Status: PASSED.**

| Validation | Command | Count/duration/result |
| --- | --- | --- |
| Focused Phase 2C | `.\.venv\Scripts\python.exe manage.py test tests.test_backups_phase2c_export -v 1` | **95 tests; 62.741s; OK** |
| Backup regression | `.\.venv\Scripts\python.exe manage.py test tests.test_backups_phase1 tests.test_backups_phase2a tests.test_backups_phase2b_snapshot tests.test_backups_phase2c_export -v 1` | **236 tests; 76.952s; OK** |
| Django checks | `.\.venv\Scripts\python.exe manage.py check` | **Passed; no issues** |
| Migration drift | `.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run` | **Passed; no changes detected** |
| Ruff | All 22 changed Python files | **Passed** |
| Black | All new and baseline-clean changed Python files | **Passed; 17 files unchanged** |
| Whitespace/diff | `git diff --check` | **Passed** |
| Manual security review | Tenant, reference, token, media, cleanup, and disabled-stack boundaries | **Passed; no high-confidence blocker remains** |

Five edited legacy files that were already outside Black format at `HEAD` were excluded
from the Black gate as required: `apps/backups/tasks.py`,
`apps/backups/engine/pipeline.py`, `apps/backups/registry.py`,
`config/settings/base.py`, and `tests/test_backups_phase2a.py`. Their Phase 2C edits were
checked by Ruff and the backup regression; they were not broadly reformatted.

A skipped application-wide suite must not be described as a full regression pass. The
separate final baseline comparison belongs after the focused Phase 2C security audit.

## 24. Remaining risks

- Component records and media indexes are plaintext private staging files until a later
  phase encrypts and packages them.
- The full shared snapshot exists temporarily; a hard process or host termination can
  leave an orphan that must be handled by an operational workspace-reaper policy.
- Windows `chmod` does not establish a private ACL by itself; staging privacy depends on
  the configured service-account ACL.
- A malicious process running as the same operating-system account can exceed the
  protection offered by path, link, and identity checks.
- Media discovery does not prove that a physical object exists, is readable, is safe, or
  matches a future hash.
- Global User references may not resolve in a different destination installation. A
  future restore must define safe match/remap/failure semantics without exporting account
  secrets.
- Reference-only and dependency-only records still require a reviewed importer policy.
- There is no final artifact hash, component integrity claim, encryption guarantee,
  restore verification, retention guarantee, or secure-download boundary.
- Limits are per component; Phase 2C does not currently impose a separate total batch
  package quota.

## 25. Explicitly deferred work

Phase 2C deliberately does not implement:

- media-byte capture or physical media verification;
- canonical final manifest serialization;
- component or whole-artifact hashes;
- archive/package construction;
- compression;
- encryption or key management;
- private object storage;
- upload, finalization, or secure download;
- deep artifact verification;
- restore planning or execution;
- global User remapping;
- operational worker lifecycle;
- scheduling or automatic execution;
- retention or deletion workflows;
- owner or Platform Admin execution UI;
- `BackupComponent` count persistence;
- backup success/integrity transitions; or
- production deployment.

## 26. Phase 2D handoff assumptions

Phase 2D must consume only opaque `ComponentExportReference` values through the narrow
component-reader API. It must never receive, package, expose, or retain the raw SQLite
snapshot.

The next phase may rely on:

- canonical `nexa.logical-record.v1` and
  `nexa.logical-media-reference.v1` streams;
- deterministic component, model, record, relation, and JSON ordering;
- trusted component/model versions and safe result counts;
- the complete validated media index;
- the snapshot already being removed before a successful batch result; and
- exact component cleanup remaining available by opaque reference.

Phase 2D must add media-byte capture, canonical manifest/package construction, integrity
hashing, compression/encryption, and exact cleanup of consumed component outputs. It must
preserve tenant isolation, global User reference restrictions, `REFERENCE_ONLY`,
`DEPENDENCY_ONLY`, and `NON_RESTORABLE` semantics.

The operational provider stack must remain disabled until later phases also provide
private storage, verification, worker lifecycle, retention, secure download, and a
separately reviewed restore boundary.
