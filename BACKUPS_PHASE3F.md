# Nexa Backup Phase 3F: production key management

## 1. Envelope encryption architecture

Each verified backup package receives a new random 32-byte DEK. The package is
encrypted with AES-256-GCM, and the configured key-encryption provider wraps that DEK.
The artifact retains the existing authenticated `nexa.encrypted-backup.v1` framing.
DEKs are never reused and are held only for the shortest practical operation lifetime.
Python cannot guarantee physical memory zeroization, so no stronger claim is made.

The authenticated artifact header already durably records the provider identifier, key
reference, key version, wrapping algorithm, and wrapped DEK. `BackupRecord` also already
contained `encryption_key_identifier` and the reserved
`encrypted_data_key_envelope` sidecar. No migration was required.

## 2. Provider interface and registry

`KeyEncryptionProvider` defines configuration validation, active key identity, DEK wrap,
DEK unwrap, and a non-mutating health check. `KeyEncryptionProviderRegistry` owns one
active provider plus explicitly registered historical providers. Unknown providers fail
closed; there is no fallback to the local provider.

Runtime composition selects the active provider dynamically from
`BACKUP_KEY_PROVIDER`. Restore reads the artifact or verified sidecar metadata and asks
the registry for the matching historical provider.

## 3. Local provider

`LocalConfiguredKekProvider` preserves Phase 2F AES-256-GCM DEK wrapping for local
development and unit tests. It is explicitly marked development-only. It remains
available while execution is disabled, but system check `backups.E047` rejects it when
backup execution or restore mutation is activated.

## 4. AWS KMS provider

`AwsKmsKeyEncryptionProvider` uses the official boto3 client and KMS `Encrypt`,
`Decrypt`, and `DescribeKey` APIs. Django generates the random DEK; KMS only wraps and
unwraps it. KMS key material never enters the process.

The provider is lazy: configuration and Django startup do not make a KMS network call.
The first runtime operation constructs the client with standard-mode retries bounded to
three total attempts.

## 5. Credential model

Configuration uses:

- `BACKUP_KEY_PROVIDER=local|aws_kms`
- `BACKUP_LOCAL_KEK_B64`, `BACKUP_LOCAL_KEK_ID`, and
  `BACKUP_LOCAL_KEK_VERSION` for development only
- `BACKUP_AWS_KMS_KEY_ID` and `BACKUP_AWS_REGION` for AWS KMS

AWS credentials use boto3's standard credential chain. Prefer an IAM role or workload
identity. No credential setting, access key, secret key, token, production key ID, or
raw KEK is committed.

## 6. Key references and historical decrypt

The artifact records the configured logical key reference and the immutable `KeyId`
returned by KMS Encrypt. The latter is used for Decrypt, so changing the active alias
does not reinterpret historical artifacts. Local historical keys must be explicitly
registered in local/test compositions; AWS KMS can resolve historical ciphertext by its
stored immutable key reference.

## 7. Rotation model

Changing the active provider/key affects new backups only. Existing backups keep their
historical metadata and remain decryptable while the old KMS key remains enabled and
authorized. Phase 3F never deletes, disables, schedules deletion of, or grants broad
permissions to a key.

## 8. DEK re-wrap

`EncryptedArtifactProvider.rewrap_encrypted_artifact_key()` performs:

1. full artifact hash/size and header identity verification;
2. current wrapper selection from the verified sidecar or original header;
3. DEK unwrap through the historical provider;
4. wrap through the active target provider;
5. test unwrap and constant-time DEK equality validation;
6. publication of the new sidecar metadata.

The artifact and AES-GCM payload are never decrypted, re-encrypted, or rewritten during
re-wrap. Consequently the durable object bytes and whole-artifact SHA-256 remain
unchanged.

## 9. Re-wrap atomicity

The original artifact header remains permanent recovery evidence. A successful re-wrap
is stored in the existing `encrypted_data_key_envelope` database sidecar.
`publish_rewrapped_key_metadata()` uses an atomic compare-and-set over the old key ID,
old sidecar, object size, and object hash. New metadata is published only after the new
wrapper has been test-unwrapped. A failed validation or concurrent update leaves the
old metadata unchanged.

There is no bulk re-wrap UI or production management-command execution in this phase.

## 10. Retry and failure behavior

KMS throttling, service unavailability, internal errors, and endpoint failures become a
sanitized retryable provider error. Permission denial, disabled/invalid keys, region
mismatch, corrupted ciphertext, and unknown providers fail closed as non-retryable key
handling errors. Raw SDK exception text, DEKs, wrapped DEKs, credentials, and local KEKs
are not logged or returned to user interfaces.

## 11. Minimum IAM permissions

The backup and restore worker requires only:

- `kms:Encrypt`
- `kms:Decrypt`
- `kms:DescribeKey`

Do not grant `kms:*`. Key policy and IAM conditions should scope access to the approved
backup KMS key and workload identity.

## 12. Availability and health checks

System checks validate provider selection and configuration structure without a live KMS
call. `DescribeKey` is available as an optional worker-side health attestation and does
not create or modify backup data. Missing key ID/region fails readiness when an
operational path is activated.

The deployment-safety hotfix remains intact: concrete provider checks do not block the
POS/WMS application while both execution paths and the operational provider stack are
disabled.

## 13. Owner and Platform exposure

Owner pages never expose provider details, key references, or wrapped DEKs. Platform
pages continue to omit object paths, full key identifiers, wrapped DEKs, and credentials.
No key deletion, rotation, or bulk re-wrap control is exposed in either UI.

## 14. Safety backup integration

Restore mutation builds one runtime provider stack and gives that same stack to the
mandatory safety-backup coordinator. Safety backups therefore use the current active
key provider; no separate or weaker encryption path exists.

## 15. Capability state and remaining blockers

`PRODUCTION_KEY_PROVIDER_READY=True` records code support for the production KMS
boundary. `OPERATIONAL_PROVIDER_STACK_READY` remains `False`,
`BACKUP_EXECUTION_ENGINE_ENABLED` remains `False`, and
`BACKUP_RESTORE_MUTATION_ENABLED` remains `False`.

Remaining operational blockers include production object storage, production
worker/beat activation, approved credentials/IAM deployment, destructive historical
retention policy, download authorization, and operational runbooks.

## 16. Deployment status

No deployment, live AWS call, real credential use, backup execution, restore execution,
key rotation, or re-wrap operation was performed in Phase 3F.
