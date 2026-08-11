"""Phase 3I production activation and disaster-recovery UAT readiness tests."""

from __future__ import annotations

import io
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.core.checks import run_checks
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.backups import owner_services, services
from apps.backups.activation_readiness import (
    ActivationMarker,
    assess_production_activation_readiness,
)
from apps.backups.engine import availability
from apps.backups.engine.retention_policy import DAILY_FULL_KEEP_COUNT
from apps.backups.enums import BackupScope, BackupStatus, BackupTrigger, IntegrityStatus
from apps.backups.operational_readiness import (
    ReadinessCategory,
    ReadinessState,
    assess_operational_readiness,
)
from apps.backups.tasks import (
    BACKUP_EXECUTION_TASK_NAME,
    BACKUP_QUEUE_NAME,
    BACKUP_SCHEDULER_QUEUE_NAME,
    RECONCILIATION_TASK_NAME,
    RESTORE_EXECUTION_TASK_NAME,
    RESTORE_QUEUE_NAME,
    SCHEDULE_DISPATCH_TASK_NAME,
)

from .test_backups_phase1 import BackupPhase1TestCase


def _production_settings(staging_root):
    return {
        "BACKUP_KEY_PROVIDER": "aws_kms",
        "BACKUP_AWS_KMS_KEY_ID": "alias/nexa-backups-uat",
        "BACKUP_AWS_REGION": "us-east-1",
        "BACKUP_STORAGE_PROVIDER": "s3",
        "BACKUP_S3_BUCKET": "nexa-backups-uat",
        "BACKUP_S3_REGION": "fra1",
        "BACKUP_S3_ENDPOINT_URL": "https://fra1.digitaloceanspaces.com",
        "BACKUP_S3_PREFIX": "nexa/backups",
        "CELERY_BROKER_URL": "rediss://broker.example/1",
        "CELERY_TASK_ALWAYS_EAGER": False,
        "BACKUP_STAGING_ROOT": Path(staging_root),
    }


def _provider_mocks():
    key = mock.Mock()
    key.health_check.return_value = SimpleNamespace(
        provider_identifier="aws-kms-v1",
        reachable=True,
        enabled=True,
    )
    storage = mock.Mock()
    storage.health_attestation.return_value = True
    return key, storage


class BackupPhase3IActivationReadinessTests(BackupPhase1TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.set_entitlements(cls, cls.business_a, pos=True, wms=False)
        cls.platform_admin = User.objects.create_superuser(
            email="phase3i-platform@example.com",
            password="StrongPass123!",
            full_name="Phase 3I Platform Admin",
        )

    def setUp(self):
        super().setUp()
        self.staging_root = Path(tempfile.gettempdir()) / (
            f"nexa-phase3i-staging-{uuid.uuid4().hex}"
        )

    def _ready_result(self, **extra_settings):
        configured = _production_settings(self.staging_root)
        configured.update(extra_settings)
        key, storage = _provider_mocks()
        with override_settings(**configured):
            return assess_production_activation_readiness(
                attest_providers=True,
                key_provider=key,
                storage_provider=storage,
            )

    def test_01_activation_flags_default_false(self):
        self.assertFalse(settings.BACKUP_EXECUTION_ENGINE_ENABLED)
        self.assertFalse(settings.BACKUP_RESTORE_MUTATION_ENABLED)
        self.assertFalse(availability.OPERATIONAL_PROVIDER_STACK_READY)

    def test_02_production_kms_code_capability_is_true(self):
        self.assertTrue(availability.PRODUCTION_KEY_PROVIDER_READY)

    def test_03_production_storage_code_capability_is_true(self):
        self.assertTrue(availability.PRODUCTION_DURABLE_STORAGE_PROVIDER_READY)

    def test_04_code_ready_is_distinct_from_infrastructure_ready(self):
        result = assess_production_activation_readiness()
        self.assertTrue(result.code_ready)
        self.assertFalse(result.infrastructure_ready)
        self.assertIn(ActivationMarker.CODE_READY, result.markers)
        self.assertIn(
            ActivationMarker.INFRASTRUCTURE_NOT_CONFIGURED,
            result.markers,
        )

    def test_05_missing_broker_is_not_infrastructure_ready(self):
        result = self._ready_result(CELERY_BROKER_URL="")
        self.assertFalse(result.infrastructure_ready)
        self.assertFalse(result.ready_for_backup_uat)

    def test_06_missing_kms_configuration_is_not_ready(self):
        configured = _production_settings(self.staging_root)
        configured["BACKUP_AWS_KMS_KEY_ID"] = ""
        with override_settings(**configured):
            result = assess_production_activation_readiness()
        provider = next(
            check for check in result.checks if check.identifier == "PROVIDER_CONFIGURATION"
        )
        self.assertFalse(provider.ready)

    def test_07_missing_s3_configuration_is_not_ready(self):
        configured = _production_settings(self.staging_root)
        configured["BACKUP_S3_BUCKET"] = ""
        with override_settings(**configured):
            result = assess_production_activation_readiness()
        self.assertFalse(result.infrastructure_configured)

    def test_08_local_kms_is_rejected_for_production(self):
        result = assess_operational_readiness()
        kms = next(
            check
            for check in result.checks
            if check.category == ReadinessCategory.KEY_MANAGEMENT
        )
        self.assertEqual(kms.state, ReadinessState.NOT_READY)

    def test_09_local_storage_is_rejected_for_production(self):
        result = assess_operational_readiness()
        storage = next(
            check
            for check in result.checks
            if check.category == ReadinessCategory.DURABLE_STORAGE
        )
        self.assertEqual(storage.state, ReadinessState.NOT_READY)

    def test_10_unsafe_staging_is_not_ready(self):
        result = self._ready_result(BACKUP_STAGING_ROOT=Path(settings.MEDIA_ROOT))
        staging = next(
            check for check in result.checks if check.identifier == "PRIVATE_STAGING"
        )
        self.assertFalse(staging.ready)
        self.assertFalse(result.ready_for_backup_uat)

    def test_11_mocked_production_providers_are_backup_uat_ready(self):
        result = self._ready_result()
        self.assertTrue(result.infrastructure_ready)
        self.assertTrue(result.ready_for_backup_uat)
        self.assertIn(ActivationMarker.READY_FOR_BACKUP_UAT, result.markers)

    def test_12_restore_remains_a_separate_gate(self):
        result = self._ready_result()
        self.assertTrue(result.ready_for_backup_uat)
        self.assertFalse(result.ready_for_restore_uat)
        self.assertNotIn(ActivationMarker.READY_FOR_RESTORE_UAT, result.markers)

    def test_13_restore_mutation_disabled_blocks_restore_uat(self):
        result = self._ready_result(BACKUP_EXECUTION_ENGINE_ENABLED=True)
        self.assertFalse(result.restore_mutation_enabled)
        self.assertFalse(result.ready_for_restore_uat)

    def test_14_restore_worker_route_is_required(self):
        routes = dict(settings.CELERY_TASK_ROUTES)
        routes.pop(RESTORE_EXECUTION_TASK_NAME)
        result = self._ready_result(CELERY_TASK_ROUTES=routes)
        restore_route = next(
            check
            for check in result.checks
            if check.identifier == "RESTORE_WORKER_ROUTE"
        )
        self.assertFalse(restore_route.ready)
        self.assertFalse(result.ready_for_restore_uat)

    def test_15_readiness_command_performs_no_backup(self):
        output = io.StringIO()
        with mock.patch(
            "apps.backups.engine.runtime.request_backup_execution"
        ) as execute_backup:
            call_command("backup_readiness", stdout=output)
        execute_backup.assert_not_called()
        self.assertIn("CODE_READY", output.getvalue())

    def test_16_readiness_command_performs_no_object_upload(self):
        with mock.patch(
            "apps.backups.engine.s3_storage."
            "S3CompatibleDurableStorageProvider.store_encrypted_artifact"
        ) as upload:
            call_command("backup_readiness", stdout=io.StringIO())
        upload.assert_not_called()

    def test_17_readiness_command_performs_no_object_deletion(self):
        with mock.patch(
            "apps.backups.engine.s3_storage."
            "S3CompatibleDurableStorageProvider.delete_stored_object"
        ) as delete:
            call_command("backup_readiness", stdout=io.StringIO())
        delete.assert_not_called()

    def test_18_readiness_command_performs_no_restore_mutation(self):
        with mock.patch(
            "apps.backups.engine.restore_mutation.RestoreExecutionCoordinator.execute"
        ) as restore:
            call_command("backup_readiness", stdout=io.StringIO())
        restore.assert_not_called()

    def test_19_owner_manual_backup_remains_disabled_by_default(self):
        capability = owner_services.manual_backup_capability()
        self.assertFalse(capability.enabled)
        self.assertIn("unavailable", capability.message.lower())

    def test_20_platform_health_renders_disabled_default_state(self):
        self.client.force_login(self.platform_admin)
        response = self.client.get(reverse("platformadmin:backup_health"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Production activation gate")
        self.assertContains(response, "Operational gate")
        self.assertContains(response, "Closed")
        self.assertContains(response, "Restore UAT")

    def test_21_platform_health_does_not_render_credentials(self):
        configured = _production_settings(self.staging_root)
        configured.update(
            {
                "CELERY_BROKER_URL": (
                    "rediss://credential-user:credential-password@broker.example/1"
                ),
                "BACKUP_AWS_KMS_KEY_ID": "alias/credential-marker",
                "BACKUP_S3_BUCKET": "credential-marker-bucket",
            }
        )
        self.client.force_login(self.platform_admin)
        with override_settings(**configured):
            response = self.client.get(reverse("platformadmin:backup_health"))
        rendered = response.content.decode()
        self.assertNotIn("credential-password", rendered)
        self.assertNotIn("credential-marker", rendered)

    def test_22_disabled_deployment_still_passes_django_checks(self):
        self.assertEqual(run_checks(), [])

    def test_23_worker_routes_are_exact_and_separate(self):
        self.assertEqual(
            settings.CELERY_TASK_ROUTES[BACKUP_EXECUTION_TASK_NAME]["queue"],
            BACKUP_QUEUE_NAME,
        )
        self.assertEqual(
            settings.CELERY_TASK_ROUTES[RESTORE_EXECUTION_TASK_NAME]["queue"],
            RESTORE_QUEUE_NAME,
        )
        self.assertEqual(
            settings.CELERY_TASK_ROUTES[SCHEDULE_DISPATCH_TASK_NAME]["queue"],
            BACKUP_SCHEDULER_QUEUE_NAME,
        )
        self.assertEqual(len({BACKUP_QUEUE_NAME, RESTORE_QUEUE_NAME, BACKUP_SCHEDULER_QUEUE_NAME}), 3)

    def test_24_beat_has_one_scheduler_and_reconciliation_entry(self):
        tasks = [entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()]
        self.assertEqual(tasks.count(SCHEDULE_DISPATCH_TASK_NAME), 1)
        self.assertEqual(tasks.count(RECONCILIATION_TASK_NAME), 1)

    def test_25_locked_retention_policy_remains_five(self):
        self.assertEqual(DAILY_FULL_KEEP_COUNT, 5)
        self.assertEqual(settings.BACKUP_RETENTION_DAILY_FULL_KEEP_COUNT, 5)

    def test_26_safety_backups_remain_retention_ineligible(self):
        source = self.make_backup()
        type(source).objects.filter(pk=source.pk).update(
            status=BackupStatus.SUCCEEDED,
            integrity_status=IntegrityStatus.VERIFIED,
        )
        source.refresh_from_db()
        restore = services.create_restore_request(
            business=self.business_a,
            source_backup=source,
            requested_scope=BackupScope.POS,
            actor=self.owner_a,
            reason="Phase 3I retention safety contract",
            idempotency_key=f"phase3i-restore:{uuid.uuid4()}",
        )
        safety = services.create_backup_request(
            business=self.business_a,
            scope=BackupScope.ALL_ENABLED,
            trigger=BackupTrigger.PRE_RESTORE_SAFETY,
            parent_restore_operation=restore,
            system_actor=True,
            idempotency_key=f"phase3i-safety:{uuid.uuid4()}",
        )
        self.assertTrue(safety.protected)
        self.assertFalse(safety.retention_eligible)

    def test_27_no_backup_download_action_exists(self):
        from apps.backups.urls import urlpatterns as owner_patterns
        from apps.platformadmin.urls import urlpatterns as platform_patterns

        names = {pattern.name for pattern in (*owner_patterns, *platform_patterns)}
        self.assertNotIn("download", names)
        self.assertNotIn("backup_download", names)

    def test_28_provider_attestation_is_explicit_only(self):
        key, storage = _provider_mocks()
        configured = _production_settings(self.staging_root)
        with override_settings(**configured):
            result = assess_production_activation_readiness(
                key_provider=key,
                storage_provider=storage,
            )
        key.health_check.assert_not_called()
        storage.health_attestation.assert_not_called()
        self.assertFalse(result.infrastructure_ready)

    def test_29_backup_uat_readiness_does_not_fake_execution_availability(self):
        result = self._ready_result()
        self.assertTrue(result.ready_for_backup_uat)
        self.assertFalse(result.backup_execution_enabled)
        self.assertFalse(availability.real_execution_available())

    def test_30_postgresql_is_rejected_by_v1_activation_gate(self):
        databases = {
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "nexa",
            }
        }
        result = self._ready_result(DATABASES=databases)
        database = next(
            check for check in result.checks if check.identifier == "DATABASE_RUNTIME"
        )
        self.assertFalse(database.ready)
        self.assertFalse(result.ready_for_backup_uat)

    def test_31_readiness_json_is_secret_free(self):
        configured = _production_settings(self.staging_root)
        configured["CELERY_BROKER_URL"] = (
            "rediss://credential-user:credential-password@broker.example/1"
        )
        key, storage = _provider_mocks()
        with override_settings(**configured):
            rendered = repr(
                assess_production_activation_readiness(
                    attest_providers=True,
                    key_provider=key,
                    storage_provider=storage,
                ).as_dict()
            )
        self.assertNotIn("credential-user", rendered)
        self.assertNotIn("credential-password", rendered)
        self.assertNotIn(str(self.staging_root), rendered)

    def test_32_operational_gate_decision_remains_false(self):
        self.assertFalse(availability.OPERATIONAL_PROVIDER_STACK_READY)
        result = self._ready_result()
        self.assertFalse(result.operational_provider_stack_ready)
        self.assertTrue(
            any("kill switch" in blocker for blocker in result.restore_uat_blockers)
        )


def load_tests(loader, standard_tests, pattern):
    del standard_tests, pattern
    return loader.loadTestsFromTestCase(BackupPhase3IActivationReadinessTests)
