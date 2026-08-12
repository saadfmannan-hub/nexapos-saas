"""Focused production key-management and KEK rotation tests for Phase 3F."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TransactionTestCase, override_settings

from apps.backups.engine import availability
from apps.backups.engine.checks import (
    check_local_kek_configuration,
    check_production_key_provider_selection,
)
from apps.backups.engine.encrypted_artifact import (
    EncryptedArtifactProvider,
    RewrappedArtifactKeyResult,
)
from apps.backups.engine.encryption_exceptions import (
    EncryptedArtifactValidationError,
    KeyProviderConfigurationError,
    KeyProviderUnavailableError,
    KeyRewrapError,
    KeyWrapError,
)
from apps.backups.engine.key_management import (
    AWS_KMS_MAX_ATTEMPTS,
    AWS_KMS_PROVIDER_IDENTIFIER,
    AWS_KMS_WRAP_ALGORITHM,
    LOCAL_KEK_PROVIDER_IDENTIFIER,
    AwsKmsKeyEncryptionProvider,
    KeyEncryptionProvider,
    KeyEncryptionProviderRegistry,
    LocalConfiguredKekProvider,
    WrappedDek,
    build_key_provider_registry_from_settings,
    deserialize_wrapped_dek,
    key_metadata_identifier,
    serialize_wrapped_dek,
    validate_key_provider_settings,
    wrapped_dek_key_identifier,
)
from apps.backups.engine.key_rotation import publish_rewrapped_key_metadata
from apps.backups.models import BackupRecord

from . import test_backups_phase2f_encryption as phase2f_tests
from .test_backups_phase1 import BackupPhase1TestCase


class _KmsError(Exception):
    def __init__(self, code, message="raw-provider-secret"):
        self.response = {"Error": {"Code": code, "Message": message}}
        super().__init__(message)


class _FakeKmsClient:
    def __init__(self, *, failure=None):
        self.failure = failure
        self.calls = []
        self.key_id = "arn:aws:kms:us-east-1:111122223333:key/phase3f"
        self._plaintext = {}

    def encrypt(self, **kwargs):
        self.calls.append(("encrypt", kwargs))
        if self.failure is not None:
            raise self.failure
        ciphertext = b"kms-envelope:" + hashlib.sha256(kwargs["Plaintext"]).digest()
        self._plaintext[ciphertext] = kwargs["Plaintext"]
        return {"CiphertextBlob": ciphertext, "KeyId": self.key_id}

    def decrypt(self, **kwargs):
        self.calls.append(("decrypt", kwargs))
        if self.failure is not None:
            raise self.failure
        return {
            "Plaintext": self._plaintext[kwargs["CiphertextBlob"]],
            "KeyId": kwargs["KeyId"],
        }

    def describe_key(self, **kwargs):
        self.calls.append(("describe_key", kwargs))
        if self.failure is not None:
            raise self.failure
        return {
            "KeyMetadata": {
                "Enabled": True,
                "KeyState": "Enabled",
                "KeyUsage": "ENCRYPT_DECRYPT",
            }
        }


class _TrackingKeyProvider(KeyEncryptionProvider):
    development_only = True

    def __init__(self, delegate, events, *, corrupt_unwrap=False):
        self.delegate = delegate
        self.events = events
        self.corrupt_unwrap = corrupt_unwrap

    @property
    def provider_identifier(self):
        return self.delegate.provider_identifier

    @property
    def key_identifier(self):
        return self.delegate.key_identifier

    @property
    def key_version(self):
        return self.delegate.key_version

    def validate_configuration(self):
        return True

    def wrap_dek(self, dek, *, nonce):
        self.events.append("wrap")
        return self.delegate.wrap_dek(dek, nonce=nonce)

    def unwrap_dek(self, wrapped):
        self.events.append("unwrap")
        value = self.delegate.unwrap_dek(wrapped)
        return b"x" * 32 if self.corrupt_unwrap else value

    def health_check(self):
        return self.delegate.health_check()


class Phase3FKeyProviderTests(SimpleTestCase):
    @staticmethod
    def _local(fill=b"k", *, identifier="local-phase3f", version="v1"):
        return LocalConfiguredKekProvider(
            key_b64=base64.b64encode(fill * 32).decode("ascii"),
            key_identifier=identifier,
            key_version=version,
        )

    @staticmethod
    def _aws(client=None):
        return AwsKmsKeyEncryptionProvider(
            key_identifier="alias/nexa-backups",
            region="us-east-1",
            client=client or _FakeKmsClient(),
        )

    def test_provider_registry_resolves_local_provider(self):
        local = self._local()
        registry = KeyEncryptionProviderRegistry(active_provider=local)
        wrapped = local.wrap_dek(b"d" * 32, nonce=b"n" * 12)
        self.assertIs(registry.resolve(wrapped), local)

    def test_unknown_provider_fails_closed(self):
        registry = KeyEncryptionProviderRegistry(active_provider=self._local())
        unknown = WrappedDek("unknown-provider", "key", "v1", "unknown", b"", b"x", b"")
        with self.assertRaises((KeyProviderConfigurationError, KeyWrapError)):
            registry.resolve(unknown)

    def test_local_provider_remains_usable_in_tests(self):
        local = self._local()
        wrapped = local.wrap_dek(b"d" * 32, nonce=b"n" * 12)
        self.assertEqual(local.unwrap_dek(wrapped), b"d" * 32)

    def test_local_provider_is_explicitly_development_only(self):
        self.assertTrue(self._local().development_only)
        self.assertFalse(self._aws().development_only)

    @override_settings(
        BACKUP_KEY_PROVIDER="aws_kms",
        BACKUP_AWS_KMS_KEY_ID="alias/nexa-backups",
        BACKUP_AWS_REGION="us-east-1",
    )
    def test_aws_kms_configuration_parsing(self):
        self.assertEqual(validate_key_provider_settings(), "aws_kms")
        client = _FakeKmsClient()
        registry = build_key_provider_registry_from_settings(aws_client=client)
        self.assertIsInstance(registry.active_provider, AwsKmsKeyEncryptionProvider)

    def test_no_aws_credentials_are_hardcoded(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "apps/backups/engine/key_management.py").read_text(
            encoding="utf-8"
        ).lower()
        self.assertNotIn("aws_secret_access_key=", source)
        self.assertNotIn("aws_access_key_id=", source)
        self.assertNotIn("secret_access_key=", source)
        self.assertNotIn("backup_s3_access_key_id", source)
        self.assertNotIn("backup_s3_secret_access_key", source)

    def test_kms_wrap_uses_encrypt_api(self):
        client = _FakeKmsClient()
        provider = self._aws(client)
        wrapped = provider.wrap_dek(b"d" * 32, nonce=b"ignored-nonce")
        operation, kwargs = client.calls[0]
        self.assertEqual(operation, "encrypt")
        self.assertEqual(kwargs["KeyId"], "alias/nexa-backups")
        self.assertEqual(wrapped.key_version, client.key_id)
        self.assertEqual(wrapped.algorithm, AWS_KMS_WRAP_ALGORITHM)

    def test_kms_unwrap_uses_decrypt_api_and_stored_key_id(self):
        client = _FakeKmsClient()
        provider = self._aws(client)
        wrapped = provider.wrap_dek(b"d" * 32, nonce=b"")
        self.assertEqual(provider.unwrap_dek(wrapped), b"d" * 32)
        operation, kwargs = client.calls[-1]
        self.assertEqual(operation, "decrypt")
        self.assertEqual(kwargs["KeyId"], client.key_id)

    def test_raw_kek_never_enters_kms_provider_interface(self):
        provider = self._aws()
        self.assertFalse(hasattr(provider, "key"))
        self.assertFalse(hasattr(provider, "kek"))
        self.assertFalse(hasattr(provider, "raw_key"))

    def test_kms_access_denied_is_sanitized_and_nonretryable(self):
        provider = self._aws(_FakeKmsClient(failure=_KmsError("AccessDeniedException")))
        with self.assertRaises(KeyWrapError) as raised:
            provider.wrap_dek(b"d" * 32, nonce=b"")
        self.assertNotIsInstance(raised.exception, KeyProviderUnavailableError)
        self.assertNotIn("raw-provider-secret", str(raised.exception))

    def test_kms_throttling_is_sanitized_and_retryable(self):
        provider = self._aws(_FakeKmsClient(failure=_KmsError("ThrottlingException")))
        with self.assertRaises(KeyProviderUnavailableError) as raised:
            provider.wrap_dek(b"d" * 32, nonce=b"")
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("raw-provider-secret", str(raised.exception))

    def test_kms_unavailable_is_a_sanitized_retryable_failure(self):
        provider = self._aws(_FakeKmsClient(failure=OSError("private endpoint")))
        with self.assertRaises(KeyProviderUnavailableError) as raised:
            provider.wrap_dek(b"d" * 32, nonce=b"")
        self.assertNotIn("private endpoint", str(raised.exception))

    def test_kms_retry_policy_is_bounded(self):
        self.assertEqual(AWS_KMS_MAX_ATTEMPTS, 3)
        self.assertEqual(self._aws().sdk_max_attempts, 3)

    def test_kms_health_check_is_describe_only_and_non_destructive(self):
        client = _FakeKmsClient()
        health = self._aws(client).health_check()
        self.assertTrue(health.reachable)
        self.assertTrue(health.enabled)
        self.assertEqual([operation for operation, _kwargs in client.calls], ["describe_key"])

    @override_settings(
        BACKUP_EXECUTION_ENGINE_ENABLED=True,
        BACKUP_ENGINE_ENABLED=False,
        BACKUP_RESTORE_MUTATION_ENABLED=False,
        BACKUP_KEY_PROVIDER="aws_kms",
        BACKUP_AWS_KMS_KEY_ID="",
        BACKUP_AWS_REGION="us-east-1",
    )
    def test_aws_kms_missing_key_id_fails_readiness(self):
        self.assertEqual(
            [error.id for error in check_local_kek_configuration(None)],
            ["backups.E028"],
        )

    @override_settings(
        BACKUP_EXECUTION_ENGINE_ENABLED=True,
        BACKUP_ENGINE_ENABLED=False,
        BACKUP_RESTORE_MUTATION_ENABLED=False,
        BACKUP_KEY_PROVIDER="aws_kms",
        BACKUP_AWS_KMS_KEY_ID="alias/nexa-backups",
        BACKUP_AWS_REGION="",
    )
    def test_aws_kms_missing_region_fails_readiness(self):
        self.assertEqual(
            [error.id for error in check_local_kek_configuration(None)],
            ["backups.E028"],
        )

    @override_settings(
        BACKUP_EXECUTION_ENGINE_ENABLED=True,
        BACKUP_ENGINE_ENABLED=False,
        BACKUP_RESTORE_MUTATION_ENABLED=False,
        BACKUP_KEY_PROVIDER="local",
        BACKUP_LOCAL_KEK_B64=base64.b64encode(b"k" * 32).decode("ascii"),
        BACKUP_LOCAL_KEK_ID="local-phase3f",
        BACKUP_LOCAL_KEK_VERSION="v1",
    )
    def test_production_activation_with_local_provider_fails_checks(self):
        self.assertEqual(
            [error.id for error in check_production_key_provider_selection(None)],
            ["backups.E047"],
        )

    @override_settings(
        BACKUP_EXECUTION_ENGINE_ENABLED=False,
        BACKUP_ENGINE_ENABLED=False,
        BACKUP_RESTORE_MUTATION_ENABLED=False,
        BACKUP_KEY_PROVIDER="local",
    )
    def test_disabled_engine_with_local_provider_passes_activation_check(self):
        self.assertEqual(check_production_key_provider_selection(None), [])
        self.assertFalse(availability.real_execution_available())

    @override_settings(
        BACKUP_EXECUTION_ENGINE_ENABLED=False,
        BACKUP_ENGINE_ENABLED=False,
        BACKUP_RESTORE_MUTATION_ENABLED=True,
        BACKUP_KEY_PROVIDER="local",
        BACKUP_LOCAL_KEK_B64=base64.b64encode(b"k" * 32).decode("ascii"),
        BACKUP_LOCAL_KEK_ID="local-phase3f",
        BACKUP_LOCAL_KEK_VERSION="v1",
    )
    def test_restore_capability_rejects_development_only_local_provider(self):
        with mock.patch.object(
            availability,
            "restore_runtime_configuration_ready",
            return_value=True,
        ):
            self.assertFalse(availability.restore_execution_available())


class Phase3FEnvelopeRotationTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = phase2f_tests.EncryptedArtifactProviderTests(methodName="runTest")
        self.fixture.setUp()
        self.old = self.fixture._kek(
            b"o", key_identifier="historical-kek", key_version="v1"
        )
        self.new = self.fixture._kek(
            b"n", key_identifier="active-kek", key_version="v2"
        )

    def tearDown(self):
        try:
            self.fixture.tearDown()
        finally:
            super().tearDown()

    def _state(self, active, *, historical=()):
        registry = KeyEncryptionProviderRegistry(
            active_provider=active,
            historical_providers=historical,
        )
        return self.fixture._encryption_fixture(
            kek_provider=active,
            key_provider_registry=registry,
        )

    def _encrypted(self, active=None, *, historical=()):
        state = self._state(active or self.old, historical=historical)
        plaintext = self.fixture._read_package(state)
        result = self.fixture._encrypt(state)
        raw = self.fixture._read_artifact(state, result)
        return state, plaintext, result, raw

    def _restore_provider(self, state, active, *, historical=()):
        registry = KeyEncryptionProviderRegistry(
            active_provider=active,
            historical_providers=historical,
        )
        return EncryptedArtifactProvider(
            package_provider=state["package_provider"],
            verification_provider=state["verifier"],
            kek_provider=active,
            key_provider_registry=registry,
            workspace_manager=self.fixture.manager,
            policy=self.fixture._policy(),
        )

    @staticmethod
    def _key_identifier(result):
        return key_metadata_identifier(
            result.kek_provider_identifier,
            result.kek_key_identifier,
            result.kek_version,
        )

    @staticmethod
    def _read_restored(provider, state, result, raw, *, key_identifier, envelope=""):
        with provider.open_restored_plaintext(
            context=state["fixture"]["context"],
            reader=io.BytesIO(raw),
            encrypted_byte_count=len(raw),
            ciphertext_sha256=hashlib.sha256(raw).hexdigest(),
            encryption_key_identifier=key_identifier,
            encrypted_data_key_envelope=envelope,
        ) as (reader, evidence):
            return phase2f_tests.EncryptedArtifactProviderTests._read_stream(reader), evidence

    def _rewrap(self, state, result, raw, target=None, *, publisher=None):
        target = target or self.new
        provider = self._restore_provider(state, target, historical=(self.old,))
        published = []
        result_value = provider.rewrap_encrypted_artifact_key(
            context=state["fixture"]["context"],
            reader=io.BytesIO(raw),
            encrypted_byte_count=len(raw),
            ciphertext_sha256=hashlib.sha256(raw).hexdigest(),
            encryption_key_identifier=self._key_identifier(result),
            encrypted_data_key_envelope="",
            target_provider=target,
            publish_metadata=publisher or published.append,
        )
        return result_value, published

    def test_new_backup_records_provider_and_key_metadata(self):
        client = _FakeKmsClient()
        aws = AwsKmsKeyEncryptionProvider(
            key_identifier="alias/nexa-backups",
            region="us-east-1",
            client=client,
        )
        _state, _plaintext, result, _raw = self._encrypted(aws)
        self.assertEqual(result.kek_provider_identifier, AWS_KMS_PROVIDER_IDENTIFIER)
        self.assertEqual(result.kek_key_identifier, "alias/nexa-backups")
        self.assertEqual(result.kek_version, client.key_id)

    def test_historical_backup_decrypt_uses_stored_key_reference(self):
        state, plaintext, result, raw = self._encrypted(self.old)
        provider = self._restore_provider(state, self.new, historical=(self.old,))
        restored, evidence = self._read_restored(
            provider,
            state,
            result,
            raw,
            key_identifier=self._key_identifier(result),
        )
        self.assertEqual(restored, plaintext)
        self.assertEqual(evidence.kek_key_identifier, self.old.key_identifier)

    def test_active_key_change_does_not_break_old_backup(self):
        state, plaintext, result, raw = self._encrypted(self.old)
        provider = self._restore_provider(state, self.new, historical=(self.old,))
        restored, _evidence = self._read_restored(
            provider,
            state,
            result,
            raw,
            key_identifier=self._key_identifier(result),
        )
        self.assertEqual(restored, plaintext)

    def test_new_backup_after_rotation_uses_new_key(self):
        _state, _plaintext, result, _raw = self._encrypted(
            self.new,
            historical=(self.old,),
        )
        self.assertEqual(result.kek_key_identifier, self.new.key_identifier)
        self.assertEqual(result.kek_version, self.new.key_version)

    def test_rewrap_preserves_encrypted_payload_and_artifact_hash(self):
        state, _plaintext, result, raw = self._encrypted(self.old)
        rewrapped, _published = self._rewrap(state, result, raw)
        self.assertEqual(rewrapped.artifact_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(rewrapped.encrypted_byte_count, len(raw))
        self.assertEqual(self.fixture._read_artifact(state, result), raw)

    def test_rewrap_changes_only_wrapped_dek_metadata(self):
        state, _plaintext, result, raw = self._encrypted(self.old)
        rewrapped, published = self._rewrap(state, result, raw)
        self.assertEqual(published, [rewrapped])
        self.assertNotEqual(rewrapped.new_key_identifier, rewrapped.previous_key_identifier)
        self.assertTrue(rewrapped.new_envelope)

    def test_rewrap_validates_new_wrapper_before_publish(self):
        state, _plaintext, result, raw = self._encrypted(self.old)
        events = []
        tracking = _TrackingKeyProvider(self.new, events)
        rewrapped, _published = self._rewrap(
            state,
            result,
            raw,
            target=tracking,
            publisher=lambda _result: events.append("publish"),
        )
        self.assertIsInstance(rewrapped, RewrappedArtifactKeyResult)
        self.assertEqual(events, ["wrap", "unwrap", "publish"])

    def test_failed_rewrap_preserves_old_metadata(self):
        state, _plaintext, result, raw = self._encrypted(self.old)
        events = []
        corrupt = _TrackingKeyProvider(self.new, events, corrupt_unwrap=True)
        with self.assertRaises(KeyRewrapError):
            self._rewrap(
                state,
                result,
                raw,
                target=corrupt,
                publisher=lambda _result: events.append("publish"),
            )
        self.assertNotIn("publish", events)

    def test_corrupted_wrapped_dek_sidecar_fails_closed(self):
        state, _plaintext, result, raw = self._encrypted(self.old)
        rewrapped, _published = self._rewrap(state, result, raw)
        corrupted = rewrapped.new_envelope[:-2] + "xx"
        provider = self._restore_provider(state, self.new)
        with self.assertRaises(EncryptedArtifactValidationError):
            self._read_restored(
                provider,
                state,
                result,
                raw,
                key_identifier=rewrapped.new_key_identifier,
                envelope=corrupted,
            )

    def test_unknown_historical_provider_sidecar_fails_closed(self):
        state, _plaintext, result, raw = self._encrypted(self.old)
        rewrapped, _published = self._rewrap(state, result, raw)
        document = json.loads(rewrapped.new_envelope)
        document["wrapped_dek"]["kek_provider_identifier"] = "unknown-provider"
        unknown = json.dumps(document, sort_keys=True, separators=(",", ":"))
        provider = self._restore_provider(state, self.new)
        with self.assertRaises(EncryptedArtifactValidationError):
            self._read_restored(
                provider,
                state,
                result,
                raw,
                key_identifier=rewrapped.new_key_identifier,
                envelope=unknown,
            )

    def test_restore_resolves_rewrapped_historical_provider(self):
        state, plaintext, result, raw = self._encrypted(self.old)
        rewrapped, _published = self._rewrap(state, result, raw)
        provider = self._restore_provider(state, self.new)
        restored, evidence = self._read_restored(
            provider,
            state,
            result,
            raw,
            key_identifier=rewrapped.new_key_identifier,
            envelope=rewrapped.new_envelope,
        )
        self.assertEqual(restored, plaintext)
        self.assertEqual(evidence.kek_key_identifier, self.new.key_identifier)

    def test_second_rewrap_uses_verified_sidecar_as_current_wrapper(self):
        state, _plaintext, result, raw = self._encrypted(self.old)
        first, _published = self._rewrap(state, result, raw)
        third = self.fixture._kek(b"t", key_identifier="third-kek", key_version="v3")
        registry = KeyEncryptionProviderRegistry(
            active_provider=third,
            historical_providers=(self.new,),
        )
        provider = EncryptedArtifactProvider(
            package_provider=state["package_provider"],
            verification_provider=state["verifier"],
            kek_provider=third,
            key_provider_registry=registry,
            workspace_manager=self.fixture.manager,
            policy=self.fixture._policy(),
        )
        published = []
        second = provider.rewrap_encrypted_artifact_key(
            context=state["fixture"]["context"],
            reader=io.BytesIO(raw),
            encrypted_byte_count=len(raw),
            ciphertext_sha256=hashlib.sha256(raw).hexdigest(),
            encryption_key_identifier=first.new_key_identifier,
            encrypted_data_key_envelope=first.new_envelope,
            target_provider=third,
            publish_metadata=published.append,
        )
        self.assertEqual(published, [second])
        self.assertIn("third-kek", second.new_key_identifier)


class Phase3FMetadataPersistenceTests(BackupPhase1TestCase):
    def _backup(self):
        return BackupRecord.objects.create(
            **self.backup_model_kwargs(
                encryption_key_identifier=(
                    f"{LOCAL_KEK_PROVIDER_IDENTIFIER}:historical-kek:v1"
                ),
                encrypted_data_key_envelope="",
                whole_artifact_hash="a" * 64,
                backup_size_bytes=4096,
            )
        )

    def _result(self, backup, **changes):
        values = {
            "previous_key_identifier": backup.encryption_key_identifier,
            "new_key_identifier": f"{LOCAL_KEK_PROVIDER_IDENTIFIER}:active-kek:v2",
            "previous_envelope": backup.encrypted_data_key_envelope,
            "new_envelope": "verified-wrapped-ciphertext",
            "encrypted_byte_count": backup.backup_size_bytes,
            "artifact_sha256": backup.whole_artifact_hash,
        }
        values.update(changes)
        return RewrappedArtifactKeyResult(**values)

    def test_verified_rewrap_metadata_is_published_atomically(self):
        backup = self._backup()
        result = self._result(backup)
        publish_rewrapped_key_metadata(backup=backup, result=result)
        self.assertEqual(backup.encryption_key_identifier, result.new_key_identifier)
        self.assertEqual(backup.encrypted_data_key_envelope, result.new_envelope)
        self.assertEqual(backup.whole_artifact_hash, result.artifact_sha256)

    def test_failed_atomic_compare_and_set_preserves_old_metadata(self):
        backup = self._backup()
        old_identifier = backup.encryption_key_identifier
        old_envelope = backup.encrypted_data_key_envelope
        with self.assertRaises(KeyRewrapError):
            publish_rewrapped_key_metadata(
                backup=backup,
                result=self._result(backup, artifact_sha256="b" * 64),
            )
        backup.refresh_from_db()
        self.assertEqual(backup.encryption_key_identifier, old_identifier)
        self.assertEqual(backup.encrypted_data_key_envelope, old_envelope)


class Phase3FSecurityContractTests(SimpleTestCase):
    def test_no_raw_dek_model_field_exists(self):
        field_names = {field.name for field in BackupRecord._meta.fields}
        self.assertTrue(
            {"raw_dek", "dek", "data_encryption_key", "plaintext_dek"}.isdisjoint(
                field_names
            )
        )

    def test_provider_failures_do_not_log_raw_dek_or_provider_errors(self):
        provider = AwsKmsKeyEncryptionProvider(
            key_identifier="alias/nexa-backups",
            region="us-east-1",
            client=_FakeKmsClient(failure=_KmsError("AccessDeniedException")),
        )
        with mock.patch("logging.Logger._log") as logger:
            with self.assertRaises(KeyWrapError):
                provider.wrap_dek(b"raw-dek-value".ljust(32, b"x"), nonce=b"")
        logger.assert_not_called()

    def test_wrapped_dek_is_not_exposed_in_owner_templates(self):
        root = Path(__file__).resolve().parents[1] / "templates/backups"
        source = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.html"))
        self.assertNotIn("encrypted_data_key_envelope", source)
        self.assertNotIn("wrapped_dek", source)

    def test_wrapped_dek_is_not_exposed_in_platform_templates(self):
        root = Path(__file__).resolve().parents[1] / "templates/platformadmin/backups"
        source = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.html"))
        self.assertNotIn("encrypted_data_key_envelope", source)
        self.assertNotIn("wrapped_dek", source)

    def test_safety_backup_reuses_the_active_runtime_provider_stack(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "apps/backups/engine/restore_mutation.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("backup_stack = build_runtime_provider_stack()", source)
        self.assertIn(
            "backup_coordinator=BackupExecutionCoordinator(provider_stack=backup_stack)",
            source,
        )

    def test_envelope_serialization_round_trips_without_raw_key_fields(self):
        local = LocalConfiguredKekProvider(
            key_b64=base64.b64encode(b"k" * 32).decode("ascii"),
            key_identifier="local-phase3f",
            key_version="v1",
        )
        wrapped = local.wrap_dek(b"d" * 32, nonce=b"n" * 12)
        serialized = serialize_wrapped_dek(wrapped)
        self.assertEqual(deserialize_wrapped_dek(serialized), wrapped)
        self.assertNotIn("raw_dek", serialized)
        self.assertEqual(wrapped_dek_key_identifier(wrapped), key_metadata_identifier(
            LOCAL_KEK_PROVIDER_IDENTIFIER,
            "local-phase3f",
            "v1",
        ))
