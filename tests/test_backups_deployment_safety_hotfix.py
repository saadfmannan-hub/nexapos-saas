"""Deployment-safety regressions for intentionally inactive backup providers."""

import tempfile
from pathlib import Path
from unittest import mock

from django.core.checks import run_checks
from django.test import override_settings
from django.utils import timezone

from apps.backups import owner_services, platform_services
from apps.backups.engine import availability
from apps.backups.engine.checks import (
    check_durable_storage_root,
    check_local_kek_configuration,
    check_media_storage_configuration,
    check_restore_preflight_configuration,
)
from apps.backups.enums import (
    BackupStatus,
    CompatibilityStatus,
    IntegrityStatus,
)
from apps.backups.models import BackupActivity, BackupRecord, RestoreOperation

from .test_backups_phase1 import BackupPhase1TestCase


class BackupDeploymentSafetyHotfixTests(BackupPhase1TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.set_entitlements(cls, cls.business_a, pos=True, wms=False)

    def _eligible_backup(self):
        return BackupRecord.objects.create(
            **self.backup_model_kwargs(
                status=BackupStatus.SUCCEEDED,
                integrity_status=IntegrityStatus.VERIFIED,
                compatibility_status=CompatibilityStatus.COMPATIBLE,
                storage_backend_identifier="private-store",
                opaque_object_key="deployment-safety-object",
                whole_artifact_hash="a" * 64,
                backup_size_bytes=4096,
                completed_at=timezone.now(),
                verified_at=timezone.now(),
            )
        )

    @override_settings(
        BACKUP_EXECUTION_ENGINE_ENABLED=False,
        BACKUP_ENGINE_ENABLED=False,
        BACKUP_RESTORE_MUTATION_ENABLED=False,
        BACKUP_LOCAL_KEK_B64="",
        BACKUP_LOCAL_KEK_ID="",
        BACKUP_LOCAL_KEK_VERSION="",
    )
    def test_disabled_provider_infrastructure_does_not_block_django_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            missing_media = root / "missing-media"
            missing_durable = root / "missing-durable"
            with self.settings(
                MEDIA_ROOT=missing_media,
                STORAGES={
                    "default": {
                        "BACKEND": "django.core.files.storage.FileSystemStorage",
                        "OPTIONS": {"location": str(missing_media)},
                    }
                },
                BACKUP_DURABLE_STORAGE_ROOT=missing_durable,
            ):
                error_ids = {error.id for error in run_checks()}

        self.assertTrue(
            {"backups.E025", "backups.E030", "backups.E034"}.isdisjoint(error_ids)
        )
        self.assertFalse(availability.provider_environment_checks_required())
        self.assertFalse(availability.restore_provider_checks_required())

    @override_settings(
        BACKUP_EXECUTION_ENGINE_ENABLED=False,
        BACKUP_ENGINE_ENABLED=False,
        BACKUP_RESTORE_MUTATION_ENABLED=False,
    )
    def test_disabled_capabilities_remain_fail_closed(self):
        self.assertFalse(availability.OPERATIONAL_PROVIDER_STACK_READY)
        self.assertFalse(availability.real_execution_available())
        self.assertFalse(availability.restore_execution_available())
        self.assertFalse(owner_services.manual_backup_capability().enabled)
        self.assertFalse(platform_services.manual_backup_capability().enabled)
        self.assertFalse(owner_services.restore_mutation_capability().enabled)
        self.assertFalse(platform_services.restore_mutation_capability().enabled)

    @override_settings(
        BACKUP_EXECUTION_ENGINE_ENABLED=False,
        BACKUP_ENGINE_ENABLED=False,
        BACKUP_RESTORE_MUTATION_ENABLED=False,
    )
    @mock.patch(
        "apps.backups.owner_services.restore_preflight_configuration_ready",
        return_value=False,
    )
    def test_preflight_without_providers_refuses_before_persisting_evidence(
        self,
        readiness,
    ):
        backup = self._eligible_backup()
        restore_count = RestoreOperation.objects.count()
        activity_count = BackupActivity.objects.count()

        with self.assertRaises(owner_services.OwnerBackupActionUnavailable) as raised:
            owner_services.run_restore_preflight(
                business=self.business_a,
                backup=backup,
                actor=self.owner_a,
                reason="Deployment safety verification",
            )

        self.assertIn("unavailable", str(raised.exception).lower())
        self.assertEqual(RestoreOperation.objects.count(), restore_count)
        self.assertEqual(BackupActivity.objects.count(), activity_count)
        backup.refresh_from_db()
        self.assertEqual(backup.status, BackupStatus.SUCCEEDED)
        readiness.assert_called_once_with()

    @override_settings(
        BACKUP_EXECUTION_ENGINE_ENABLED=False,
        BACKUP_ENGINE_ENABLED=False,
        BACKUP_RESTORE_MUTATION_ENABLED=False,
    )
    @mock.patch(
        "apps.backups.platform_services.restore_preflight_configuration_ready",
        return_value=False,
    )
    @mock.patch(
        "apps.backups.platform_services.has_platform_backup_capability",
        return_value=True,
    )
    def test_platform_preflight_without_providers_also_refuses_before_persistence(
        self,
        capability,
        readiness,
    ):
        backup = self._eligible_backup()
        restore_count = RestoreOperation.objects.count()
        activity_count = BackupActivity.objects.count()

        with self.assertRaises(platform_services.PlatformBackupActionUnavailable):
            platform_services.platform_run_restore_preflight(
                business=self.business_a,
                backup=backup,
                actor=self.owner_a,
                reason="Platform deployment safety verification",
            )

        self.assertEqual(RestoreOperation.objects.count(), restore_count)
        self.assertEqual(BackupActivity.objects.count(), activity_count)
        capability.assert_called_once()
        readiness.assert_called_once_with()

    @override_settings(
        BACKUP_EXECUTION_ENGINE_ENABLED=True,
        BACKUP_ENGINE_ENABLED=False,
        BACKUP_RESTORE_MUTATION_ENABLED=False,
    )
    def test_backup_activation_retains_strict_media_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            missing_media = Path(directory).resolve() / "missing-media"
            with self.settings(
                MEDIA_ROOT=missing_media,
                STORAGES={
                    "default": {
                        "BACKEND": "django.core.files.storage.FileSystemStorage",
                    }
                },
            ):
                errors = check_media_storage_configuration(None)
        self.assertEqual([error.id for error in errors], ["backups.E025"])

    @override_settings(
        BACKUP_EXECUTION_ENGINE_ENABLED=True,
        BACKUP_ENGINE_ENABLED=False,
        BACKUP_RESTORE_MUTATION_ENABLED=False,
        BACKUP_DURABLE_STORAGE_REQUIRE_LOCAL=False,
    )
    def test_backup_activation_retains_strict_durable_root_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory).resolve()
            with self.settings(
                BACKUP_STAGING_ROOT=staging,
                BACKUP_DURABLE_STORAGE_ROOT=staging / "inside-staging",
            ):
                errors = check_durable_storage_root(None)
        self.assertEqual([error.id for error in errors], ["backups.E030"])

    @override_settings(
        BACKUP_EXECUTION_ENGINE_ENABLED=False,
        BACKUP_ENGINE_ENABLED=False,
        BACKUP_RESTORE_MUTATION_ENABLED=True,
        BACKUP_LOCAL_KEK_B64="",
        BACKUP_LOCAL_KEK_ID="",
        BACKUP_LOCAL_KEK_VERSION="",
    )
    def test_restore_activation_retains_strict_provider_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with self.settings(
                MEDIA_ROOT=root / "missing-media",
                BACKUP_DURABLE_STORAGE_ROOT=root / "missing-durable",
            ):
                restore_errors = check_restore_preflight_configuration(None)
                key_errors = check_local_kek_configuration(None)
        self.assertEqual([error.id for error in restore_errors], ["backups.E034"])
        self.assertEqual([error.id for error in key_errors], ["backups.E028"])
