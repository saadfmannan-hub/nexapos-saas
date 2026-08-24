"""Focused no-500 coverage for backup restore and lock write boundaries."""

import uuid
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.backups import owner_services, platform_services, services
from apps.backups.enums import (
    BackupScope,
    BackupStatus,
    BackupTrigger,
    CompatibilityStatus,
    IntegrityStatus,
    OperationKind,
    ProductOwner,
)
from apps.backups.models import BackupActivity, BackupRecord, RestoreOperation

from .base import TenantTestCase


class BackupInputIntegrityTests(TenantTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.platform_admin = User.objects.create_superuser(
            email="backup-integrity-platform@example.com",
            password="StrongPass123!",
            full_name="Backup Integrity Platform Admin",
        )

    def _eligible_backup(self):
        return BackupRecord.objects.create(
            business=self.business_a,
            tenant_public_id_snapshot=self.business_a.public_id,
            scope=BackupScope.POS,
            included_products=[ProductOwner.POS],
            trigger=BackupTrigger.MANUAL,
            status=BackupStatus.SUCCEEDED,
            integrity_status=IntegrityStatus.VERIFIED,
            format_version="1.0",
            application_version="1.0.0",
            schema_fingerprint="a" * 64,
            minimum_restore_version="1.0.0",
            compatibility_status=CompatibilityStatus.COMPATIBLE,
            storage_backend_identifier="private-store",
            opaque_object_key=f"integrity/{uuid.uuid4()}",
            whole_artifact_hash="b" * 64,
            completed_at=timezone.now(),
            verified_at=timezone.now(),
            idempotency_key=f"integrity:{uuid.uuid4()}",
        )

    @staticmethod
    def _persist_restore_before_validation_failure(**kwargs):
        return RestoreOperation.objects.create(
            business=kwargs["business"],
            source_backup=kwargs["source_backup"],
            requested_scope=kwargs["requested_scope"],
            requested_by=kwargs["actor"],
            reason=kwargs["reason"],
            compatibility_status=CompatibilityStatus.COMPATIBLE,
            idempotency_key=f"partial:{uuid.uuid4()}",
        )

    def test_owner_stale_restore_validation_is_409_and_rolls_back_metadata(self):
        self.client.force_login(self.owner_a)
        backup = self._eligible_backup()
        restore_count = RestoreOperation.objects.count()
        activity_count = BackupActivity.objects.count()

        with (
            mock.patch.object(
                owner_services,
                "restore_preflight_configuration_ready",
                return_value=True,
            ),
            mock.patch.object(
                owner_services.services,
                "create_restore_request",
                side_effect=self._persist_restore_before_validation_failure,
            ),
            mock.patch.object(
                owner_services.services,
                "create_backup_activity",
                side_effect=ValidationError("stale internal restore state"),
            ),
        ):
            response = self.client.post(
                reverse(
                    "backups:restore_preflight",
                    kwargs={"public_id": backup.public_id},
                ),
                {"reason": "Validate recovery readiness"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertContains(
            response,
            "Restore readiness changed. Refresh the page and try again.",
            status_code=409,
        )
        self.assertNotContains(
            response,
            "stale internal restore state",
            status_code=409,
        )
        self.assertEqual(RestoreOperation.objects.count(), restore_count)
        self.assertEqual(BackupActivity.objects.count(), activity_count)
        self.assertNotIn("backups_owner_preflight", self.client.session)

    def test_platform_stale_restore_validation_is_409_and_rolls_back_metadata(self):
        self.client.force_login(self.platform_admin)
        backup = self._eligible_backup()
        restore_count = RestoreOperation.objects.count()
        activity_count = BackupActivity.objects.count()

        with (
            mock.patch.object(
                platform_services,
                "restore_preflight_configuration_ready",
                return_value=True,
            ),
            mock.patch.object(
                platform_services.services,
                "create_restore_request",
                side_effect=self._persist_restore_before_validation_failure,
            ),
            mock.patch.object(
                platform_services.services,
                "create_backup_activity",
                side_effect=ValidationError("stale internal restore state"),
            ),
        ):
            response = self.client.post(
                reverse(
                    "platformadmin:backup_preflight",
                    args=[self.business_a.public_id, backup.public_id],
                ),
                {"reason": "Validate platform recovery readiness"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertContains(
            response,
            "Restore readiness changed. Refresh the page and try again.",
            status_code=409,
        )
        self.assertNotContains(
            response,
            "stale internal restore state",
            status_code=409,
        )
        self.assertEqual(RestoreOperation.objects.count(), restore_count)
        self.assertEqual(BackupActivity.objects.count(), activity_count)
        self.assertNotIn("backups_platform_preflight", self.client.session)

    def test_active_same_business_lock_is_a_controlled_conflict(self):
        services.acquire_tenant_operation_lock(
            business=self.business_a,
            operation_kind=OperationKind.BACKUP,
            operation_public_id=uuid.uuid4(),
        )

        with self.assertRaises(services.TenantOperationLocked):
            services.acquire_tenant_operation_lock(
                business=self.business_a,
                operation_kind=OperationKind.RESTORE,
                operation_public_id=uuid.uuid4(),
            )

    def test_lock_integrity_without_same_business_conflict_is_reraised(self):
        other_lock = services.acquire_tenant_operation_lock(
            business=self.business_b,
            operation_kind=OperationKind.BACKUP,
            operation_public_id=uuid.uuid4(),
        )

        with (
            mock.patch.object(
                services.TenantOperationLock.objects,
                "create",
                side_effect=IntegrityError("unrelated lock constraint"),
            ),
            self.assertRaisesMessage(IntegrityError, "unrelated lock constraint"),
        ):
            services.acquire_tenant_operation_lock(
                business=self.business_a,
                operation_kind=OperationKind.RESTORE,
                operation_public_id=uuid.uuid4(),
            )

        other_lock.refresh_from_db()
        self.assertTrue(other_lock.active)
