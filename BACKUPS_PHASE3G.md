# Nexa Backup Phase 3G — Production Durable Object Storage

## Scope

Phase 3G adds a production-capable S3-compatible durable object provider for
application-encrypted Nexa backup artifacts. DigitalOcean Spaces is the primary
deployment target. The existing private local-filesystem provider remains
available for development, tests, and isolated local UAT.

No provider is operationally activated by this phase. No live bucket operation,
credential validation, deployment, backup, restore, or retention deletion was
performed while implementing Phase 3G.

## Provider architecture

`DurableBackupStorageProvider` remains the provider contract for:

- storing and independently verifying an encrypted artifact;
- opening an exact private object;
- re-attesting restart-persisted object evidence;
- validating ownership and tenant/backup binding;
- deleting one exact retention-approved object; and
- confirming exact-object absence.

`DurableStorageProviderRegistry` resolves the active provider and historical
providers by persisted backend identifier. Unknown or missing identifiers fail
closed. There is no fallback to the active provider.

Supported providers:

- `local-private-filesystem`: development/test only;
- `s3-compatible`: production-capable S3 API provider.

## Persisted object evidence

Phase 3G adds two backward-compatible `BackupRecord` fields:

- `storage_bucket_identifier` — up to 255 characters, blank for local objects;
- `storage_object_version_identifier` — up to 1024 characters, blank when the
  provider does not return a version ID.

Existing evidence remains authoritative:

- `storage_backend_identifier` selects the historical provider;
- `opaque_object_key` stores the local UUID or exact S3 object key;
- `backup_size_bytes` stores verified encrypted bytes; and
- `whole_artifact_hash` stores the authoritative encrypted-artifact SHA-256.

The migration adds blank-string columns only. It performs no object scan,
historical rewrite, destructive backfill, or external API call. Existing local
records remain valid only as local records with a UUID object key and blank
bucket/version fields.

## DigitalOcean Spaces configuration model

Select the S3 provider and provide non-secret storage identity:

```text
BACKUP_STORAGE_PROVIDER=s3
BACKUP_S3_BUCKET=
BACKUP_S3_REGION=
BACKUP_S3_ENDPOINT_URL=https://<region>.digitaloceanspaces.com
BACKUP_S3_PREFIX=nexa/backups
BACKUP_S3_ADDRESSING_STYLE=virtual
```

Transfer limits and timeouts are bounded with:

```text
BACKUP_S3_MULTIPART_THRESHOLD_BYTES=67108864
BACKUP_S3_MULTIPART_PART_BYTES=16777216
BACKUP_S3_CONNECT_TIMEOUT_SECONDS=10
BACKUP_S3_READ_TIMEOUT_SECONDS=60
```

Credentials use the standard boto3 credential chain. DigitalOcean Spaces can
use `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` supplied through the
deployment secret environment. Credential values are not copied into Django
settings, object metadata, logs, UI, or source control.

The client is created lazily. Django import and disabled-state system checks do
not create a client or make a network request.

## Private access

Only the encrypted `artifact.nxb` byte stream is uploaded. Plaintext packages,
database snapshots, media staging, DEKs, and KEKs are never storage inputs.

Uploads request private object semantics. The provider does not create public
ACLs, public bucket policies, permanent URLs, or presigned URLs. Internal
retrieval uses authenticated SDK calls. The Owner UI receives only a secure
high-level state; Platform Admin receives a safe storage label and never the
bucket, object key, version, credentials, or URL.

Application-level AES-256-GCM encryption and Phase 3F envelope/KMS processing
remain mandatory. Provider-side encryption is not treated as a replacement.
No provider-specific SSE mode is required for DigitalOcean Spaces.

## Object keys and collision safety

New S3 objects use this deterministic, non-sensitive structure:

```text
<safe-prefix>/<business-public-uuid>/<backup-public-uuid>/artifact.bin
```

The key contains no database primary key, customer name, email, branch name, or
source filename. Prefix validation rejects traversal, backslashes, absolute
keys, query/fragment characters, and empty or unsafe components.

Every backup public UUID is unique. Before upload, the provider checks the exact
target key. A pre-existing object is idempotent only when its size, SHA-256
metadata, tenant/backup binding, and independently streamed hash all match.
Mismatched evidence fails closed. DigitalOcean Spaces does not document
`If-None-Match` for `PutObject` or `CompleteMultipartUpload`, so collision safety
also relies on Nexa's tenant operation lock, unique backup UUID, pre-upload
attestation, and mandatory post-upload verification rather than an unsupported
conditional header.

## Upload lifecycle

1. Open the owned encrypted staging artifact.
2. Stream a small object through `PutObject`, or use bounded multipart upload
   when the configured threshold is reached.
3. Hash and count bytes while reading encrypted staging.
4. Abort a known incomplete multipart session after failure.
5. Perform an independent `HeadObject` using the exact bucket/key/version.
6. Compare content length and trusted binding metadata.
7. Stream the private object with `GetObject` and independently recompute
   SHA-256. Multipart ETag is never interpreted as SHA-256.
8. Publish durable database metadata only after verification succeeds.
9. Delete encrypted staging only after durable verification succeeds.

An upload followed by failed verification fails the backup safely and preserves
encrypted staging. Verified publication followed by staging-cleanup failure is
represented explicitly and may be retried.

## Historical retrieval and restore

Restore selection constructs a reference only from persisted metadata:

```text
BackupRecord
→ exact persisted backend
→ provider registry
→ exact persisted bucket/key/version
→ HeadObject and streamed SHA-256 verification
→ Phase 3F KMS unwrap
→ AES-256-GCM decryption
→ package re-verification
→ existing restore flow
```

The configured bucket is used only for new uploads. If historical metadata has
a bucket and version, retrieval uses those persisted values. A changed current
bucket or prefix does not reinterpret the historical reference. Legacy local
UUID records continue through the local provider only.

No `ListObjects` discovery is used. Runtime always knows the exact persisted
reference.

## Retention deletion

The S3 provider accepts only an exact tenant/backup-bound key. When a persisted
version ID exists, `DeleteObject` includes that exact `VersionId`. It never uses
wildcards, prefix deletion, or bulk deletion.

After deletion, the provider checks absence for the same bucket/key/version. A
failed or ambiguous delete is not reported as successful. The retention engine
continues to protect pinned, safety, manual, incomplete, corrupted, or otherwise
ineligible backups.

The architecture is compatible with bucket versioning and object lock, but
neither feature is required for correctness. Version IDs are captured and used
when the provider returns them.

## Retries, timeouts, and failures

boto3/botocore standard retry mode is bounded to three total attempts.
Connection and read timeouts are explicitly bounded. Throttling, connection,
timeout, and transient service failures are exposed only as sanitized retryable
storage errors. Authentication, authorization, missing bucket, malformed
reference, binding mismatch, and checksum mismatch fail closed and are not
indefinitely retried.

Raw provider messages, credential values, authorization headers, and signed
query strings are never included in application exceptions or events.

## Non-destructive health attestation

The provider offers an optional `HeadBucket` health attestation. It creates no
test object and performs no upload or delete. Startup checks validate structure
only and do not call health attestation.

If health checks are enabled operationally, `HeadBucket` may require an
additional bucket-level permission beyond object operations.

## Minimum bucket permissions

The deployment identity should be limited conceptually to the configured bucket
and prefix:

- `PutObject`
- `GetObject`
- `HeadObject`
- `DeleteObject`
- `AbortMultipartUpload`
- `ListMultipartUploadParts` if required by the provider implementation

`HeadBucket`/limited bucket inspection is separate and needed only for optional
health attestation. Unrestricted account administration, public ACL management,
bucket-policy mutation, and list-based discovery are not required.

## Capability and activation state

Phase 3G declares:

```text
PRODUCTION_KEY_PROVIDER_READY=True
PRODUCTION_DURABLE_STORAGE_PROVIDER_READY=True
OPERATIONAL_PROVIDER_STACK_READY=False
real_execution_available()=False
restore_execution_available()=False
```

`BACKUP_EXECUTION_ENGINE_ENABLED` and `BACKUP_RESTORE_MUTATION_ENABLED` remain
false by default. When both operational paths are disabled, missing S3/KMS
infrastructure does not block normal Django startup or migrations. Explicit
activation is strict: local key management and local durable storage are
rejected, and S3 identity, endpoint, prefix, KMS, and worker configuration must
be structurally valid.

## Remaining operational blockers

Before production activation, Nexa still requires an approved deployment
runbook, provisioned private bucket and secret injection, worker/beat activation,
monitoring and alerting, orphan multipart/object reconciliation, retention audit
operations, restore drills, incident procedures, and download authorization.

Phase 3G does not deploy or activate any of those controls.

## Provider references

- DigitalOcean Spaces S3 compatibility:
  <https://docs.digitalocean.com/products/spaces/reference/s3-compatibility/>
- DigitalOcean Spaces S3 API operations and supported headers:
  <https://docs.digitalocean.com/reference/api/spaces/>
