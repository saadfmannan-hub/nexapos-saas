"""Focused production S3-compatible durable-storage tests for Phase 3G."""

from __future__ import annotations

import hashlib
import io
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from botocore.exceptions import ClientError
from django.core.checks import run_checks
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from apps.backups.engine import availability
from apps.backups.engine.checks import (
    check_production_storage_provider_selection,
    check_storage_provider_configuration,
)
from apps.backups.engine.context import ActorIdentitySnapshot, BackupExecutionContext
from apps.backups.engine.contracts import (
    EncryptedArtifactReference,
    EncryptedArtifactResult,
    PersistedStoredObjectDescriptor,
    StoredBackupObjectReference,
    StoredBackupObjectRequest,
    StoredBackupObjectResult,
    StoredObjectDurabilityState,
    StoredObjectVerificationState,
)
from apps.backups.engine.durable_storage import (
    LOCAL_DURABLE_STORAGE_BACKEND_IDENTIFIER,
    LocalPrivateDurableStorageProvider,
)
from apps.backups.engine.durable_storage_exceptions import (
    DurableObjectValidationError,
    DurableStorageAuthorizationError,
    DurableStoragePolicyError,
    DurableStorageUnavailable,
)
from apps.backups.engine.encrypted_artifact import EncryptedArtifactProvider
from apps.backups.engine.retention import (
    BackupRetentionClass,
    RetentionCandidate,
    RetentionEngine,
    RetentionExecutionState,
)
from apps.backups.engine.retention_policy import RetentionPolicy
from apps.backups.engine.runtime import BackupExecutionCoordinator
from apps.backups.engine.s3_storage import (
    S3_DURABLE_STORAGE_BACKEND_IDENTIFIER,
    S3_DURABLE_STORAGE_PROVIDER_IDENTIFIER,
    S3_MAX_ATTEMPTS,
    S3CompatibleDurableStorageProvider,
    S3StorageConfiguration,
    normalize_s3_prefix,
)
from apps.backups.engine.storage_registry import (
    DurableStorageProviderRegistry,
    build_storage_provider_registry,
    stored_reference_from_metadata,
    validate_storage_provider_settings,
)
from apps.backups.engine.workspace import WorkspaceReference
from apps.backups.enums import BackupScope, BackupTrigger, ProductOwner
from apps.backups.models import BackupRecord
from apps.backups.platform_selectors import safe_storage_label

from .test_backups_phase1 import BackupPhase1TestCase

_S3_SETTINGS = {
    "BACKUP_STORAGE_PROVIDER": "s3",
    "BACKUP_S3_BUCKET": "nexa-backups-test",
    "BACKUP_S3_REGION": "nyc3",
    "BACKUP_S3_ENDPOINT_URL": "https://nyc3.digitaloceanspaces.com",
    "BACKUP_S3_ACCESS_KEY_ID": "spaces-test-access-id",
    "BACKUP_S3_SECRET_ACCESS_KEY": "spaces-test-secret-key",
    "BACKUP_S3_PREFIX": "nexa/backups",
    "BACKUP_S3_ADDRESSING_STYLE": "virtual",
    "BACKUP_S3_MULTIPART_THRESHOLD_BYTES": 8 * 1024**2,
    "BACKUP_S3_MULTIPART_PART_BYTES": 5 * 1024**2,
    "BACKUP_S3_CONNECT_TIMEOUT_SECONDS": 5.0,
    "BACKUP_S3_READ_TIMEOUT_SECONDS": 30.0,
    "BACKUP_DURABLE_STORAGE_MAX_OBJECT_BYTES": 65 * 1024**2,
    "BACKUP_DURABLE_STORAGE_CHUNK_BYTES": 4096,
}


def _client_error(code, operation):
    return ClientError(
        {
            "Error": {"Code": code, "Message": "raw-provider-secret"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        operation,
    )


class _FakeS3Client:
    def __init__(self):
        self.calls = []
        self.objects = {}
        self.current_versions = {}
        self.uploads = {}
        self.failures = {}
        self.corrupt_head_size = False
        self._version = 0
        self._upload = 0

    def _fail(self, method):
        failure = self.failures.get(method)
        if failure:
            raise _client_error(failure, method)

    def _new_version(self):
        self._version += 1
        return f"version-{self._version}"

    def _resolve(self, bucket, key, version=""):
        selected = version or self.current_versions.get((bucket, key), "")
        value = self.objects.get((bucket, key, selected))
        if value is None:
            raise _client_error("NoSuchKey", "HeadObject")
        return selected, value

    def head_bucket(self, **kwargs):
        self.calls.append(("head_bucket", kwargs))
        self._fail("head_bucket")
        return {}

    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        self._fail("head_object")
        version, value = self._resolve(
            kwargs["Bucket"],
            kwargs["Key"],
            kwargs.get("VersionId", ""),
        )
        return {
            "ContentLength": len(value["data"]) + (1 if self.corrupt_head_size else 0),
            "Metadata": dict(value["metadata"]),
            "VersionId": version,
            "ETag": '"multipart-etag-not-a-sha256"',
        }

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        self._fail("get_object")
        version, value = self._resolve(
            kwargs["Bucket"],
            kwargs["Key"],
            kwargs.get("VersionId", ""),
        )
        return {
            "Body": io.BytesIO(value["data"]),
            "ContentLength": len(value["data"]),
            "VersionId": version,
        }

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))
        self._fail("put_object")
        identity = (kwargs["Bucket"], kwargs["Key"])
        if kwargs.get("IfNoneMatch") == "*" and identity in self.current_versions:
            raise _client_error("PreconditionFailed", "PutObject")
        chunks = []
        while True:
            chunk = kwargs["Body"].read(4096)
            if not chunk:
                break
            chunks.append(chunk)
        version = self._new_version()
        self.objects[(*identity, version)] = {
            "data": b"".join(chunks),
            "metadata": dict(kwargs["Metadata"]),
        }
        self.current_versions[identity] = version
        return {"VersionId": version, "ETag": '"opaque-etag"'}

    def create_multipart_upload(self, **kwargs):
        self.calls.append(("create_multipart_upload", kwargs))
        self._fail("create_multipart_upload")
        self._upload += 1
        upload_id = f"upload-{self._upload}"
        self.uploads[upload_id] = {"request": kwargs, "parts": {}}
        return {"UploadId": upload_id}

    def upload_part(self, **kwargs):
        self.calls.append(("upload_part", kwargs))
        self._fail("upload_part")
        self.uploads[kwargs["UploadId"]]["parts"][kwargs["PartNumber"]] = kwargs["Body"]
        return {"ETag": f'"part-{kwargs["PartNumber"]}"'}

    def complete_multipart_upload(self, **kwargs):
        self.calls.append(("complete_multipart_upload", kwargs))
        self._fail("complete_multipart_upload")
        upload = self.uploads[kwargs["UploadId"]]
        identity = (kwargs["Bucket"], kwargs["Key"])
        if kwargs.get("IfNoneMatch") == "*" and identity in self.current_versions:
            raise _client_error("PreconditionFailed", "CompleteMultipartUpload")
        data = b"".join(upload["parts"][number] for number in sorted(upload["parts"]))
        version = self._new_version()
        self.objects[(*identity, version)] = {
            "data": data,
            "metadata": dict(upload["request"]["Metadata"]),
        }
        self.current_versions[identity] = version
        del self.uploads[kwargs["UploadId"]]
        return {"VersionId": version, "ETag": '"multipart-2"'}

    def abort_multipart_upload(self, **kwargs):
        self.calls.append(("abort_multipart_upload", kwargs))
        self.uploads.pop(kwargs["UploadId"], None)
        return {}

    def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))
        self._fail("delete_object")
        identity = (kwargs["Bucket"], kwargs["Key"])
        version = kwargs.get("VersionId") or self.current_versions.get(identity, "")
        self.objects.pop((*identity, version), None)
        if self.current_versions.get(identity) == version:
            self.current_versions.pop(identity, None)
        return {"VersionId": version}


class Phase3GS3StorageTests(SimpleTestCase):
    def setUp(self):
        self.raw = b"NEXA-ENCRYPTED-ARTIFACT\x00" + b"ciphertext" * 2048
        self.context = self._context()
        self.source = self._source(self.raw)
        self.artifact_provider = object.__new__(EncryptedArtifactProvider)
        self.cleaned = []

        @contextmanager
        def open_artifact(*, context, reference):
            if context != self.context or reference != self.source.reference:
                raise DurableObjectValidationError()
            yield io.BytesIO(self.raw)

        def cleanup_artifact(*, context, reference):
            if context != self.context or reference != self.source.reference:
                raise DurableObjectValidationError()
            self.cleaned.append(reference)

        self.artifact_provider.open_encrypted_artifact = open_artifact
        self.artifact_provider.cleanup_encrypted_artifact = cleanup_artifact
        self.client = _FakeS3Client()
        self.configuration = S3StorageConfiguration(
            bucket="nexa-backups-test",
            region="nyc3",
            endpoint_url="https://nyc3.digitaloceanspaces.com",
            access_key_id="spaces-test-access-id",
            secret_access_key="spaces-test-secret-key",
            prefix="nexa/backups",
            addressing_style="virtual",
            multipart_threshold_bytes=8 * 1024**2,
            multipart_part_bytes=5 * 1024**2,
            connect_timeout_seconds=5,
            read_timeout_seconds=30,
            maximum_object_bytes=65 * 1024**2,
            chunk_bytes=4096,
        )

    @staticmethod
    def _context(*, backup_id=None, tenant_id=None, business_id=991):
        backup_id = backup_id or uuid.uuid4()
        tenant_id = tenant_id or uuid.uuid4()
        return BackupExecutionContext(
            backup_public_id=backup_id,
            business_id=business_id,
            business_public_id=tenant_id,
            requested_scope=BackupScope.ALL_ENABLED,
            resolved_products=(ProductOwner.POS,),
            trigger_type=BackupTrigger.SCHEDULED,
            actor_identity=ActorIdentitySnapshot("", "", "", "SYSTEM", False),
            application_version="3g-test",
            backup_format_version="1.0",
            schema_migration_fingerprint="a" * 64,
            minimum_restore_version="1.0",
            idempotency_key=f"phase3g-{backup_id}",
            operation_correlation_id=backup_id,
            workspace_reference=WorkspaceReference(backup_id),
        )

    @staticmethod
    def _source(raw):
        digest = hashlib.sha256(raw).hexdigest()
        return EncryptedArtifactResult(
            reference=EncryptedArtifactReference(uuid.uuid4()),
            encrypted_byte_count=len(raw),
            ciphertext_sha256=digest,
            plaintext_byte_count=100,
            plaintext_sha256="b" * 64,
            header_sha256="c" * 64,
            format_identifier="nexa.encrypted-backup.v1",
            encryption_algorithm="AES-256-GCM",
            kek_provider_identifier="aws-kms-v1",
            kek_key_identifier="alias/nexa-backups",
            kek_version="arn:aws:kms:nyc3:111122223333:key/test",
            created_at=datetime.now(UTC),
            provider_identifier="nexa.encrypted-artifact.v1",
            plaintext_cleanup_incomplete=False,
        )

    def _provider(self, *, client=None, configuration=None):
        return S3CompatibleDurableStorageProvider(
            encrypted_artifact_provider=self.artifact_provider,
            configuration=configuration or self.configuration,
            client_factory=lambda: client or self.client,
        )

    def _store(self, provider=None):
        provider = provider or self._provider()
        result = provider.store_encrypted_artifact(
            StoredBackupObjectRequest(context=self.context, encrypted_artifact=self.source)
        )
        return provider, result

    def test_provider_registry_resolves_local_and_s3_without_fallback(self):
        local = object.__new__(LocalPrivateDurableStorageProvider)
        s3 = self._provider()
        registry = DurableStorageProviderRegistry(
            active_provider=s3,
            factories={LOCAL_DURABLE_STORAGE_BACKEND_IDENTIFIER: lambda: local},
        )
        self.assertIs(registry.resolve(S3_DURABLE_STORAGE_BACKEND_IDENTIFIER), s3)
        self.assertIs(registry.resolve(LOCAL_DURABLE_STORAGE_BACKEND_IDENTIFIER), local)
        with self.assertRaises(DurableObjectValidationError):
            registry.resolve("unknown-storage")

    def test_local_provider_is_development_only_and_s3_is_production_capable(self):
        self.assertTrue(LocalPrivateDurableStorageProvider.development_only)
        self.assertFalse(S3CompatibleDurableStorageProvider.development_only)

    @override_settings(**_S3_SETTINGS)
    def test_s3_configuration_parses_spaces_endpoint_and_safe_prefix(self):
        configuration = S3StorageConfiguration.from_settings()
        self.assertEqual(validate_storage_provider_settings(), "s3")
        self.assertEqual(configuration.endpoint_url, "https://nyc3.digitaloceanspaces.com")
        self.assertEqual(configuration.prefix, "nexa/backups")

    def test_missing_bucket_region_endpoint_and_unsafe_prefix_fail_closed(self):
        for change in (
            {"bucket": ""},
            {"region": ""},
            {"endpoint_url": ""},
            {"prefix": "../customer"},
            {"prefix": "nexa\\backups"},
            {"prefix": "nexa/backups?public=true"},
        ):
            with self.subTest(change=change), self.assertRaises(DurableStoragePolicyError):
                replace(self.configuration, **change).validated()

    def test_client_is_lazy_and_uses_dedicated_credentials_with_bounded_sdk_config(self):
        with mock.patch("apps.backups.engine.s3_storage.boto3.client") as factory:
            factory.return_value = self.client
            provider = S3CompatibleDurableStorageProvider(
                encrypted_artifact_provider=self.artifact_provider,
                configuration=self.configuration,
            )
            self.assertFalse(provider.client_created)
            provider.health_attestation()
        kwargs = factory.call_args.kwargs
        self.assertEqual(kwargs["aws_access_key_id"], "spaces-test-access-id")
        self.assertEqual(kwargs["aws_secret_access_key"], "spaces-test-secret-key")
        self.assertEqual(kwargs["config"].retries["total_max_attempts"], S3_MAX_ATTEMPTS)
        self.assertEqual(kwargs["config"].s3["addressing_style"], "virtual")

    def test_upload_is_private_encrypted_only_and_uses_public_uuid_key(self):
        _provider, result = self._store()
        put = next(kwargs for name, kwargs in self.client.calls if name == "put_object")
        self.assertEqual(put["ACL"], "private")
        self.assertEqual(put["ContentType"], "application/octet-stream")
        self.assertNotIn("IfNoneMatch", put)
        self.assertEqual(
            put["Key"],
            f"nexa/backups/{self.context.business_public_id}/{self.context.backup_public_id}/artifact.bin",
        )
        self.assertNotIn(str(self.context.business_id), put["Key"])
        self.assertEqual(result.sha256, hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(result.provider_identifier, S3_DURABLE_STORAGE_PROVIDER_IDENTIFIER)
        self.assertEqual(self.cleaned, [self.source.reference])

    def test_upload_verifies_head_size_metadata_and_independent_stream_hash(self):
        _provider, _result = self._store()
        names = [name for name, _kwargs in self.client.calls]
        self.assertIn("head_object", names)
        self.assertIn("get_object", names)
        put = next(kwargs for name, kwargs in self.client.calls if name == "put_object")
        self.assertEqual(put["Metadata"]["sha256"], hashlib.sha256(self.raw).hexdigest())
        self.assertNotEqual('"opaque-etag"'.strip('"'), put["Metadata"]["sha256"])

    def test_failed_remote_verification_preserves_encrypted_staging(self):
        self.client.corrupt_head_size = True
        with self.assertRaises(DurableObjectValidationError):
            self._store()
        self.assertEqual(self.cleaned, [])

    def test_verified_upload_allows_encrypted_staging_cleanup(self):
        self._store()
        self.assertEqual(self.cleaned, [self.source.reference])

    def test_large_upload_uses_multipart_and_aborts_known_failure(self):
        raw = b"x" * (5 * 1024**2 + 1)
        self.raw = raw
        self.source = self._source(raw)
        configuration = replace(
            self.configuration,
            multipart_threshold_bytes=5 * 1024**2,
        )
        provider = self._provider(configuration=configuration)
        self.client.failures["upload_part"] = "InternalError"
        with self.assertRaises(DurableStorageUnavailable):
            self._store(provider)
        names = [name for name, _kwargs in self.client.calls]
        self.assertIn("create_multipart_upload", names)
        self.assertIn("abort_multipart_upload", names)
        self.assertNotIn("complete_multipart_upload", names)
        self.assertEqual(self.cleaned, [])

    def test_successful_large_upload_completes_bounded_parts(self):
        raw = b"x" * (5 * 1024**2 + 1)
        self.raw = raw
        self.source = self._source(raw)
        provider = self._provider(
            configuration=replace(
                self.configuration,
                multipart_threshold_bytes=5 * 1024**2,
            )
        )
        _provider, result = self._store(provider)
        uploads = [kwargs for name, kwargs in self.client.calls if name == "upload_part"]
        self.assertEqual(len(uploads), 2)
        completed = next(
            kwargs for name, kwargs in self.client.calls if name == "complete_multipart_upload"
        )
        self.assertNotIn("IfNoneMatch", completed)
        self.assertEqual(result.byte_count, len(raw))

    def test_matching_duplicate_is_idempotent_but_mismatch_fails(self):
        self._store()
        self.cleaned.clear()
        second = self._provider()
        _provider, duplicate = self._store(second)
        self.assertEqual(duplicate.sha256, self.source.ciphertext_sha256)
        puts = [call for call in self.client.calls if call[0] == "put_object"]
        self.assertEqual(len(puts), 1)
        self.raw = b"different encrypted artifact"
        self.source = self._source(self.raw)
        with self.assertRaises(DurableObjectValidationError):
            self._store(self._provider())

    def test_historical_retrieval_uses_persisted_bucket_key_and_version(self):
        provider, result = self._store()
        historical = replace(
            result.reference,
            bucket_identifier=result.reference.bucket_identifier,
            version_identifier=result.reference.version_identifier,
        )
        descriptor = PersistedStoredObjectDescriptor(
            reference=historical,
            backend_identifier=S3_DURABLE_STORAGE_BACKEND_IDENTIFIER,
            byte_count=result.byte_count,
            sha256=result.sha256,
            backup_public_id=self.context.backup_public_id,
            tenant_public_id=self.context.business_public_id,
            bucket_identifier=historical.bucket_identifier,
            version_identifier=historical.version_identifier,
        )
        attested = provider.reattest_stored_object(context=self.context, descriptor=descriptor)
        with provider.open_reattested_object(context=self.context, result=attested) as reader:
            restored = b""
            while True:
                chunk = reader.read()
                if not chunk:
                    break
                restored += chunk
            self.assertEqual(restored, self.raw)
        gets = [kwargs for name, kwargs in self.client.calls if name == "get_object"]
        self.assertTrue(all(call["Bucket"] == historical.bucket_identifier for call in gets))
        self.assertTrue(all(call["VersionId"] == historical.version_identifier for call in gets))

    def test_historical_bucket_does_not_fall_back_to_current_bucket_setting(self):
        provider, result = self._store()
        old_bucket = "historical-backups-test"
        identity = (result.reference.bucket_identifier, result.reference.identifier)
        version = result.reference.version_identifier
        value = self.client.objects.pop((*identity, version))
        self.client.current_versions.pop(identity)
        self.client.objects[(old_bucket, result.reference.identifier, version)] = value
        self.client.current_versions[(old_bucket, result.reference.identifier)] = version
        historical = replace(result.reference, bucket_identifier=old_bucket)
        descriptor = PersistedStoredObjectDescriptor(
            reference=historical,
            backend_identifier=S3_DURABLE_STORAGE_BACKEND_IDENTIFIER,
            byte_count=result.byte_count,
            sha256=result.sha256,
            backup_public_id=self.context.backup_public_id,
            tenant_public_id=self.context.business_public_id,
            bucket_identifier=old_bucket,
            version_identifier=version,
        )
        attested = provider.reattest_stored_object(context=self.context, descriptor=descriptor)
        self.assertEqual(attested.reference.bucket_identifier, old_bucket)

    def test_retrieval_rejects_wrong_size_hash_key_and_cross_tenant_binding(self):
        provider, result = self._store()
        for descriptor in (
            PersistedStoredObjectDescriptor(
                result.reference,
                result.backend_identifier,
                result.byte_count + 1,
                result.sha256,
                result.backup_public_id,
                result.tenant_public_id,
                result.reference.bucket_identifier,
                result.reference.version_identifier,
            ),
            PersistedStoredObjectDescriptor(
                result.reference,
                result.backend_identifier,
                result.byte_count,
                "0" * 64,
                result.backup_public_id,
                result.tenant_public_id,
                result.reference.bucket_identifier,
                result.reference.version_identifier,
            ),
            PersistedStoredObjectDescriptor(
                replace(result.reference, identifier="nexa/backups/wrong/artifact.bin"),
                result.backend_identifier,
                result.byte_count,
                result.sha256,
                result.backup_public_id,
                result.tenant_public_id,
                result.reference.bucket_identifier,
                result.reference.version_identifier,
            ),
        ):
            with self.assertRaises(DurableObjectValidationError):
                provider.reattest_stored_object(context=self.context, descriptor=descriptor)
        other_context = replace(self.context, business_public_id=uuid.uuid4())
        with self.assertRaises(DurableObjectValidationError):
            provider.reattest_stored_object(
                context=other_context,
                descriptor=PersistedStoredObjectDescriptor(
                    result.reference,
                    result.backend_identifier,
                    result.byte_count,
                    result.sha256,
                    result.backup_public_id,
                    result.tenant_public_id,
                    result.reference.bucket_identifier,
                    result.reference.version_identifier,
                ),
            )

    def test_corrupted_remote_object_fails_closed(self):
        provider, result = self._store()
        identity = (
            result.reference.bucket_identifier,
            result.reference.identifier,
            result.reference.version_identifier,
        )
        self.client.objects[identity]["data"] += b"corruption"
        with self.assertRaises(DurableObjectValidationError):
            provider.validate_stored_object(context=self.context, result=result)

    def test_no_list_discovery_or_presigned_public_url_is_used(self):
        self._store()
        names = {name for name, _kwargs in self.client.calls}
        self.assertNotIn("list_objects", names)
        self.assertNotIn("list_objects_v2", names)
        self.assertNotIn("generate_presigned_url", names)

    def test_version_aware_retention_delete_targets_exact_object_only(self):
        provider, result = self._store()
        provider.delete_stored_object(context=self.context, reference=result.reference)
        deletion = next(kwargs for name, kwargs in self.client.calls if name == "delete_object")
        self.assertEqual(
            deletion,
            {
                "Bucket": result.reference.bucket_identifier,
                "Key": result.reference.identifier,
                "VersionId": result.reference.version_identifier,
            },
        )
        self.assertNotIn("*", deletion["Key"])
        self.assertTrue(
            provider.confirm_stored_object_absent(
                context=self.context,
                reference=result.reference,
            )
        )

    def test_retention_engine_deletes_only_the_exact_old_s3_version(self):
        provider, first_result = self._store()
        first_context = self.context
        first_result = replace(
            first_result,
            stored_at=datetime.now(UTC) - timedelta(hours=1),
        )
        self.cleaned.clear()
        self.context = self._context(tenant_id=first_context.business_public_id)
        self.source = self._source(self.raw)
        _provider, second_result = self._store(provider)
        candidates = (
            RetentionCandidate(
                context=first_context,
                stored_object=first_result,
                retention_class=BackupRetentionClass.DAILY_FULL,
                package_verified=True,
                encrypted_artifact_valid=True,
                durable_verified=True,
            ),
            RetentionCandidate(
                context=self.context,
                stored_object=second_result,
                retention_class=BackupRetentionClass.DAILY_FULL,
                package_verified=True,
                encrypted_artifact_valid=True,
                durable_verified=True,
            ),
        )
        engine = RetentionEngine(
            durable_provider=provider,
            policy=RetentionPolicy(1, 10, 30),
        )
        plan = engine.build_retention_plan(
            tenant_public_id=first_context.business_public_id,
            candidates=candidates,
        )
        outcome = engine.execute_retention_plan(
            plan=plan,
            current_candidates=candidates,
        )
        self.assertEqual(outcome.execution_state, RetentionExecutionState.COMPLETED)
        deletion = next(kwargs for name, kwargs in self.client.calls if name == "delete_object")
        self.assertEqual(deletion["Key"], first_result.reference.identifier)
        self.assertEqual(deletion["VersionId"], first_result.reference.version_identifier)
        self.assertNotEqual(deletion["Key"], second_result.reference.identifier)

    def test_failed_delete_is_not_reported_as_absent(self):
        provider, result = self._store()
        self.client.failures["delete_object"] = "AccessDenied"
        with self.assertRaises(DurableStorageAuthorizationError):
            provider.delete_stored_object(context=self.context, reference=result.reference)
        self.client.failures.clear()
        self.assertFalse(
            provider.confirm_stored_object_absent(
                context=self.context,
                reference=result.reference,
            )
        )

    def test_retention_engine_never_deletes_protected_safety_backup(self):
        provider, result = self._store()
        candidate = RetentionCandidate(
            context=self.context,
            stored_object=result,
            retention_class=BackupRetentionClass.DAILY_FULL,
            package_verified=True,
            encrypted_artifact_valid=True,
            durable_verified=True,
            protected=True,
        )
        engine = RetentionEngine(
            durable_provider=provider,
            policy=RetentionPolicy(1, 10, 30),
        )
        plan = engine.build_retention_plan(
            tenant_public_id=self.context.business_public_id,
            candidates=(candidate,),
        )
        outcome = engine.execute_retention_plan(plan=plan, current_candidates=(candidate,))
        self.assertEqual(outcome.execution_state, RetentionExecutionState.NO_ACTION_REQUIRED)
        self.assertFalse(any(name == "delete_object" for name, _kwargs in self.client.calls))

    def test_throttling_and_transient_failures_are_retryable_and_bounded(self):
        for code in ("SlowDown", "ServiceUnavailable"):
            client = _FakeS3Client()
            client.failures["head_object"] = code
            provider = self._provider(client=client)
            with self.subTest(code=code), self.assertRaises(DurableStorageUnavailable) as raised:
                self._store(provider)
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(S3_MAX_ATTEMPTS, 3)

    def test_auth_failure_is_nonretryable_and_provider_message_is_sanitized(self):
        self.client.failures["head_object"] = "AccessDenied"
        with self.assertRaises(DurableStorageAuthorizationError) as raised:
            self._store()
        self.assertFalse(raised.exception.retryable)
        self.assertNotIn("raw-provider-secret", str(raised.exception))

    def test_credentials_are_not_hardcoded_or_logged(self):
        source = Path("apps/backups/engine/s3_storage.py").read_text(encoding="utf-8")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY=", source)
        self.assertNotIn("AWS_ACCESS_KEY_ID=", source)
        self.assertNotIn("generate_presigned_url", source)

    def test_exact_provider_metadata_resolution_preserves_legacy_local_semantics(self):
        local_id = uuid.uuid4()
        local = stored_reference_from_metadata(
            backend_identifier=LOCAL_DURABLE_STORAGE_BACKEND_IDENTIFIER,
            opaque_object_key=str(local_id),
        )
        self.assertEqual(local.identifier, local_id)
        with self.assertRaises(DurableObjectValidationError):
            stored_reference_from_metadata(
                backend_identifier=LOCAL_DURABLE_STORAGE_BACKEND_IDENTIFIER,
                opaque_object_key=str(local_id),
                bucket_identifier="must-not-reinterpret",
            )
        with self.assertRaises(DurableObjectValidationError):
            stored_reference_from_metadata(
                backend_identifier="",
                opaque_object_key=str(local_id),
            )

    def test_s3_reference_requires_persisted_bucket_and_keeps_version(self):
        key = f"nexa/backups/{self.context.business_public_id}/{self.context.backup_public_id}/artifact.bin"
        reference = stored_reference_from_metadata(
            backend_identifier=S3_DURABLE_STORAGE_BACKEND_IDENTIFIER,
            opaque_object_key=key,
            bucket_identifier="historical-bucket",
            version_identifier="historical-version",
        )
        self.assertEqual(reference.bucket_identifier, "historical-bucket")
        self.assertEqual(reference.version_identifier, "historical-version")
        with self.assertRaises(DurableObjectValidationError):
            stored_reference_from_metadata(
                backend_identifier=S3_DURABLE_STORAGE_BACKEND_IDENTIFIER,
                opaque_object_key=key,
            )

    @override_settings(
        BACKUP_EXECUTION_ENGINE_ENABLED=False,
        BACKUP_ENGINE_ENABLED=False,
        BACKUP_RESTORE_MUTATION_ENABLED=False,
        BACKUP_STORAGE_PROVIDER="local",
        BACKUP_S3_BUCKET="",
        BACKUP_S3_REGION="",
        BACKUP_S3_ENDPOINT_URL="",
        BACKUP_S3_ACCESS_KEY_ID="",
        BACKUP_S3_SECRET_ACCESS_KEY="",
    )
    def test_disabled_engine_with_missing_s3_configuration_is_deployment_safe(self):
        self.assertEqual(check_storage_provider_configuration(None), [])
        self.assertEqual(check_production_storage_provider_selection(None), [])
        self.assertFalse(availability.real_execution_available())
        self.assertFalse(availability.restore_execution_available())
        self.assertFalse(any(error.id in {"backups.E048", "backups.E049"} for error in run_checks()))

    @override_settings(
        BACKUP_EXECUTION_ENGINE_ENABLED=True,
        BACKUP_ENGINE_ENABLED=True,
        BACKUP_RESTORE_MUTATION_ENABLED=False,
        BACKUP_STORAGE_PROVIDER="s3",
        BACKUP_S3_BUCKET="",
        BACKUP_S3_REGION="",
        BACKUP_S3_ENDPOINT_URL="",
        BACKUP_S3_ACCESS_KEY_ID="",
        BACKUP_S3_SECRET_ACCESS_KEY="",
    )
    def test_enabled_engine_with_unsafe_s3_configuration_fails_strict_check(self):
        self.assertEqual(
            [error.id for error in check_storage_provider_configuration(None)],
            ["backups.E048"],
        )

    @override_settings(
        BACKUP_EXECUTION_ENGINE_ENABLED=True,
        BACKUP_ENGINE_ENABLED=True,
        BACKUP_STORAGE_PROVIDER="local",
    )
    def test_production_activation_rejects_local_storage(self):
        self.assertEqual(
            [error.id for error in check_production_storage_provider_selection(None)],
            ["backups.E049"],
        )

    @override_settings(**_S3_SETTINGS)
    def test_safety_backup_composition_selects_current_production_storage(self):
        registry = build_storage_provider_registry(
            encrypted_artifact_provider=self.artifact_provider
        )
        self.assertIsInstance(registry.active_provider, S3CompatibleDurableStorageProvider)
        self.assertFalse(registry.active_provider.client_created)

    def test_platform_label_is_safe_and_owner_templates_do_not_render_storage_internals(self):
        backup = SimpleNamespace(
            status="SUCCEEDED",
            integrity_status="VERIFIED",
            deleted_at=None,
            storage_backend_identifier=S3_DURABLE_STORAGE_BACKEND_IDENTIFIER,
            opaque_object_key="secret/object/key",
            whole_artifact_hash="a" * 64,
        )
        self.assertEqual(safe_storage_label(backup), "S3-compatible storage")
        owner_templates = "\n".join(
            path.read_text(encoding="utf-8")
            for path in Path("templates/backups").glob("*.html")
        )
        for secret_field in (
            "opaque_object_key",
            "storage_bucket_identifier",
            "storage_object_version_identifier",
        ):
            self.assertNotIn("{{ backup." + secret_field, owner_templates)

    def test_capability_flags_remain_safely_disabled(self):
        self.assertTrue(availability.PRODUCTION_KEY_PROVIDER_READY)
        self.assertTrue(availability.PRODUCTION_DURABLE_STORAGE_PROVIDER_READY)
        self.assertFalse(availability.OPERATIONAL_PROVIDER_STACK_READY)
        self.assertFalse(availability.real_execution_available())
        self.assertFalse(availability.restore_execution_available())

    def test_migration_fields_are_exact_blank_default_contract(self):
        bucket = BackupRecord._meta.get_field("storage_bucket_identifier")
        version = BackupRecord._meta.get_field("storage_object_version_identifier")
        self.assertEqual(bucket.max_length, 255)
        self.assertTrue(bucket.blank)
        self.assertEqual(bucket.get_default(), "")
        self.assertEqual(version.max_length, 1024)
        self.assertTrue(version.blank)
        self.assertEqual(version.get_default(), "")

    def test_spaces_sse_is_not_assumed_to_replace_application_encryption(self):
        self._store()
        request = next(kwargs for name, kwargs in self.client.calls if name == "put_object")
        self.assertNotIn("ServerSideEncryption", request)
        self.assertEqual(self.source.encryption_algorithm, "AES-256-GCM")

    def test_health_attestation_is_non_destructive(self):
        provider = self._provider()
        self.assertTrue(provider.health_attestation())
        self.assertEqual(self.client.calls, [("head_bucket", {"Bucket": "nexa-backups-test"})])

    def test_prefix_normalization_rejects_absolute_and_empty_components(self):
        self.assertEqual(normalize_s3_prefix("nexa/backups/"), "nexa/backups")
        for value in (
            "/nexa/backups",
            "nexa//backups",
            "nexa/../backups",
            "nexa/./backups",
        ):
            with self.subTest(value=value), self.assertRaises(DurableStoragePolicyError):
                normalize_s3_prefix(value)

    def test_result_preserves_phase3f_kms_metadata(self):
        _provider, result = self._store()
        self.assertEqual(result.kek_provider_identifier, "aws-kms-v1")
        self.assertEqual(result.kek_key_identifier, "alias/nexa-backups")
        self.assertTrue(result.kek_version.startswith("arn:aws:kms:"))


class Phase3GStoragePersistenceTests(BackupPhase1TestCase):
    def test_s3_bucket_and_version_publish_atomically_with_existing_evidence(self):
        backup = self.make_backup()
        self.assertEqual(backup.storage_bucket_identifier, "")
        self.assertEqual(backup.storage_object_version_identifier, "")
        key = (
            f"nexa/backups/{self.business_a.public_id}/{backup.public_id}/artifact.bin"
        )
        result = StoredBackupObjectResult(
            reference=StoredBackupObjectReference(
                key,
                bucket_identifier="nexa-backups-test",
                version_identifier="version-42",
            ),
            backend_identifier=S3_DURABLE_STORAGE_BACKEND_IDENTIFIER,
            object_schema_identifier="nexa.stored-backup-object.v1",
            byte_count=4096,
            sha256="a" * 64,
            source_encrypted_artifact_sha256="a" * 64,
            backup_public_id=backup.public_id,
            tenant_public_id=self.business_a.public_id,
            stored_at=timezone.now(),
            provider_identifier=S3_DURABLE_STORAGE_PROVIDER_IDENTIFIER,
            durability_state=StoredObjectDurabilityState.STORED,
            verification_state=StoredObjectVerificationState.STORED_AND_VERIFIED,
            encrypted_format_identifier="nexa.encrypted-backup.v1",
            encryption_algorithm="AES-256-GCM",
            kek_provider_identifier="aws-kms-v1",
            kek_key_identifier="alias/nexa-backups",
            kek_version="arn:aws:kms:nyc3:111122223333:key/test",
            encrypted_staging_cleanup_incomplete=False,
        )
        coordinator = object.__new__(BackupExecutionCoordinator)
        coordinator.clock = timezone.now
        updated = coordinator._persist_durable_metadata(
            backup=backup,
            stored=result,
            phase2d1_result=SimpleNamespace(
                manifest=SimpleNamespace(
                    total_record_count=7,
                    component_count=2,
                    unique_media_object_count=1,
                )
            ),
            verification=SimpleNamespace(restore_ready=True),
        )
        self.assertEqual(updated.storage_backend_identifier, "s3-compatible")
        self.assertEqual(updated.opaque_object_key, key)
        self.assertEqual(updated.storage_bucket_identifier, "nexa-backups-test")
        self.assertEqual(updated.storage_object_version_identifier, "version-42")
        self.assertEqual(updated.whole_artifact_hash, "a" * 64)
        self.assertEqual(updated.backup_size_bytes, 4096)
