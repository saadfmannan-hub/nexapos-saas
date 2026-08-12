"""Credential-domain isolation regressions for AWS KMS and DigitalOcean Spaces."""

from __future__ import annotations

import os
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.backups.engine.durable_storage_exceptions import DurableStoragePolicyError
from apps.backups.engine.encrypted_artifact import EncryptedArtifactProvider
from apps.backups.engine.key_management import AwsKmsKeyEncryptionProvider
from apps.backups.engine.s3_storage import (
    S3CompatibleDurableStorageProvider,
    S3StorageConfiguration,
)
from apps.backups.engine.storage_registry import validate_storage_provider_settings
from apps.backups.models import BackupActivity, BackupRecord, RestoreOperation
from apps.backups.operational_readiness import (
    ReadinessCategory,
    ReadinessState,
    assess_operational_readiness,
)

_SPACES_SETTINGS = {
    "BACKUP_STORAGE_PROVIDER": "s3",
    "BACKUP_S3_BUCKET": "nexa-hotfix-test",
    "BACKUP_S3_REGION": "fra1",
    "BACKUP_S3_ENDPOINT_URL": "https://fra1.digitaloceanspaces.com",
    "BACKUP_S3_ACCESS_KEY_ID": "spaces-test-access-id",
    "BACKUP_S3_SECRET_ACCESS_KEY": "spaces-test-secret-key",
    "BACKUP_S3_PREFIX": "nexa/backups",
    "BACKUP_S3_ADDRESSING_STYLE": "virtual",
}


class BackupCredentialIsolationHotfixTests(SimpleTestCase):
    @staticmethod
    def _artifact_provider():
        return object.__new__(EncryptedArtifactProvider)

    @override_settings(**_SPACES_SETTINGS)
    def test_s3_client_receives_only_dedicated_spaces_credentials(self):
        aws_environment = {
            "AWS_ACCESS_KEY_ID": "aws-kms-test-id",
            "AWS_SECRET_ACCESS_KEY": "aws-kms-test-secret",
        }
        with (
            mock.patch.dict(os.environ, aws_environment, clear=False),
            mock.patch("apps.backups.engine.s3_storage.boto3.client") as client_factory,
        ):
            provider = S3CompatibleDurableStorageProvider(
                encrypted_artifact_provider=self._artifact_provider()
            )
            self.assertFalse(provider.client_created)
            self.assertIs(provider.client, client_factory.return_value)

        args, kwargs = client_factory.call_args
        self.assertEqual(args, ("s3",))
        self.assertEqual(kwargs["aws_access_key_id"], "spaces-test-access-id")
        self.assertEqual(kwargs["aws_secret_access_key"], "spaces-test-secret-key")
        self.assertNotEqual(kwargs["aws_access_key_id"], aws_environment["AWS_ACCESS_KEY_ID"])
        self.assertNotEqual(
            kwargs["aws_secret_access_key"],
            aws_environment["AWS_SECRET_ACCESS_KEY"],
        )

    @override_settings(
        BACKUP_S3_ACCESS_KEY_ID="spaces-test-access-id",
        BACKUP_S3_SECRET_ACCESS_KEY="spaces-test-secret-key",
    )
    def test_kms_client_keeps_standard_aws_credential_chain(self):
        kms_client = mock.Mock()
        kms_client.describe_key.return_value = {
            "KeyMetadata": {
                "Enabled": True,
                "KeyState": "Enabled",
                "KeyUsage": "ENCRYPT_DECRYPT",
            }
        }
        with mock.patch("boto3.client", return_value=kms_client) as client_factory:
            provider = AwsKmsKeyEncryptionProvider(
                key_identifier="alias/nexa-backups",
                region="us-east-1",
            )
            self.assertTrue(provider.health_check().enabled)

        args, kwargs = client_factory.call_args
        self.assertEqual(args, ("kms",))
        self.assertNotIn("aws_access_key_id", kwargs)
        self.assertNotIn("aws_secret_access_key", kwargs)
        self.assertNotIn("spaces-test-access-id", repr(client_factory.call_args))
        self.assertNotIn("spaces-test-secret-key", repr(client_factory.call_args))

    def test_missing_or_unsafe_spaces_credentials_fail_closed(self):
        invalid_pairs = (
            ("", ""),
            ("spaces-test-access-id", ""),
            ("", "spaces-test-secret-key"),
            ("   ", "spaces-test-secret-key"),
            ("spaces-test-access-id", " \t"),
            ("spaces-test-access-id\nunsafe-marker", "spaces-test-secret-key"),
            ("spaces-test-access-id", "spaces-test-secret-key\x00unsafe-marker"),
        )
        for access_key_id, secret_access_key in invalid_pairs:
            with (
                self.subTest(access_key_id=bool(access_key_id), secret=bool(secret_access_key)),
                override_settings(
                    **{
                        **_SPACES_SETTINGS,
                        "BACKUP_S3_ACCESS_KEY_ID": access_key_id,
                        "BACKUP_S3_SECRET_ACCESS_KEY": secret_access_key,
                    }
                ),
                self.assertRaises(DurableStoragePolicyError) as raised,
            ):
                validate_storage_provider_settings()
            rendered = str(raised.exception)
            if access_key_id:
                self.assertNotIn(access_key_id, rendered)
            if secret_access_key:
                self.assertNotIn(secret_access_key, rendered)
            self.assertNotIn("unsafe-marker", rendered)

    @override_settings(
        **{
            **_SPACES_SETTINGS,
            "BACKUP_S3_ACCESS_KEY_ID": "",
            "BACKUP_S3_SECRET_ACCESS_KEY": "",
        }
    )
    def test_missing_spaces_credentials_report_not_ready_without_values(self):
        result = assess_operational_readiness(attest_providers=False)
        storage = next(
            check
            for check in result.checks
            if check.category == ReadinessCategory.DURABLE_STORAGE
        )
        self.assertEqual(storage.state, ReadinessState.NOT_READY)
        rendered = repr(result.as_dict())
        self.assertNotIn("aws_access_key_id", rendered.lower())
        self.assertNotIn("secret_access_key", rendered.lower())

    @override_settings(
        BACKUP_STORAGE_PROVIDER="local",
        BACKUP_S3_ACCESS_KEY_ID="",
        BACKUP_S3_SECRET_ACCESS_KEY="",
    )
    def test_local_provider_does_not_require_spaces_credentials(self):
        self.assertEqual(validate_storage_provider_settings(), "local")

    @override_settings(**_SPACES_SETTINGS)
    def test_spaces_secrets_are_absent_from_reprs_and_readiness(self):
        configuration = S3StorageConfiguration.from_settings()
        provider = S3CompatibleDurableStorageProvider(
            encrypted_artifact_provider=self._artifact_provider(),
            configuration=configuration,
            client_factory=mock.Mock(),
        )
        readiness = assess_operational_readiness(attest_providers=False)
        rendered = " ".join((repr(configuration), repr(provider), repr(readiness.as_dict())))
        self.assertNotIn("spaces-test-access-id", rendered)
        self.assertNotIn("spaces-test-secret-key", rendered)

    def test_spaces_credentials_have_no_database_field(self):
        field_names = {
            field.name
            for model in (BackupRecord, RestoreOperation, BackupActivity)
            for field in model._meta.get_fields()
        }
        self.assertNotIn("backup_s3_access_key_id", field_names)
        self.assertNotIn("backup_s3_secret_access_key", field_names)
        self.assertNotIn("access_key_id", field_names)
        self.assertNotIn("secret_access_key", field_names)
