"""Private production durable storage through S3-compatible APIs."""

from __future__ import annotations

import hashlib
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.utils import timezone

from .contracts import (
    DurableBackupStorageProvider,
    PersistedStoredObjectDescriptor,
    ReattestedStoredObjectResult,
    StoredBackupObjectReference,
    StoredBackupObjectRequest,
    StoredBackupObjectResult,
    StoredObjectDurabilityState,
    StoredObjectVerificationState,
)
from .durable_storage import STORED_OBJECT_SCHEMA_IDENTIFIER
from .durable_storage_exceptions import (
    DurableObjectCleanupError,
    DurableObjectCreationError,
    DurableObjectNotFound,
    DurableObjectValidationError,
    DurableStorageAuthorizationError,
    DurableStorageEngineError,
    DurableStoragePolicyError,
    DurableStorageUnavailable,
    EncryptedStagingCleanupError,
    Phase2GCoordinationError,
)
from .encrypted_artifact import EncryptedArtifactProvider
from .encryption_exceptions import (
    EncryptedArtifactCleanupError,
    EncryptedArtifactNotFound,
    EncryptedArtifactValidationError,
)

S3_DURABLE_STORAGE_BACKEND_IDENTIFIER = "s3-compatible"
S3_DURABLE_STORAGE_PROVIDER_IDENTIFIER = "s3-compatible-durable-storage-v1"
S3_MAX_ATTEMPTS = 3
_MIN_PART_BYTES = 5 * 1024**2
_MAX_PART_BYTES = 512 * 1024**2
_MAX_S3_OBJECT_BYTES = 5 * 1024**4
_MAX_MULTIPART_PARTS = 10_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,253}[a-z0-9]$")
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_AUTH_CODES = frozenset(
    {
        "AccessDenied",
        "AllAccessDisabled",
        "AuthorizationHeaderMalformed",
        "ExpiredToken",
        "InvalidAccessKeyId",
        "InvalidSecurity",
        "NoSuchBucket",
        "SignatureDoesNotMatch",
    }
)
_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound", "NoSuchVersion"})
_TRANSIENT_CODES = frozenset(
    {
        "RequestTimeout",
        "RequestTimeoutException",
        "SlowDown",
        "Throttling",
        "ThrottlingException",
        "InternalError",
        "InternalFailure",
        "ServiceUnavailable",
        "503",
    }
)


def _safe_text(value, *, maximum, allow_empty=False):
    if type(value) is not str:
        raise DurableStoragePolicyError()
    candidate = value.strip()
    if (not candidate and not allow_empty) or len(candidate) > maximum:
        raise DurableStoragePolicyError()
    if any(character in candidate for character in ("\x00", "\r", "\n")):
        raise DurableStoragePolicyError()
    return candidate


def normalize_s3_prefix(value):
    raw_prefix = _safe_text(value, maximum=200, allow_empty=True)
    if raw_prefix.startswith("/"):
        raise DurableStoragePolicyError()
    prefix = raw_prefix.rstrip("/")
    if not prefix:
        return ""
    if "\\" in prefix or "?" in prefix or "#" in prefix:
        raise DurableStoragePolicyError()
    components = prefix.split("/")
    if any(
        not component
        or component in {".", ".."}
        or not _SAFE_COMPONENT.fullmatch(component)
        for component in components
    ):
        raise DurableStoragePolicyError()
    return "/".join(components)


def _endpoint(value):
    candidate = _safe_text(value, maximum=500)
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise DurableStoragePolicyError()
    return candidate.rstrip("/")


@dataclass(frozen=True, slots=True)
class S3StorageConfiguration:
    bucket: str
    region: str
    endpoint_url: str
    prefix: str
    addressing_style: str
    multipart_threshold_bytes: int
    multipart_part_bytes: int
    connect_timeout_seconds: float
    read_timeout_seconds: float
    maximum_object_bytes: int
    chunk_bytes: int

    @classmethod
    def from_settings(cls):
        try:
            return cls(
                bucket=settings.BACKUP_S3_BUCKET,
                region=settings.BACKUP_S3_REGION,
                endpoint_url=settings.BACKUP_S3_ENDPOINT_URL,
                prefix=settings.BACKUP_S3_PREFIX,
                addressing_style=settings.BACKUP_S3_ADDRESSING_STYLE,
                multipart_threshold_bytes=settings.BACKUP_S3_MULTIPART_THRESHOLD_BYTES,
                multipart_part_bytes=settings.BACKUP_S3_MULTIPART_PART_BYTES,
                connect_timeout_seconds=settings.BACKUP_S3_CONNECT_TIMEOUT_SECONDS,
                read_timeout_seconds=settings.BACKUP_S3_READ_TIMEOUT_SECONDS,
                maximum_object_bytes=settings.BACKUP_DURABLE_STORAGE_MAX_OBJECT_BYTES,
                chunk_bytes=settings.BACKUP_DURABLE_STORAGE_CHUNK_BYTES,
            ).validated()
        except DurableStorageEngineError:
            raise
        except (AttributeError, TypeError, ValueError):
            raise DurableStoragePolicyError() from None

    def validated(self):
        bucket = _safe_text(self.bucket, maximum=255)
        region = _safe_text(self.region, maximum=64)
        if (
            len(bucket) > 63
            or ".." in bucket
            or not _BUCKET.fullmatch(bucket)
            or not _SAFE_COMPONENT.fullmatch(region)
        ):
            raise DurableStoragePolicyError()
        endpoint_url = _endpoint(self.endpoint_url)
        prefix = normalize_s3_prefix(self.prefix)
        if self.addressing_style not in {"auto", "path", "virtual"}:
            raise DurableStoragePolicyError()
        numeric = (
            type(self.multipart_threshold_bytes) is int
            and 5 * 1024**2 <= self.multipart_threshold_bytes <= 5 * 1024**4
            and type(self.multipart_part_bytes) is int
            and _MIN_PART_BYTES <= self.multipart_part_bytes <= _MAX_PART_BYTES
            and type(self.maximum_object_bytes) is int
            and 1 <= self.maximum_object_bytes <= 10 * 1024**4 + 16 * 1024**2
            and type(self.chunk_bytes) is int
            and 4096 <= self.chunk_bytes <= 16 * 1024**2
        )
        bounded_timeouts = all(
            type(value) in (int, float)
            and type(value) is not bool
            and 1 <= float(value) <= 300
            for value in (
                self.connect_timeout_seconds,
                self.read_timeout_seconds,
            )
        )
        if not numeric or not bounded_timeouts:
            raise DurableStoragePolicyError()
        return type(self)(
            bucket=bucket,
            region=region,
            endpoint_url=endpoint_url,
            prefix=prefix,
            addressing_style=self.addressing_style,
            multipart_threshold_bytes=self.multipart_threshold_bytes,
            multipart_part_bytes=self.multipart_part_bytes,
            connect_timeout_seconds=float(self.connect_timeout_seconds),
            read_timeout_seconds=float(self.read_timeout_seconds),
            maximum_object_bytes=min(self.maximum_object_bytes, _MAX_S3_OBJECT_BYTES),
            chunk_bytes=self.chunk_bytes,
        )


def _error_code(exc):
    if not isinstance(exc, ClientError):
        return ""
    try:
        return str(exc.response.get("Error", {}).get("Code", ""))
    except Exception:
        return ""


def _safe_provider_error(exc, *, cleanup=False):
    code = _error_code(exc)
    if code in _AUTH_CODES:
        return DurableStorageAuthorizationError()
    if code in _TRANSIENT_CODES or isinstance(exc, BotoCoreError):
        return DurableStorageUnavailable()
    if cleanup:
        return DurableObjectCleanupError()
    return DurableObjectCreationError()


class _HashingUploadReader:
    def __init__(self, reader):
        self._reader = reader
        self.byte_count = 0
        self.digest = hashlib.sha256()

    def read(self, size=-1):
        chunk = self._reader.read(size)
        if type(chunk) is not bytes:
            raise DurableObjectValidationError()
        self.byte_count += len(chunk)
        self.digest.update(chunk)
        return chunk

    def tell(self):
        return self._reader.tell()

    def seek(self, offset, whence=0):
        position = self._reader.seek(offset, whence)
        if position == 0:
            self.byte_count = 0
            self.digest = hashlib.sha256()
        return position


class _VerifiedRemoteReader:
    def __init__(self, body, *, byte_count, sha256, maximum_read_bytes):
        self._body = body
        self._expected_count = byte_count
        self._expected_sha256 = sha256
        self._maximum_read_bytes = maximum_read_bytes
        self._count = 0
        self._digest = hashlib.sha256()

    def read(self, size=-1):
        if type(size) is not int:
            raise DurableObjectValidationError()
        if size < 0 or size > self._maximum_read_bytes:
            size = self._maximum_read_bytes
        chunk = self._body.read(size)
        if type(chunk) is not bytes:
            raise DurableObjectValidationError()
        self._count += len(chunk)
        if self._count > self._expected_count:
            raise DurableObjectValidationError()
        self._digest.update(chunk)
        if not chunk:
            self.validate()
        return chunk

    def validate(self):
        if (
            self._count != self._expected_count
            or self._digest.hexdigest() != self._expected_sha256
        ):
            raise DurableObjectValidationError()
        return True

    def close(self):
        return self._body.close()


class S3CompatibleDurableStorageProvider(DurableBackupStorageProvider):
    """Store application-encrypted artifacts as exact private S3 objects."""

    development_only = False
    display_label = "S3-compatible storage"

    def __init__(
        self,
        *,
        encrypted_artifact_provider,
        configuration=None,
        client_factory=None,
        clock=None,
    ):
        if type(encrypted_artifact_provider) is not EncryptedArtifactProvider:
            raise Phase2GCoordinationError()
        self.encrypted_artifact_provider = encrypted_artifact_provider
        self.configuration = (configuration or S3StorageConfiguration.from_settings()).validated()
        self._client_factory = client_factory or self._build_client
        self._client_instance = None
        self._client_lock = threading.Lock()
        self.clock = clock or timezone.now
        self._stored = {}
        self._reattested = {}
        self._deleted = set()
        self._state_lock = threading.RLock()

    def __repr__(self):
        return (
            "S3CompatibleDurableStorageProvider("
            f"provider_identifier={S3_DURABLE_STORAGE_PROVIDER_IDENTIFIER!r})"
        )

    @property
    def client_created(self):
        return self._client_instance is not None

    def _build_client(self):
        config = self.configuration
        return boto3.client(
            "s3",
            region_name=config.region,
            endpoint_url=config.endpoint_url,
            config=Config(
                connect_timeout=config.connect_timeout_seconds,
                read_timeout=config.read_timeout_seconds,
                retries={"mode": "standard", "total_max_attempts": S3_MAX_ATTEMPTS},
                s3={"addressing_style": config.addressing_style},
            ),
        )

    @property
    def client(self):
        if self._client_instance is None:
            with self._client_lock:
                if self._client_instance is None:
                    try:
                        self._client_instance = self._client_factory()
                    except Exception as exc:
                        raise _safe_provider_error(exc) from None
        return self._client_instance

    def health_attestation(self):
        try:
            self.client.head_bucket(Bucket=self.configuration.bucket)
            return True
        except Exception as exc:
            raise _safe_provider_error(exc) from None

    def _object_key(self, context):
        tenant = str(context.business_public_id)
        backup = str(context.backup_public_id)
        base = f"{tenant}/{backup}/artifact.bin"
        key = f"{self.configuration.prefix}/{base}" if self.configuration.prefix else base
        if len(key) > 500 or ".." in key or "\\" in key or "?" in key or "#" in key:
            raise DurableObjectCreationError()
        return key

    @staticmethod
    def _request_values(reference):
        values = {"Bucket": reference.bucket_identifier, "Key": reference.identifier}
        if reference.version_identifier:
            values["VersionId"] = reference.version_identifier
        return values

    def _validate_reference(self, context, reference, *, error_type=DurableObjectValidationError):
        try:
            expected_suffix = (
                f"/{context.business_public_id}/{context.backup_public_id}/artifact.bin"
            )
            key = reference.identifier if type(reference) is StoredBackupObjectReference else None
            if (
                type(reference) is not StoredBackupObjectReference
                or type(key) is not str
                or type(reference.bucket_identifier) is not str
                or type(reference.version_identifier) is not str
                or not reference.bucket_identifier
                or len(reference.bucket_identifier) > 255
                or not _BUCKET.fullmatch(reference.bucket_identifier)
                or not key
                or len(key) > 500
                or len(reference.version_identifier) > 1024
                or "\\" in key
                or "?" in key
                or "#" in key
                or key.startswith("/")
                or any(
                    component in {"", ".", ".."}
                    or not _SAFE_COMPONENT.fullmatch(component)
                    for component in key.split("/")
                )
                or not f"/{key}".endswith(expected_suffix)
            ):
                raise error_type()
            return reference
        except DurableStorageEngineError:
            raise
        except Exception:
            raise error_type() from None

    @staticmethod
    def _metadata(sha256, context):
        return {
            "sha256": sha256,
            "backup-public-id": str(context.backup_public_id),
            "tenant-public-id": str(context.business_public_id),
        }

    def _head(self, reference):
        try:
            return self.client.head_object(**self._request_values(reference))
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                raise DurableObjectNotFound() from None
            raise _safe_provider_error(exc) from None
        except Exception as exc:
            raise _safe_provider_error(exc) from None

    def _stream_hash(self, reference, *, expected_count, expected_sha256):
        response = None
        body = None
        try:
            response = self.client.get_object(**self._request_values(reference))
            body = response["Body"]
            count = 0
            digest = hashlib.sha256()
            while True:
                chunk = body.read(self.configuration.chunk_bytes)
                if type(chunk) is not bytes or len(chunk) > self.configuration.chunk_bytes:
                    raise DurableObjectValidationError()
                if not chunk:
                    break
                count += len(chunk)
                if count > expected_count or count > self.configuration.maximum_object_bytes:
                    raise DurableObjectValidationError()
                digest.update(chunk)
            if count != expected_count or digest.hexdigest() != expected_sha256:
                raise DurableObjectValidationError()
            return True
        except DurableStorageEngineError:
            raise
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                raise DurableObjectNotFound() from None
            raise _safe_provider_error(exc) from None
        except Exception as exc:
            raise _safe_provider_error(exc) from None
        finally:
            if body is not None:
                try:
                    body.close()
                except Exception:
                    pass

    def _verify_reference(self, context, reference, *, byte_count, sha256):
        self._validate_reference(context, reference)
        if type(byte_count) is not int or not 1 <= byte_count <= self.configuration.maximum_object_bytes:
            raise DurableObjectValidationError()
        if type(sha256) is not str or not _SHA256.fullmatch(sha256):
            raise DurableObjectValidationError()
        head = self._head(reference)
        try:
            metadata = head.get("Metadata") or {}
            if int(head.get("ContentLength", -1)) != byte_count:
                raise DurableObjectValidationError()
            if metadata.get("sha256") != sha256:
                raise DurableObjectValidationError()
            if metadata.get("backup-public-id") != str(context.backup_public_id):
                raise DurableObjectValidationError()
            if metadata.get("tenant-public-id") != str(context.business_public_id):
                raise DurableObjectValidationError()
            returned_version = str(head.get("VersionId") or "")
            if reference.version_identifier and returned_version != reference.version_identifier:
                raise DurableObjectValidationError()
        except DurableStorageEngineError:
            raise
        except Exception:
            raise DurableObjectValidationError() from None
        self._stream_hash(reference, expected_count=byte_count, expected_sha256=sha256)
        return str(head.get("VersionId") or reference.version_identifier or "")

    def _existing_reference(self, context, key, source):
        provisional = StoredBackupObjectReference(
            key,
            bucket_identifier=self.configuration.bucket,
        )
        try:
            head = self._head(provisional)
        except DurableObjectNotFound:
            return None
        version = str(head.get("VersionId") or "")
        reference = replace(provisional, version_identifier=version)
        self._verify_reference(
            context,
            reference,
            byte_count=source.encrypted_byte_count,
            sha256=source.ciphertext_sha256,
        )
        return reference

    def _put_single(self, context, source, key):
        with self.encrypted_artifact_provider.open_encrypted_artifact(
            context=context,
            reference=source.reference,
        ) as reader:
            hashing_reader = _HashingUploadReader(reader)
            response = self.client.put_object(
                Bucket=self.configuration.bucket,
                Key=key,
                Body=hashing_reader,
                ContentLength=source.encrypted_byte_count,
                ContentType="application/octet-stream",
                ACL="private",
                Metadata=self._metadata(source.ciphertext_sha256, context),
            )
            if (
                hashing_reader.byte_count != source.encrypted_byte_count
                or hashing_reader.digest.hexdigest() != source.ciphertext_sha256
            ):
                raise DurableObjectValidationError()
        return str(response.get("VersionId") or "")

    def _put_multipart(self, context, source, key):
        upload_id = ""
        completed = False
        try:
            created = self.client.create_multipart_upload(
                Bucket=self.configuration.bucket,
                Key=key,
                ContentType="application/octet-stream",
                ACL="private",
                Metadata=self._metadata(source.ciphertext_sha256, context),
            )
            upload_id = str(created["UploadId"])
            parts = []
            digest = hashlib.sha256()
            byte_count = 0
            with self.encrypted_artifact_provider.open_encrypted_artifact(
                context=context,
                reference=source.reference,
            ) as reader:
                part_bytes = max(
                    self.configuration.multipart_part_bytes,
                    (source.encrypted_byte_count + _MAX_MULTIPART_PARTS - 1)
                    // _MAX_MULTIPART_PARTS,
                )
                if part_bytes > _MAX_PART_BYTES:
                    raise DurableStoragePolicyError()
                part_number = 1
                while True:
                    chunk = reader.read(part_bytes)
                    if type(chunk) is not bytes or len(chunk) > part_bytes:
                        raise DurableObjectValidationError()
                    if not chunk:
                        break
                    byte_count += len(chunk)
                    if byte_count > source.encrypted_byte_count:
                        raise DurableObjectValidationError()
                    digest.update(chunk)
                    uploaded = self.client.upload_part(
                        Bucket=self.configuration.bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=part_number,
                        Body=chunk,
                        ContentLength=len(chunk),
                    )
                    parts.append({"ETag": uploaded["ETag"], "PartNumber": part_number})
                    part_number += 1
            if byte_count != source.encrypted_byte_count or digest.hexdigest() != source.ciphertext_sha256:
                raise DurableObjectValidationError()
            response = self.client.complete_multipart_upload(
                Bucket=self.configuration.bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
            completed = True
            return str(response.get("VersionId") or "")
        except BaseException:
            if upload_id and not completed:
                try:
                    self.client.abort_multipart_upload(
                        Bucket=self.configuration.bucket,
                        Key=key,
                        UploadId=upload_id,
                    )
                except Exception:
                    pass
            raise

    def _result(self, context, source, reference, stored_at, *, cleanup_incomplete):
        return StoredBackupObjectResult(
            reference=reference,
            backend_identifier=S3_DURABLE_STORAGE_BACKEND_IDENTIFIER,
            object_schema_identifier=STORED_OBJECT_SCHEMA_IDENTIFIER,
            byte_count=source.encrypted_byte_count,
            sha256=source.ciphertext_sha256,
            source_encrypted_artifact_sha256=source.ciphertext_sha256,
            backup_public_id=context.backup_public_id,
            tenant_public_id=context.business_public_id,
            stored_at=stored_at.astimezone(UTC),
            provider_identifier=S3_DURABLE_STORAGE_PROVIDER_IDENTIFIER,
            durability_state=StoredObjectDurabilityState.STORED,
            verification_state=StoredObjectVerificationState.STORED_AND_VERIFIED,
            encrypted_format_identifier=source.format_identifier,
            encryption_algorithm=source.encryption_algorithm,
            kek_provider_identifier=source.kek_provider_identifier,
            kek_key_identifier=source.kek_key_identifier,
            kek_version=source.kek_version,
            encrypted_staging_cleanup_incomplete=cleanup_incomplete,
        )

    def store_encrypted_artifact(self, request):
        try:
            if type(request) is not StoredBackupObjectRequest:
                raise Phase2GCoordinationError()
            context = request.context
            source = request.encrypted_artifact
            key = self._object_key(context)
            if (
                source.encrypted_byte_count < 1
                or source.encrypted_byte_count > self.configuration.maximum_object_bytes
                or not _SHA256.fullmatch(source.ciphertext_sha256)
            ):
                raise DurableObjectValidationError()
            stored_at = self.clock()
            if not isinstance(stored_at, datetime) or stored_at.tzinfo is None:
                raise DurableObjectCreationError()
            reference = self._existing_reference(context, key, source)
            if reference is None:
                try:
                    if source.encrypted_byte_count >= self.configuration.multipart_threshold_bytes:
                        version = self._put_multipart(context, source, key)
                    else:
                        version = self._put_single(context, source, key)
                except (EncryptedArtifactNotFound, EncryptedArtifactValidationError):
                    raise DurableObjectValidationError() from None
                except DurableStorageEngineError:
                    raise
                except Exception as exc:
                    raise _safe_provider_error(exc) from None
                reference = StoredBackupObjectReference(
                    key,
                    bucket_identifier=self.configuration.bucket,
                    version_identifier=version,
                )
            verified_version = self._verify_reference(
                context,
                reference,
                byte_count=source.encrypted_byte_count,
                sha256=source.ciphertext_sha256,
            )
            if verified_version and not reference.version_identifier:
                reference = replace(reference, version_identifier=verified_version)
            result = self._result(context, source, reference, stored_at, cleanup_incomplete=True)
            with self._state_lock:
                self._stored[(context.business_public_id, context.backup_public_id, reference)] = result
            try:
                self.encrypted_artifact_provider.cleanup_encrypted_artifact(
                    context=context,
                    reference=source.reference,
                )
            except EncryptedArtifactCleanupError:
                return result
            completed = replace(result, encrypted_staging_cleanup_incomplete=False)
            with self._state_lock:
                self._stored[(context.business_public_id, context.backup_public_id, reference)] = completed
            return completed
        except DurableStorageEngineError:
            raise
        except Exception as exc:
            raise _safe_provider_error(exc) from None

    def retry_encrypted_staging_cleanup(self, request, result):
        if (
            type(request) is not StoredBackupObjectRequest
            or type(result) is not StoredBackupObjectResult
            or result.backend_identifier != S3_DURABLE_STORAGE_BACKEND_IDENTIFIER
            or result.encrypted_staging_cleanup_incomplete is not True
        ):
            raise EncryptedStagingCleanupError()
        self.validate_stored_object(context=request.context, result=result)
        try:
            self.encrypted_artifact_provider.cleanup_encrypted_artifact(
                context=request.context,
                reference=request.encrypted_artifact.reference,
            )
        except EncryptedArtifactCleanupError:
            raise EncryptedStagingCleanupError(
                encrypted_staging_cleanup_incomplete=True
            ) from None
        completed = replace(result, encrypted_staging_cleanup_incomplete=False)
        with self._state_lock:
            self._stored[(request.context.business_public_id, request.context.backup_public_id, result.reference)] = completed
        return completed

    def validate_stored_object(self, *, context, result):
        if (
            type(result) is not StoredBackupObjectResult
            or result.backend_identifier != S3_DURABLE_STORAGE_BACKEND_IDENTIFIER
            or result.backup_public_id != context.backup_public_id
            or result.tenant_public_id != context.business_public_id
        ):
            raise DurableObjectValidationError()
        self._verify_reference(
            context,
            result.reference,
            byte_count=result.byte_count,
            sha256=result.sha256,
        )
        return True

    def owns_stored_object_reference(self, *, context, reference):
        try:
            self._validate_reference(context, reference)
            return True
        except DurableStorageEngineError:
            return False

    def owns_stored_object_result(self, *, context, result):
        try:
            self.validate_stored_object(context=context, result=result)
            return True
        except DurableStorageEngineError:
            return False

    def reattest_stored_object(self, *, context, descriptor):
        if (
            type(descriptor) is not PersistedStoredObjectDescriptor
            or descriptor.backend_identifier != S3_DURABLE_STORAGE_BACKEND_IDENTIFIER
            or descriptor.backup_public_id != context.backup_public_id
            or descriptor.tenant_public_id != context.business_public_id
            or descriptor.bucket_identifier != descriptor.reference.bucket_identifier
            or descriptor.version_identifier != descriptor.reference.version_identifier
        ):
            raise DurableObjectValidationError()
        self._verify_reference(
            context,
            descriptor.reference,
            byte_count=descriptor.byte_count,
            sha256=descriptor.sha256,
        )
        result = ReattestedStoredObjectResult(
            reference=descriptor.reference,
            backend_identifier=descriptor.backend_identifier,
            object_schema_identifier=STORED_OBJECT_SCHEMA_IDENTIFIER,
            byte_count=descriptor.byte_count,
            sha256=descriptor.sha256,
            backup_public_id=descriptor.backup_public_id,
            tenant_public_id=descriptor.tenant_public_id,
            provider_identifier=S3_DURABLE_STORAGE_PROVIDER_IDENTIFIER,
            attested_at=self.clock().astimezone(UTC),
        )
        with self._state_lock:
            self._reattested[(context.business_public_id, context.backup_public_id, result.reference)] = result
        return result

    def validate_reattested_object(self, *, context, result):
        if type(result) is not ReattestedStoredObjectResult:
            raise DurableObjectValidationError()
        key = (context.business_public_id, context.backup_public_id, result.reference)
        with self._state_lock:
            if self._reattested.get(key) != result:
                raise DurableObjectValidationError()
        self._verify_reference(
            context,
            result.reference,
            byte_count=result.byte_count,
            sha256=result.sha256,
        )
        return True

    @contextmanager
    def _open_verified(self, *, context, reference, byte_count, sha256):
        self._validate_reference(context, reference, error_type=DurableObjectNotFound)
        body = None
        reader = None
        active_error = False
        try:
            try:
                response = self.client.get_object(**self._request_values(reference))
            except ClientError as exc:
                if _error_code(exc) in _NOT_FOUND_CODES:
                    raise DurableObjectNotFound() from None
                raise _safe_provider_error(exc) from None
            except Exception as exc:
                raise _safe_provider_error(exc) from None
            if int(response.get("ContentLength", -1)) != byte_count:
                raise DurableObjectValidationError()
            body = response["Body"]
            reader = _VerifiedRemoteReader(
                body,
                byte_count=byte_count,
                sha256=sha256,
                maximum_read_bytes=self.configuration.chunk_bytes,
            )
            yield reader
        except BaseException:
            active_error = True
            raise
        finally:
            validation_error = None
            if reader is not None and not active_error:
                try:
                    reader.validate()
                except BaseException as exc:
                    validation_error = exc
            if body is not None:
                try:
                    body.close()
                except BaseException as exc:
                    if not active_error and validation_error is None:
                        validation_error = exc
            if validation_error is not None and not active_error:
                raise DurableObjectValidationError() from None

    def open_reattested_object(self, *, context, result):
        self.validate_reattested_object(context=context, result=result)
        return self._open_verified(
            context=context,
            reference=result.reference,
            byte_count=result.byte_count,
            sha256=result.sha256,
        )

    def release_reattested_object(self, *, context, result):
        key = (context.business_public_id, context.backup_public_id, result.reference)
        with self._state_lock:
            if self._reattested.get(key) != result:
                raise DurableObjectValidationError()
            del self._reattested[key]
        return True

    def open_stored_object(self, *, context, reference):
        key = (context.business_public_id, context.backup_public_id, reference)
        with self._state_lock:
            result = self._stored.get(key)
        if result is None:
            raise DurableObjectNotFound()
        return self._open_verified(
            context=context,
            reference=reference,
            byte_count=result.byte_count,
            sha256=result.sha256,
        )

    def delete_stored_object(self, *, context, reference):
        self._validate_reference(context, reference, error_type=DurableObjectCleanupError)
        values = self._request_values(reference)
        try:
            self.client.delete_object(**values)
        except Exception as exc:
            raise _safe_provider_error(exc, cleanup=True) from None
        if not self.confirm_stored_object_absent(context=context, reference=reference):
            raise DurableObjectCleanupError()
        with self._state_lock:
            self._deleted.add((context.business_public_id, context.backup_public_id, reference))
            self._stored.pop((context.business_public_id, context.backup_public_id, reference), None)
        return True

    def confirm_stored_object_absent(self, *, context, reference):
        try:
            self._validate_reference(context, reference)
            self._head(reference)
            return False
        except DurableObjectNotFound:
            return True
        except DurableStorageEngineError:
            return False


__all__ = [
    "S3_DURABLE_STORAGE_BACKEND_IDENTIFIER",
    "S3_DURABLE_STORAGE_PROVIDER_IDENTIFIER",
    "S3_MAX_ATTEMPTS",
    "S3CompatibleDurableStorageProvider",
    "S3StorageConfiguration",
    "normalize_s3_prefix",
]
