# Nexa Backup Phase 3I: Production Activation and DR UAT Runbook

This is the operator contract for preparing Nexa Backup & Restore v1 for a
controlled production activation. Phase 3I performed no deployment, provider
mutation, backup, restore, worker startup, or Beat startup.

## 1. Current capability state

The backup and restore code paths are complete for the locked v1 scope: SQLite
snapshot, explicit tenant logical export, media capture, deterministic packaging,
independent verification, AES-256-GCM encryption, AWS KMS wrapping, private
S3-compatible storage, exact bucket/key/version persistence, daily-full retention,
durable dispatch, reconciliation, scheduling, restore preflight, guarded mutation,
mandatory protected safety backup, restart-safe restore claiming, and operator UI.

The following defaults and code gate remain closed:

```text
OPERATIONAL_PROVIDER_STACK_READY=False
BACKUP_EXECUTION_ENGINE_ENABLED=False
BACKUP_RESTORE_MUTATION_ENABLED=False
```

`OPERATIONAL_PROVIDER_STACK_READY` represents approved, deployed operational
readiness, not code capability. It remains false because no production KMS,
Spaces, Redis, worker, Beat, filesystem, or UAT evidence was collected in this
phase. A later activation release may open it only after this runbook's evidence
gate is approved. The two environment feature flags must remain false in the
Phase 3I deployment.

Use the structured gate without network calls:

```bash
/opt/nexapos/venv/bin/python manage.py backup_readiness --json
```

Only during an approved readiness window, run the explicit non-mutating provider
attestation. It performs KMS `DescribeKey` and Spaces `HeadBucket`; it does not
upload, delete, decrypt, back up, restore, or change tenant data:

```bash
/opt/nexapos/venv/bin/python manage.py backup_readiness --attest-providers --json
```

The result distinguishes `CODE_READY`, `INFRASTRUCTURE_NOT_CONFIGURED`,
`READY_FOR_BACKUP_UAT`, and `READY_FOR_RESTORE_UAT`. `READY_FOR_BACKUP_UAT`
means prerequisites have been proven; it does not mean customer execution is
enabled. Restore is a separate, later gate.

## 2. Final code-level readiness audit

### READY

| Area | Evidence |
|---|---|
| Tenant isolation | Business-scoped managers/selectors, immutable tenant/public UUID identity, and exact tenant binding at task and restore boundaries. |
| Snapshot | SQLite online-backup provider with WAL/FULL policy, bounded time/free-space checks, local-source requirement, and independent verification. |
| Logical data/media | Explicit versioned registries, no automatic model discovery, bounded export/media policies, deterministic package, and canonical manifest. |
| Encryption/KMS | AES-256-GCM envelope encryption, wrapped DEK only, production AWS KMS provider, historical key resolution, re-wrap support, sanitized errors, and bounded retries. |
| Durable storage | Private S3-compatible provider, UUID-derived keys, persisted backend/bucket/key/version, exact historical retrieval, checksum/size verification, and version-aware deletion. |
| Retention | Latest five verified successful scheduled daily full backups; protected safety backups, failed/corrupt/incomplete/manual backups are ineligible. |
| Runtime/locks | Tenant operation leases, guarded state machines, bounded Celery tasks, worker-only execution, and no inline web execution. |
| Scheduling/dispatch | One durable scheduled occurrence, append-only queue intent, bounded publish attempts, DB-authoritative reconciliation, and duplicate-safe worker claims. |
| Restore | Exact historical retrieval, preflight without mutation, dedicated queue, mandatory durable safety backup, transactional logical restore, media rollback, post-verification, and recovery-required fail-closed state. |
| UI/security | Owner/platform separation, platform capability checks, support-actor isolation, CSRF-protected POST actions, public UUID URLs, and sanitized operator output. |
| Deployment safety | Disabled infrastructure does not block ordinary Django startup or migrations. Local providers remain usable only for development/test. |
| Activation observability | Secret-free structured activation gate, management command, Platform Admin health, DB backlog/stale/lease evidence, and fixed queue topology checks. |

### BLOCKED pending production evidence

- The operational provider-stack kill switch is false.
- Production KMS identity, key status, region, and permissions are not attested.
- A private Spaces bucket and its versioning/lifecycle/access policy are not attested.
- A dedicated production Redis broker is not attested.
- Live backup, restore, and control workers are not installed or observed.
- Single live Beat ownership is not assigned or observed.
- The production staging path, ownership, permissions, and free space are not proven.
- The production SQLite path, local filesystem, WAL mode, FULL synchronous mode,
  free space, and maintenance procedure are not proven.
- No disposable-tenant backup, schedule, retention, preflight, or restore rehearsal
  evidence exists.

These are operational blockers, not missing schema. No migration is required.

### INTENTIONALLY DEFERRED

- Backup download/grant consumption is not exposed in v1.
- Customer pin controls and weekly/monthly retention are future enhancements.
- PostgreSQL backup snapshots are not implemented. If production changes from
  local SQLite to PostgreSQL, activation is blocked until a PostgreSQL snapshot
  provider is designed and approved.
- Customer restore mutation remains unavailable until a controlled UAT and
  separate production approval.

## 3. Canonical production environment contract

Never commit real secrets. Store them in `/etc/nexapos/nexapos.env` with owner
read access only, or in an equivalent managed secret facility. KMS and Spaces
use independent credentials and must never share or copy credentials:

```text
AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / optional AWS_SESSION_TOKEN
    -> standard AWS/boto3 credential chain -> AWS KMS only

BACKUP_S3_ACCESS_KEY_ID / BACKUP_S3_SECRET_ACCESS_KEY
    -> explicit S3 client credentials -> DigitalOcean Spaces only
```

The Spaces client does not fall back to the standard AWS credential chain. Never
render, log, persist, or include either credential domain in readiness evidence.

### Django and security

| Variable | Production contract |
|---|---|
| `DJANGO_SETTINGS_MODULE` | `config.settings.production` |
| `SECRET_KEY` | Required, unique, high entropy, secret-managed. |
| `DEBUG` | `False`. |
| `ALLOWED_HOSTS` | Exact production hostnames. |
| `CSRF_TRUSTED_ORIGINS` | Exact HTTPS origins. |
| `DATABASE_URL` | Backup v1 requires an absolute local SQLite URL such as `sqlite:////var/lib/nexapos/db.sqlite3`. |
| `SECURE_SSL_REDIRECT` | `True`. |
| `SESSION_COOKIE_SECURE` | `True`. |
| `CSRF_COOKIE_SECURE` | `True`. |

The application can use PostgreSQL, but Backup v1 cannot snapshot PostgreSQL.
Do not activate backup execution against a PostgreSQL `DATABASE_URL`.

### Celery and Redis

| Variable | Production contract |
|---|---|
| `CELERY_BROKER_URL` | Required dedicated Redis URL; non-eager and non-local. Prefer `rediss://` or a private network with authentication. |
| `REDIS_URL` | Recommended for application cache/result transport. Celery results are never authoritative backup evidence. |
| `BACKUP_EXECUTION_QUEUE_NAME` | Exactly `nexa.backups`. |
| `BACKUP_RESTORE_QUEUE_NAME` | Exactly `nexa.restores`. |
| `BACKUP_SCHEDULER_QUEUE_NAME` | Exactly `nexa.backup_scheduling`. |
| `BACKUP_SCHEDULE_DISPATCH_INTERVAL_SECONDS` | `300`; allowed code range 60–3600. |
| `BACKUP_RECONCILIATION_INTERVAL_SECONDS` | `600`; allowed code range 300–900. |
| `BACKUP_DISPATCH_RECONCILE_AFTER_SECONDS` | `300`. |
| `BACKUP_DISPATCH_MAX_IMMEDIATE_ATTEMPTS` | `3`. |
| `BACKUP_DISPATCH_MAX_TOTAL_ATTEMPTS` | `12`. |

### Activation flags

| Variable | Phase 3I value |
|---|---|
| `BACKUP_EXECUTION_ENGINE_ENABLED` | `False`. Change only for approved disposable-tenant backup UAT after the operational gate is opened. |
| `BACKUP_RESTORE_MUTATION_ENABLED` | `False`. Change only in the later controlled restore rehearsal window. |

### Production KMS

| Variable | Contract |
|---|---|
| `BACKUP_KEY_PROVIDER` | `aws_kms`. |
| `BACKUP_AWS_KMS_KEY_ID` | Key ARN, key ID, or controlled alias; never a credential. |
| `BACKUP_AWS_REGION` | Region containing that key. |
| `AWS_ACCESS_KEY_ID` | AWS IAM access-key ID supplied through the standard boto3 credential chain. |
| `AWS_SECRET_ACCESS_KEY` | AWS IAM secret supplied only through the protected runtime environment. |
| `AWS_SESSION_TOKEN` | Optional standard AWS session token for temporary IAM credentials. |

The development-only `BACKUP_LOCAL_KEK_B64`, `BACKUP_LOCAL_KEK_ID`, and
`BACKUP_LOCAL_KEK_VERSION` must not be used for production activation.

### Production Spaces/S3

| Variable | Contract |
|---|---|
| `BACKUP_STORAGE_PROVIDER` | `s3`. |
| `BACKUP_S3_BUCKET` | Dedicated private bucket name. |
| `BACKUP_S3_REGION` | Spaces region, for example `fra1`; choose the actual bucket region. |
| `BACKUP_S3_ENDPOINT_URL` | Regional HTTPS endpoint, for example `https://fra1.digitaloceanspaces.com`. |
| `BACKUP_S3_ACCESS_KEY_ID` | Dedicated DigitalOcean Spaces access-key ID; never an AWS IAM credential. |
| `BACKUP_S3_SECRET_ACCESS_KEY` | Dedicated DigitalOcean Spaces secret; injected securely and never committed. |
| `BACKUP_S3_PREFIX` | Dedicated prefix, normally `nexa/backups`. |
| `BACKUP_S3_ADDRESSING_STYLE` | `auto`, unless provider testing requires `path` or `virtual`. |
| `BACKUP_S3_MULTIPART_THRESHOLD_BYTES` | `67108864`. |
| `BACKUP_S3_MULTIPART_PART_BYTES` | `16777216`. |
| `BACKUP_S3_CONNECT_TIMEOUT_SECONDS` | `10`. |
| `BACKUP_S3_READ_TIMEOUT_SECONDS` | `60`. |

### Private staging and runtime bounds

`BACKUP_STAGING_ROOT` is required and must be an absolute private local path
outside the repository, `MEDIA_ROOT`, and `STATIC_ROOT`; recommended:
`/var/lib/nexapos/backup-staging`. Configure the remaining bounded values from
`.env.example` without relaxing policy during UAT:

- Workspace: `BACKUP_WORKSPACE_CLEANUP_TIMEOUT_SECONDS`.
- SQLite: `BACKUP_SQLITE_REQUIRED_JOURNAL_MODE`,
  `BACKUP_SQLITE_REQUIRED_SYNCHRONOUS`, `BACKUP_SQLITE_BUSY_TIMEOUT_SECONDS`,
  `BACKUP_SQLITE_BACKUP_PAGES_PER_STEP`, `BACKUP_SQLITE_BACKUP_SLEEP_SECONDS`,
  `BACKUP_SQLITE_SNAPSHOT_TIMEOUT_SECONDS`, `BACKUP_SQLITE_MIN_FREE_BYTES`,
  `BACKUP_SQLITE_HEADROOM_MULTIPLIER`, and
  `BACKUP_SQLITE_REQUIRE_LOCAL_STAGING`.
- Logical export: `BACKUP_LOGICAL_EXPORT_FETCH_BATCH_SIZE`,
  `BACKUP_LOGICAL_EXPORT_COMPONENT_TIMEOUT_SECONDS`,
  `BACKUP_LOGICAL_EXPORT_MAX_RECORDS_BYTES`,
  `BACKUP_LOGICAL_EXPORT_MAX_MEDIA_INDEX_BYTES`,
  `BACKUP_LOGICAL_EXPORT_MAX_ROW_INPUT_BYTES`,
  `BACKUP_LOGICAL_EXPORT_MAX_JSON_DEPTH`, and
  `BACKUP_LOGICAL_EXPORT_MAX_MEDIA_NAME_LENGTH`.
- Media: `BACKUP_MEDIA_CAPTURE_CHUNK_BYTES`,
  `BACKUP_MEDIA_CAPTURE_MAX_FILE_BYTES`, `BACKUP_MEDIA_CAPTURE_MAX_TOTAL_BYTES`,
  `BACKUP_MEDIA_CAPTURE_MAX_OBJECTS`, `BACKUP_MEDIA_CAPTURE_TIMEOUT_SECONDS`,
  `BACKUP_MEDIA_CAPTURE_MIN_FREE_BYTES`,
  `BACKUP_MEDIA_CAPTURE_HEADROOM_MULTIPLIER`,
  `BACKUP_MEDIA_CAPTURE_REQUIRE_LOCAL_STAGING`, and
  `BACKUP_MEDIA_INDEX_MAX_LINE_BYTES`.
- Encryption: `BACKUP_ENCRYPTION_CHUNK_BYTES`,
  `BACKUP_ENCRYPTION_MAX_PLAINTEXT_BYTES`,
  `BACKUP_ENCRYPTION_MAX_ARTIFACT_BYTES`,
  `BACKUP_ENCRYPTION_TIMEOUT_SECONDS`, `BACKUP_ENCRYPTION_MIN_FREE_BYTES`,
  `BACKUP_ENCRYPTION_HEADROOM_MULTIPLIER`, and
  `BACKUP_ENCRYPTION_MAX_HEADER_BYTES`.
- Durable object bounds: `BACKUP_DURABLE_STORAGE_CHUNK_BYTES`,
  `BACKUP_DURABLE_STORAGE_MAX_OBJECT_BYTES`,
  `BACKUP_DURABLE_STORAGE_TIMEOUT_SECONDS`,
  `BACKUP_DURABLE_STORAGE_MIN_FREE_BYTES`, and
  `BACKUP_DURABLE_STORAGE_HEADROOM_MULTIPLIER`.
- Retention/leases: `BACKUP_RETENTION_DAILY_FULL_KEEP_COUNT=5`,
  `BACKUP_RETENTION_MAX_DELETE_BATCH=100`,
  `BACKUP_RETENTION_TIMEOUT_SECONDS=300`, and
  `BACKUP_EXECUTION_LOCK_LEASE_SECONDS=21600`.
- Task limits: `BACKUP_EXECUTION_TASK_SOFT_TIME_LIMIT_SECONDS=21600`,
  `BACKUP_EXECUTION_TASK_TIME_LIMIT_SECONDS=21900`,
  `BACKUP_RESTORE_TASK_SOFT_TIME_LIMIT_SECONDS=43200`, and
  `BACKUP_RESTORE_TASK_TIME_LIMIT_SECONDS=43500`.
- Alerts: `BACKUP_QUEUED_AGE_WARNING_SECONDS=900`,
  `BACKUP_RESTORE_QUEUED_AGE_WARNING_SECONDS=900`,
  `BACKUP_FAILED_COUNT_WARNING=1`, and
  `BACKUP_STALE_OPERATION_SECONDS=21600`.

## 4. DigitalOcean production topology

Run each role as a separate supervised process. Gunicorn never performs backup
or restore work.

```text
Nginx -> Gunicorn web
Redis -> backup queue -> one backup worker (concurrency 1)
      -> restore queue -> one restore worker (concurrency 1)
      -> control queue -> one control worker (concurrency 1)
single Beat -> scheduled dispatch + reconciliation -> control queue
```

Commands, shown for documentation only:

```bash
gunicorn config.wsgi:application
celery -A config worker -Q nexa.backups --concurrency=1 -l info
celery -A config worker -Q nexa.restores --concurrency=1 -l info
celery -A config worker -Q nexa.backup_scheduling --concurrency=1 -l info
celery -A config beat -l info
```

Exactly one Beat process may own the schedule. Do not add Beat to multiple web
instances, containers, release commands, or process managers.

## 5. Inactive systemd service blueprints

These examples are not installed or enabled by Phase 3I. Adjust only the
deployment user/group and approved paths; keep queues isolated.

### `/etc/systemd/system/nexapos-backup-worker.service`

```ini
[Unit]
Description=NexaPOS backup worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=nexapos
Group=nexapos
WorkingDirectory=/opt/nexapos/app
EnvironmentFile=/etc/nexapos/nexapos.env
ExecStart=/opt/nexapos/venv/bin/celery -A config worker -Q nexa.backups --concurrency=1 -l info
Restart=on-failure
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=22000
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

### `/etc/systemd/system/nexapos-restore-worker.service`

```ini
[Unit]
Description=NexaPOS guarded restore worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=nexapos
Group=nexapos
WorkingDirectory=/opt/nexapos/app
EnvironmentFile=/etc/nexapos/nexapos.env
ExecStart=/opt/nexapos/venv/bin/celery -A config worker -Q nexa.restores --concurrency=1 -l info
Restart=on-failure
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=43600
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

### `/etc/systemd/system/nexapos-backup-control-worker.service`

```ini
[Unit]
Description=NexaPOS backup scheduler and reconciliation worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=nexapos
Group=nexapos
WorkingDirectory=/opt/nexapos/app
EnvironmentFile=/etc/nexapos/nexapos.env
ExecStart=/opt/nexapos/venv/bin/celery -A config worker -Q nexa.backup_scheduling --concurrency=1 -l info
Restart=on-failure
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=600
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

### `/etc/systemd/system/nexapos-celery-beat.service`

```ini
[Unit]
Description=NexaPOS single Celery Beat owner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=nexapos
Group=nexapos
WorkingDirectory=/opt/nexapos/app
EnvironmentFile=/etc/nexapos/nexapos.env
ExecStart=/opt/nexapos/venv/bin/celery -A config beat -l info --pidfile=/run/nexapos/celerybeat.pid --schedule=/var/lib/nexapos/celerybeat-schedule
Restart=on-failure
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=300
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

Create `/run/nexapos`, `/var/lib/nexapos`, and the staging directory out of band
with ownership `nexapos:nexapos` and restrictive permissions. Review systemd
hardening against the need to read the SQLite database/media and write staging.

## 6. Redis and broker requirements

- Use dedicated production Redis, never Celery's in-memory transport or a local
  development broker.
- Prefer TLS (`rediss://`) or a private trusted network, authentication, firewall
  restrictions, monitoring, capacity limits, and an explicit backup/upgrade plan.
- Configure persistence appropriate for queue durability (AOF is recommended)
  and understand the broker's failover/data-loss guarantees.
- A broker outage leaves durable requests in the authoritative database as
  queued dispatch intents. Bounded reconciliation republishes only eligible
  requests after recovery.
- Do not delete queued or ambiguous records to “fix” a broker outage.
- Celery result-backend state is diagnostic only. BackupRecord,
  RestoreOperation, BackupActivity, schedule rows, and leases are authoritative.

## 7. AWS KMS operational prerequisites

- Create a symmetric encrypt/decrypt KMS key in `BACKUP_AWS_REGION`.
- Supply its AWS IAM runtime identity through the standard boto3/AWS credential
  chain. Do not place DigitalOcean credentials in `AWS_ACCESS_KEY_ID` or
  `AWS_SECRET_ACCESS_KEY`, and do not use `BACKUP_S3_*` for KMS.
- Confirm the key is enabled and not pending deletion or disabled.
- Grant the runtime identity only `kms:Encrypt`, `kms:Decrypt`, and
  `kms:DescribeKey` for the selected key.
- Restrict the key policy and IAM policy to the deployment identity and approved
  administrators. Do not grant broad `kms:*`.
- Decide rotation policy before activation. Rotating the active key must not
  delete or disable historical keys still required to unwrap old artifacts.
- Record key ARN/ID, region, policy revision, rotation owner, and attestation
  timestamp as activation evidence. Never record wrapped-DEK plaintext or access
  credentials.

## 8. DigitalOcean Spaces prerequisites

- Use a dedicated private standard Space; keep file listing restricted and do
  not enable public ACLs or a public CDN for the backup prefix.
- Use the exact regional HTTPS endpoint and the matching region.
- Use a least-privilege key/policy permitting the required bucket/object checks,
  get/put/multipart operations, and exact-version deletion for the backup prefix.
- Store the bucket-scoped credentials only as `BACKUP_S3_ACCESS_KEY_ID` and
  `BACKUP_S3_SECRET_ACCESS_KEY` in the deployment secret system or protected
  environment file. These are explicitly passed only to the Spaces S3 client;
  never copy AWS IAM/KMS credentials into them.
- Enable bucket versioning before UAT. DigitalOcean currently supports S3
  versioning through its API and exposes status in the control panel. Once
  versioning has been enabled it may be suspended but the bucket does not return
  to an unversioned history model. See the official
  [Spaces versioning guide](https://docs.digitalocean.com/products/spaces/how-to/enable-versioning/).
- Record the returned object VersionId. Nexa persists bucket/key/version and
  retrieves or deletes the exact version when present.
- Review all lifecycle rules. Do not configure an expiration rule that removes
  current or non-current Nexa objects earlier than application retention. A rule
  that only aborts abandoned multipart uploads may be appropriate. See the
  official [lifecycle guide](https://docs.digitalocean.com/products/spaces/how-to/configure-lifecycle-rules/).
- Confirm bucket deletion protection, access logging/monitoring, and ownership.

Do not create or modify a bucket as part of the readiness command.

## 9. Filesystem staging requirements

- Use an absolute local path such as `/var/lib/nexapos/backup-staging`.
- Do not place it under the source checkout, `MEDIA_ROOT`, `STATIC_ROOT`, `/tmp`,
  shared web roots, or network filesystems.
- Restrict it to the worker user/group, normally mode `0700`.
- Size it for SQLite snapshot + logical export + media + plaintext package +
  encrypted artifact, with configured headroom and peak concurrent use.
- Monitor capacity and inode exhaustion.
- Workspace cleanup is bounded and fail-closed. Investigate abandoned workspace
  evidence before manual removal.
- Staging is transient plaintext handling space, not a durable recovery point.
  Only a verified encrypted Spaces object counts as successful.

## 10. SQLite production considerations

Backup v1 supports only a local SQLite source. Before activation:

- Confirm `PRAGMA journal_mode=WAL` and `PRAGMA synchronous=FULL` on the actual
  file used by `DATABASE_URL`.
- Keep the database, `-wal`, and `-shm` files on the same local filesystem and
  ensure the worker identity can read them.
- Prove free space for the live database, WAL growth, snapshot, media, package,
  encryption, and safety margin.
- Schedule initial UAT outside peak writes. The online snapshot reduces blocking
  but consumes disk I/O and may prolong WAL retention while readers are active.
- Monitor backup duration, busy/locked failures, database size, WAL size, and
  checkout latency during the test.
- A destructive restore requires a maintenance window, operator presence, no
  concurrent tenant writes, and enough space for the mandatory safety backup.

SQLite is not an inherent blocker for the controlled v1 restore when it is local,
WAL/FULL, capacity-tested, and used during a maintenance window. A network-mounted
SQLite file or a PostgreSQL production database is a blocker for this v1 snapshot
provider. Phase 3I does not migrate the database engine.

## 11. Safe backup activation order

1. Deploy Phase 3I code with both environment flags false and the operational
   provider-stack gate false.
2. Run normal Django checks and migrations. Disabled backup infrastructure must
   not block them.
3. Configure the KMS key and runtime identity.
4. Configure the private, versioned Spaces bucket and non-conflicting lifecycle.
5. Configure and permission the private staging path.
6. Confirm the local SQLite WAL/FULL and capacity requirements.
7. Configure dedicated Redis and the exact three queues.
8. Install—but do not yet expose customer actions through flags—the control,
   backup, and restore worker services.
9. Assign and start exactly one Beat owner.
10. Run `backup_readiness --json`; correct all configuration blockers.
11. During an approved window, run `backup_readiness --attest-providers --json`.
12. Verify the Platform Admin health page and external systemd/Redis monitoring.
13. Prepare only the disposable UAT tenant and capture the acceptance ticket.
14. Obtain approval for a separate activation release that opens
    `OPERATIONAL_PROVIDER_STACK_READY`; Phase 3I does not open it.
15. Set `BACKUP_EXECUTION_ENGINE_ENABLED=True` only for the controlled backup UAT
    window and restart the relevant application processes safely.
16. Perform one manual UAT backup and complete section 14.
17. Test one scheduled occurrence and complete section 15.
18. Complete the retention UAT in section 16.
19. Keep restore mutation false. Evaluate the separate restore gate only after
    every backup acceptance item is approved.

Do not enable customer-facing execution merely because configuration fields are
present. Provider, process, data-path, monitoring, and UAT evidence are required.

## 12. Restore activation order

Before setting `BACKUP_RESTORE_MUTATION_ENABLED=True`, require all of the following:

- an approved successful production backup for the disposable UAT tenant;
- exact-version cloud retrieval and successful non-mutating restore preflight;
- a dedicated restore worker proven online on `nexa.restores`;
- a successful mandatory safety-backup path using the same production providers;
- an announced maintenance window with tenant writes stopped;
- an operator and incident/recovery owner present;
- a recorded baseline and expected post-restore comparison;
- a reviewed rollback/recovery runbook and enough local/cloud capacity;
- no stale, active, ambiguous, or recovery-required operation for the tenant.

Then enable restore mutation only for the controlled disposable-tenant rehearsal.
Disable it again immediately afterward unless a separate production approval says
otherwise. Never use the first rehearsal on a customer tenant.

## 13. UAT tenant policy

Use a disposable, non-customer business with no production dependency. It should
contain realistic POS and, where entitled, WMS records: products/variants,
inventory across warehouses, customers, suppliers, purchases, orders/sales,
payments, registers, user/role data, reports-relevant history, and representative
media attachments. Record row/media counts and identifying test markers. Do not
copy unnecessary personal customer data into UAT.

## 14. First live backup UAT checklist

- [ ] Approval ticket identifies operators, UAT tenant UUID, window, commit, and
      configuration revision.
- [ ] Health page has no stale/recovery-required operations or active tenant lease.
- [ ] Manual request creates one QUEUED BackupRecord and append-only dispatch intent.
- [ ] Broker confirmation is recorded and the `nexa.backups` worker claims it once.
- [ ] Status advances through prepare, snapshot, package, upload, and verification.
- [ ] SQLite snapshot completes and passes source/snapshot integrity verification.
- [ ] Logical record counts and media capture counts match expected UAT data.
- [ ] Canonical manifest and deterministic package verification pass.
- [ ] AES-256-GCM encryption succeeds; no plaintext artifact remains after cleanup.
- [ ] Persisted KMS provider/key metadata matches the approved key without exposing
      credentials or plaintext DEKs.
- [ ] The Spaces object is private and under the approved UUID-based prefix.
- [ ] Persisted backend, bucket, key, and VersionId match the exact uploaded object.
- [ ] Stored size and SHA-256 match the locally verified encrypted artifact.
- [ ] Backup is marked SUCCEEDED only after durable retrieval/verification evidence.
- [ ] Owner and Platform Admin status/history are correct; size and duration exist.
- [ ] Activity/audit trail contains request, dispatch, worker, provider, verification,
      completion, and retention events without secrets.
- [ ] No customer tenant, web request, or unrelated queue was affected.

Retain screenshots/log extracts, sanitized BackupRecord/activity evidence, object
metadata, command output, worker status, and approval ticket. Never retain secrets.

## 15. Scheduled backup UAT checklist

- [ ] Enable the UAT tenant schedule only.
- [ ] Confirm IANA timezone, local execution time, and UTC `next_run`.
- [ ] Confirm one Beat owner and one scheduler entry.
- [ ] Beat creates one durable occurrence for the intended local date.
- [ ] Repeated scheduler delivery does not create a duplicate occurrence.
- [ ] Control worker dispatches the correct tenant/public backup UUID.
- [ ] Backup worker claims the occurrence once and completes it.
- [ ] `next_run` advances exactly once to the next local day.
- [ ] `last_successful_backup` or `last_failed_backup` links to the exact occurrence.
- [ ] Reconciliation does not republish confirmed, active, terminal, legacy, or
      provider-evidenced work.

## 16. Five-day retention UAT

Use controlled accelerated scheduling/test orchestration without changing
`BACKUP_RETENTION_DAILY_FULL_KEEP_COUNT=5`.

1. Produce five successful, verified, scheduled `ALL_ENABLED` daily full backups.
2. Add a failed backup and a corrupt/incomplete backup; prove neither is eligible.
3. Produce a protected pre-restore safety backup; prove it is ineligible.
4. Produce the sixth eligible successful daily full backup.
5. Confirm the plan selects only the oldest eligible daily full backup.
6. Confirm deletion targets its persisted provider, bucket, key, and exact VersionId.
7. Confirm remote absence of that exact version before marking metadata deleted.
8. Confirm the newest five eligible objects remain retrievable and verified.
9. Confirm the failed, corrupt/incomplete, and protected objects were not counted
   toward the five and were not deleted by retention.
10. Capture plan/execution/activity evidence and test a provider delete failure;
    it must fail independently without converting the successful new backup to
    failure or deleting a different object.

## 17. Controlled failure drills

| Drill | Expected result |
|---|---|
| Broker unavailable | Request remains durable QUEUED with sanitized failed dispatch; bounded reconciliation republishes only when eligible. |
| KMS permission denied | Fail closed before durable success; no key material or provider response secret is logged. |
| Spaces unavailable | Fail closed/retry within bounds; no SUCCEEDED state without durable verification. |
| Corrupted remote object | Exact retrieval fails verification; restore/retention eligibility is denied. |
| Checksum mismatch | Backup or preflight fails closed; never reinterpret or mutate tenant data. |
| Backup worker crash before provider mutation | Lease/operation becomes stale and is classified; replay only if the durable dispatch rules prove eligibility. |
| Restore worker crash before mutation | Safe pre-mutation failure or operator-reviewed stale state; no blind destructive replay. |
| Restore worker crash after mutation begins | INDETERMINATE/RECOVERY_REQUIRED or proven rollback; automatic replay is blocked. |
| Retention delete failure | Retention reports failed/partial safely; exact version remains evidence and no unrelated object is deleted. |
| Stale queued dispatch | Reconciliation uses DB journal/lease/provider evidence and total-attempt bound. |
| Ambiguous restore state | Fail closed, preserve database/broker/evidence, and require an operator recovery decision. |

Run drills only against UAT resources. For every drill capture initial state, injected
fault, sanitized event sequence, final DB/object state, and recovery decision.

## 18. Restore-preflight UAT

Against a successful UAT backup:

- resolve the exact persisted provider/backend, bucket, key, and VersionId;
- retrieve that exact encrypted object;
- resolve and unwrap the DEK with its persisted historical KMS key metadata;
- decrypt only inside the private transient workspace;
- verify encrypted-object hash/size, package hash, manifest, and component hashes;
- validate application/schema/component compatibility and tenant binding;
- report component, logical-record, and media counts to the operator;
- clean plaintext workspace evidence safely; and
- prove that no tenant record or media path changed.

Preflight success is necessary but never sufficient authorization for mutation.

## 19. Controlled restore rehearsal

1. Record UAT tenant baseline counts, checksums, key business values, and media list.
2. Create and fully verify a source cloud backup.
3. Intentionally change known UAT records and media; record the changed state.
4. Request restore preflight for the exact source backup.
5. Review compatibility, counts, provider identity, KMS identity, and audit evidence.
6. Stop tenant writes and enter the approved maintenance window.
7. Confirm the restore worker is online and no tenant lease/operation is active.
8. Temporarily set `BACKUP_RESTORE_MUTATION_ENABLED=True` in the controlled
   environment and restart only the necessary processes.
9. Queue the exact preflight-approved restore once.
10. Verify a new protected, retention-ineligible safety backup reaches durable
    verified SUCCEEDED before any tenant mutation.
11. Verify registered logical components restore inside the guarded transaction.
12. Verify media staging/publication and rollback boundaries.
13. Verify post-restore logical records and media against the source/baseline.
14. Confirm source and safety backups remain private, retrievable, and protected.
15. Review every activity/audit/lease event and ensure no credential exposure.
16. Set `BACKUP_RESTORE_MUTATION_ENABLED=False` again and restart safely unless a
    separate approval explicitly authorizes continued availability.
17. Record pass/fail, discrepancies, recovery actions, and evidence references.

If mutation becomes ambiguous, stop. Do not retry, clear, or recreate the operation.
Preserve DB, Redis, logs, staging evidence, and durable objects for recovery review.

## 20. Rollback and emergency deactivation

1. Set `BACKUP_RESTORE_MUTATION_ENABLED=False` first.
2. Set `BACKUP_EXECUTION_ENGINE_ENABLED=False`.
3. Stop the single Beat owner if new schedule dispatch must cease.
4. Stop restore and backup workers gracefully; do not kill an active mutation
   without invoking the incident procedure.
5. Stop the control worker if reconciliation/dispatch must also cease.
6. Keep Gunicorn/web service independent and restore normal web operation.
7. Preserve Redis, database, activity, lease, systemd, and application log evidence.
8. Do not delete Spaces objects, KMS keys, wrapped DEKs, queued requests, ambiguous
   restores, leases, or staging evidence blindly.
9. Use the Platform Admin health page and configuration-only readiness command.
10. For recovery-required restore state, assign an incident owner and determine
    actual database/media state before any manual action.

Deactivation does not mean data deletion. Key/bucket destruction requires a
separate retention and legal approval process.

## 21. Monitoring and alerts

Monitor systemd state/restarts, Redis availability/depth/persistence, Beat singleton
ownership, control/backup/restore worker heartbeats, staging/disk/inodes, SQLite/WAL
growth, KMS/Spaces errors, backup duration/size, scheduled success, retention
failures, and audit-log delivery.

Default application thresholds:

- queued backup age: 900 seconds;
- queued restore age: 900 seconds;
- failed backup count: 1;
- stale operation: 21,600 seconds;
- reconciliation cadence: 600 seconds.

Treat any `RECOVERY_REQUIRED` restore as critical. Treat stale/ambiguous mutation,
repeated provider authorization failures, checksum mismatch, unexpected retention
deletion, or multiple Beat owners as immediate activation-stop conditions.

The Platform Admin health page is configuration- and DB-derived. It reports code
capability, provider configuration, flags, operational gate, queue/backlog ages,
stale operations, recovery-required restores, scheduler evidence, leases, routes,
and warnings without credentials or live network calls. External monitoring must
prove actual broker/worker/Beat process health.

## 22. Acceptance evidence and gates

Before customer-facing backup execution, retain an approved evidence bundle:

- exact deployed commit and dependency lock;
- migrations/check output and sanitized readiness JSON;
- KMS key status/region/policy/identity attestation;
- private Spaces bucket, versioning, lifecycle, region, endpoint, bucket-scoped
  identity, and proof of credential isolation from AWS IAM/KMS;
- staging/SQLite path ownership, permissions, mount, WAL/FULL, and capacity evidence;
- Redis security/persistence/monitoring evidence;
- systemd status for isolated workers and exactly one Beat owner;
- health-page screenshot with no stale/recovery-required state;
- first backup, schedule, retention, and failure-drill evidence;
- operator, reviewer, approval ticket, timestamps, tenant UUID, and rollback owner.

Backup UAT acceptance requires every section 14–17 item. Restore UAT additionally
requires sections 18–19 and immediate re-disablement afterward. Customer activation
requires a separate explicit approval; UAT completion is not automatic approval.

## 23. Deferred feature status

The locked v1 policy is the latest five successful verified scheduled daily full
backups. Pin metadata exists as a retention exclusion foundation, but customer pin,
weekly, and monthly policies are not part of v1. Download authorization metadata
exists, but there is no download route/action and no artifact streaming endpoint.
These must not be improvised during activation.

## 24. Phase 3I safety declaration

Phase 3I prepared code, configuration documentation, inactive systemd examples,
operator gates, and test coverage only. It did not deploy, install/enable services,
start Celery/Beat, contact live KMS/Spaces, add credentials, upload/delete objects,
run a live backup, run restore preflight against production, mutate tenant data, or
enable either execution flag. `OPERATIONAL_PROVIDER_STACK_READY` remains false.
