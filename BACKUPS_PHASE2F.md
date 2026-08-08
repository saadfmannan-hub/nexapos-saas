# Backup Engine Phase 2F: Encrypted Artifact Boundary

Phase 2F adds an internal authenticated-encryption boundary after independent
package verification. It does not enable backup execution, add a runtime route,
store artifacts durably, or perform restore mutation. No deployment or database
migration is part of this phase.

## Trust and threat model

Phase 2F assumes the process and its private staging root are trusted while it
runs. It protects a published backup artifact against disclosure and undetected
modification after a Phase 2E package has been proven internally consistent and
restore-ready. It also rejects forged caller metadata by revalidating the exact
provider-held package and verification evidence before encryption, then hashes
the plaintext package again as it is streamed.

The boundary does not protect against a process or host that can read the live
KEK or transient DEK, replace trusted application code, or access plaintext
before cleanup. The local KEK provider is development/test-only; a production
KMS and durable private storage remain mandatory future work.

## Cryptographic construction

Each artifact uses a fresh 256-bit DEK from the operating system CSPRNG and a
fresh 96-bit data nonce. The verified ZIP bytes are encrypted with AES-256-GCM
through `cryptography`'s streaming `Cipher` API. A canonical header is supplied
as AEAD additional authenticated data, and the 128-bit authentication tag is
stored at the end of the artifact.

Envelope encryption keeps the payload DEK separate from the artifact. The
Phase 2F `KekProvider` boundary exposes only safe provider, key identifier, and
version metadata. `LocalConfiguredKekProvider` wraps the DEK with AES-256-GCM,
using its own fresh 96-bit nonce and domain-separated canonical AAD. Its KEK
must be supplied explicitly as strict standard Base64 encoding of exactly 32
bytes. It is never generated automatically or written into result metadata.

Raw key material exists transiently in Python objects. Python and the
`cryptography` binding do not provide a reliable way for this code to guarantee
that every interpreter or native-memory copy is zeroized. Phase 2F therefore
does not claim secure memory erasure; production KMS integration should reduce
raw KEK exposure and keep key operations behind a hardened provider boundary.

## Artifact format

The format identifier is `nexa.encrypted-backup.v1`. All integers in the binary
prefix use network byte order (big-endian):

| Field | Encoding |
| --- | --- |
| Magic | 8 bytes: ASCII `NEXA2F01` |
| Header length | unsigned 32-bit big-endian integer |
| Header | exact canonical UTF-8 JSON bytes |
| Ciphertext | exactly `ciphertext_byte_count` bytes |
| Authentication tag | 16 bytes |

Parsing is exact: the magic, bounded header length, canonical JSON bytes,
required key set, value types, Base64 encodings, calculated total length, and
absence of trailing bytes must all agree. Duplicate JSON keys, floating-point
numbers, unknown keys, truncated fields, and non-canonical encodings are
rejected.

The authenticated header contains only non-secret metadata:

- schema and format version;
- payload encryption algorithm and nonce;
- KEK provider, key identifier/version, wrapping algorithm, wrapping nonce,
  wrapped DEK, and wrapping tag;
- plaintext and ciphertext byte counts and plaintext SHA-256;
- verified package format;
- backup and tenant public UUIDs;
- verification schema, version, and provider;
- UTC creation timestamp.

It deliberately contains no raw DEK or KEK, path, tenant database identifier,
SQLite location, table name, or internal primary key. Any header-byte mutation
changes the AES-GCM AAD and fails authentication, even if a caller forges outer
hash metadata.

## Streaming, publication, and validation

Plaintext reads, ciphertext writes, whole-artifact hashing, and authenticated
decryption use policy-bounded chunks. Policy also bounds plaintext size,
encrypted size, header size, elapsed time, free staging capacity, and capacity
headroom. Invalid settings fail closed.

Artifacts are staged privately at:

```text
<workspace>/encrypted/<opaque-artifact-uuid>/artifact.bin
```

Directories use mode `0700` and files use `0600` where the platform can enforce
POSIX modes. Publication uses exclusive temporary creation, descriptor identity
checks, flush and `fsync`, same-device validation, a no-clobber hard-link
publication step, and removal of the temporary link. Symlink, reparse-point,
replacement, unexpected-directory-content, and hard-link ambiguity are rejected.

Before deleting plaintext, the provider independently validates the published
file identity, complete artifact byte count and SHA-256, canonical header,
wrapped DEK, ciphertext/tag authentication, and streamed plaintext byte count
and SHA-256. Decrypted validation bytes go only to an in-memory digest sink;
they are not persisted as a second plaintext file. The same internal streaming
reader forms a future restore boundary but is not connected to HTTP, UI, Celery,
schedulers, signals, management commands, or restore mutation.

## Cleanup and failure semantics

Only a successfully published and independently authenticated encrypted
artifact can trigger deletion of its deterministic plaintext package.
Verification evidence is retained. A failure before that point preserves the
plaintext package and verification evidence and removes only partial encrypted
output whose identity proves it belongs to the current attempt.

If package cleanup fails after encrypted validation, the encrypted artifact is
kept and the result reports `plaintext_cleanup_incomplete=True`. The exact same
request/result pair can retry package cleanup. Completion changes the flag to
false only after the provider confirms cleanup. Encrypted-artifact cleanup is
exact and idempotent, rejects forged context, and refuses to delete unowned or
hard-linked replacements. Errors are categorized and sanitized; paths, key
bytes, ciphertext, plaintext, and raw cryptography/OpenSSL details are omitted.
Process abort signals are preserved.

## Rotation and provider roadmap

The format records enough authenticated wrapping metadata to select a KEK
provider, key identifier, and key version. A future rotation workflow can
authenticate the artifact, unwrap the DEK with the old KEK, and re-wrap that DEK
with a new KEK without re-encrypting the payload ciphertext. Because the header
is payload AAD, safe re-wrapping also requires recomputing the payload GCM tag
against the new canonical header while streaming/authenticating the existing
ciphertext, or using a future format with a separately authenticated wrapping
header. Phase 2F intentionally implements no in-place rewrap mutation.

Production work still requires a real KMS/HSM-backed provider, durable private
object storage, retention and deletion policy, operational orchestration,
monitoring, and the complete restore workflow.

## Configuration and capability state

The encryption policy uses these settings:

- `BACKUP_ENCRYPTION_CHUNK_BYTES`
- `BACKUP_ENCRYPTION_MAX_PLAINTEXT_BYTES`
- `BACKUP_ENCRYPTION_MAX_ARTIFACT_BYTES`
- `BACKUP_ENCRYPTION_TIMEOUT_SECONDS`
- `BACKUP_ENCRYPTION_MIN_FREE_BYTES`
- `BACKUP_ENCRYPTION_HEADROOM_MULTIPLIER`
- `BACKUP_ENCRYPTION_MAX_HEADER_BYTES`
- `BACKUP_LOCAL_KEK_B64`
- `BACKUP_LOCAL_KEK_ID`
- `BACKUP_LOCAL_KEK_VERSION`

Normal disabled development configuration may leave all local KEK values empty.
If the engine is configured for use, or any local KEK value is supplied, Django
system checks require a complete valid local configuration. `.env.example`
contains no key.

All seven internal provider flags through
`ENCRYPTED_ARTIFACT_PROVIDER_READY` are true. Nevertheless,
`OPERATIONAL_PROVIDER_STACK_READY` and `real_execution_available()` remain
false. Phase 2F is an internal primitive only and authorizes no deployment.
