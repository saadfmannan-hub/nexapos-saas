"""Phase 3H production-activation reliability and observability tests."""

from __future__ import annotations

import inspect
import uuid
from contextlib import contextmanager
from datetime import time, timedelta
from types import SimpleNamespace
from unittest import mock

from django.core.checks import run_checks
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.backups import dispatch, owner_services, platform_services, services
from apps.backups.engine import availability, events
from apps.backups.engine.runtime_exceptions import RuntimeStateError
from apps.backups.enums import (
    BackupScope,
    BackupStatus,
    BackupTrigger,
    CompatibilityStatus,
    IntegrityStatus,
    OperationKind,
    RestoreStatus,
)
from apps.backups.models import (
    BackupActivity,
    BackupRecord,
    BackupSchedule,
    RestoreOperation,
    TenantOperationLock,
)
from apps.backups.operational_health import operations_health_snapshot
from apps.backups.operational_readiness import (
    ReadinessCategory,
    ReadinessState,
    assess_operational_readiness,
)
from apps.backups.reconciliation import (
    StaleBackupCategory,
    classify_backup_operation,
    reconcile_queued_backup_dispatches,
    reconcile_queued_restore_dispatches,
    reconcile_stale_backup_operations,
)
from apps.backups.restore_execution import (
    StaleRestoreCategory,
    claim_restore_operation,
    reconcile_stale_restore_operations,
)
from apps.backups.scheduling import dispatch_due_schedules
from apps.backups.tasks import (
    BACKUP_QUEUE_NAME,
    BACKUP_SCHEDULER_QUEUE_NAME,
    RECONCILIATION_TASK_NAME,
    RESTORE_QUEUE_NAME,
    execute_backup,
    execute_restore,
    reconcile_backup_control_plane,
)

from .test_backups_phase1 import BackupPhase1TestCase

PRODUCTION_SETTINGS = {
    "BACKUP_KEY_PROVIDER": "aws_kms",
    "BACKUP_AWS_KMS_KEY_ID": "alias/nexa-backups",
    "BACKUP_AWS_REGION": "us-east-1",
    "BACKUP_STORAGE_PROVIDER": "s3",
    "BACKUP_S3_BUCKET": "nexa-backups-test",
    "BACKUP_S3_REGION": "nyc3",
    "BACKUP_S3_ENDPOINT_URL": "https://nyc3.digitaloceanspaces.com",
    "BACKUP_S3_PREFIX": "nexa/backups",
    "CELERY_BROKER_URL": "redis://broker.example/1",
    "CELERY_TASK_ALWAYS_EAGER": False,
}


@contextmanager
def _worker_request(task, *, queue):
    task.push_request(
        id=f"phase3h-{uuid.uuid4().hex}",
        called_directly=False,
        is_eager=False,
        retries=0,
        delivery_info={"routing_key": queue},
    )
    try:
        yield
    finally:
        task.pop_request()


class BackupPhase3HActivationHardeningTests(BackupPhase1TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.set_entitlements(cls, cls.business_a, pos=True, wms=False)
        cls.set_entitlements(cls, cls.business_b, pos=True, wms=False)
        cls.platform_admin = User.objects.create_superuser(
            email="phase3h-platform@example.com",
            password="StrongPass123!",
            full_name="Phase 3H Platform Admin",
        )

    def _backup(self, *, status=BackupStatus.QUEUED, trigger=BackupTrigger.MANUAL, **values):
        kwargs = self.backup_model_kwargs(
            business=self.business_a,
            scope=BackupScope.POS,
            status=status,
            trigger=trigger,
            created_by=self.owner_a,
            **values,
        )
        return BackupRecord.objects.create(**kwargs)

    def _source_backup(self):
        return self._backup(
            status=BackupStatus.SUCCEEDED,
            integrity_status=IntegrityStatus.VERIFIED,
            compatibility_status=CompatibilityStatus.COMPATIBLE,
            storage_backend_identifier="local-private-filesystem",
            opaque_object_key=str(uuid.uuid4()),
            whole_artifact_hash="b" * 64,
            completed_at=timezone.now(),
            verified_at=timezone.now(),
        )

    def _restore(self, *, status=RestoreStatus.QUEUED, queue_intent=False, **values):
        restore = RestoreOperation.objects.create(
            business=self.business_a,
            source_backup=values.pop("source_backup", self._source_backup()),
            requested_scope=BackupScope.POS,
            requested_by=self.owner_a,
            reason="Phase 3H controlled recovery test",
            compatibility_status=CompatibilityStatus.COMPATIBLE,
            idempotency_key=f"phase3h:{uuid.uuid4()}",
            status=status,
            **values,
        )
        if queue_intent:
            services.create_backup_activity(
                business=self.business_a,
                backup=restore.source_backup,
                restore=restore,
                actor=self.owner_a,
                event_type=events.RESTORE_QUEUED,
                sanitized_message="Restore request queued for its dedicated worker.",
            )
        return restore

    def _intent_backup(self):
        backup = self._backup()
        dispatch.record_backup_dispatch_intent(backup)
        return backup

    def test_01_disabled_deployment_checks_remain_clean(self):
        self.assertEqual(run_checks(), [])

    def test_02_production_flags_default_fail_closed(self):
        self.assertTrue(availability.PRODUCTION_KEY_PROVIDER_READY)
        self.assertTrue(availability.PRODUCTION_DURABLE_STORAGE_PROVIDER_READY)
        self.assertFalse(availability.OPERATIONAL_PROVIDER_STACK_READY)
        self.assertFalse(availability.real_execution_available())
        self.assertFalse(availability.restore_execution_available())

    def test_03_manual_backup_broker_failure_preserves_queued_record(self):
        with (
            mock.patch.object(
                owner_services,
                "manual_backup_capability",
                return_value=owner_services.OwnerActionCapability(True, "Available"),
            ),
            mock.patch.object(owner_services, "_enqueue_backup", side_effect=RuntimeError("secret")) as publish,
            self.assertRaises(owner_services.OwnerBackupActionUnavailable),
        ):
            owner_services.request_manual_backup(
                business=self.business_a,
                actor=self.owner_a,
                scope=BackupScope.POS,
            )
        backup = BackupRecord.objects.filter(status=BackupStatus.QUEUED).latest("created_at")
        self.assertEqual(publish.call_count, 3)
        self.assertEqual(backup.failure_code, "")
        self.assertTrue(BackupActivity.objects.filter(backup=backup, event_type=events.BACKUP_DISPATCH_FAILED).exists())

    def test_04_platform_manual_broker_failure_preserves_queued_record(self):
        with (
            mock.patch.object(platform_services, "manual_backup_capability", return_value=platform_services.PlatformActionCapability(True, "Available")),
            mock.patch.object(platform_services, "_enqueue_backup", side_effect=RuntimeError),
            self.assertRaises(platform_services.PlatformBackupActionUnavailable),
        ):
            platform_services.platform_request_manual_backup(
                business=self.business_a,
                actor=self.platform_admin,
                scope=BackupScope.POS,
            )
        self.assertTrue(BackupRecord.objects.filter(status=BackupStatus.QUEUED).exists())

    def test_05_scheduled_broker_failure_preserves_occurrence(self):
        now = timezone.now()
        BackupSchedule.objects.create(
            business=self.business_a,
            enabled=True,
            timezone_name="Asia/Muscat",
            local_execution_time=time(3, 0),
            next_run=now - timedelta(minutes=1),
            scope=BackupScope.ALL_ENABLED,
            created_by=self.owner_a,
        )
        publish = mock.Mock(side_effect=RuntimeError("broker-secret"))
        with self.captureOnCommitCallbacks(execute=True):
            result = dispatch_due_schedules(enqueue=publish, now=now)
        backup = BackupRecord.objects.get(trigger=BackupTrigger.SCHEDULED)
        self.assertEqual(result.dispatched_count, 1)
        self.assertEqual(publish.call_count, 3)
        self.assertEqual(backup.status, BackupStatus.QUEUED)

    def test_06_restore_broker_failure_preserves_exact_queued_state(self):
        restore = self._restore()
        with (
            mock.patch.object(owner_services, "restore_mutation_capability", return_value=owner_services.OwnerActionCapability(True, "Available")),
            mock.patch.object(owner_services, "_enqueue_restore", side_effect=RuntimeError) as publish,
            self.assertRaises(owner_services.OwnerBackupActionUnavailable),
        ):
            owner_services.request_restore(
                business=self.business_a,
                backup=restore.source_backup,
                restore=restore,
                actor=self.owner_a,
            )
        restore.refresh_from_db()
        self.assertEqual(restore.status, RestoreStatus.QUEUED)
        self.assertIsNone(restore.started_at)
        self.assertEqual(publish.call_count, 3)

    def test_07_backup_successful_handoff_is_confirmed(self):
        backup = self._intent_backup()
        outcome = dispatch.dispatch_backup(backup=backup, publisher=mock.Mock())
        self.assertTrue(outcome.confirmed)
        self.assertTrue(BackupActivity.objects.filter(backup=backup, event_type=events.BACKUP_DISPATCH_CONFIRMED).exists())

    def test_08_restore_successful_handoff_is_confirmed(self):
        restore = self._restore(queue_intent=True)
        outcome = dispatch.dispatch_restore(restore=restore, publisher=mock.Mock())
        self.assertTrue(outcome.confirmed)
        self.assertTrue(BackupActivity.objects.filter(restore=restore, event_type=events.RESTORE_DISPATCH_CONFIRMED).exists())

    def test_09_phase3h_backup_intent_is_republish_eligible(self):
        self.assertTrue(dispatch.backup_dispatch_eligible(self._intent_backup()))

    def test_10_legacy_backup_without_intent_is_not_republished(self):
        self.assertFalse(dispatch.backup_dispatch_eligible(self._backup()))

    def test_11_confirmed_backup_is_not_republished(self):
        backup = self._intent_backup()
        dispatch.dispatch_backup(backup=backup, publisher=mock.Mock())
        self.assertFalse(dispatch.backup_dispatch_eligible(backup))

    def test_12_successful_backup_is_not_republished(self):
        self.assertFalse(dispatch.backup_dispatch_eligible(self._source_backup()))

    def test_13_failed_backup_is_not_republished(self):
        self.assertFalse(dispatch.backup_dispatch_eligible(self._backup(status=BackupStatus.FAILED)))

    def test_14_active_backup_lease_blocks_redispatch(self):
        backup = self._intent_backup()
        now = timezone.now()
        TenantOperationLock.objects.create(
            business=self.business_a,
            operation_kind=OperationKind.BACKUP,
            operation_public_id=backup.public_id,
            acquired_at=now,
            lease_expires_at=now + timedelta(hours=1),
        )
        self.assertFalse(dispatch.backup_dispatch_eligible(backup))

    def test_15_provider_metadata_blocks_backup_redispatch(self):
        backup = self._intent_backup()
        BackupRecord.objects.filter(pk=backup.pk).update(opaque_object_key=str(uuid.uuid4()))
        backup.refresh_from_db()
        self.assertFalse(dispatch.backup_dispatch_eligible(backup))

    def test_16_exact_queued_restore_is_republish_eligible(self):
        self.assertTrue(dispatch.restore_dispatch_eligible(self._restore(queue_intent=True)))

    def test_17_preflight_only_restore_is_not_republished(self):
        self.assertFalse(dispatch.restore_dispatch_eligible(self._restore()))

    def test_18_confirmed_restore_is_not_republished(self):
        restore = self._restore(queue_intent=True)
        dispatch.dispatch_restore(restore=restore, publisher=mock.Mock())
        self.assertFalse(dispatch.restore_dispatch_eligible(restore))

    def test_19_active_restore_is_never_republished(self):
        restore = self._restore(status=RestoreStatus.AUTHORIZING, queue_intent=True)
        self.assertFalse(dispatch.restore_dispatch_eligible(restore))

    def test_20_ambiguous_restore_is_never_republished(self):
        restore = self._restore(status=RestoreStatus.RESTORING, queue_intent=True)
        self.assertFalse(dispatch.restore_dispatch_eligible(restore))

    def test_21_worker_start_evidence_blocks_restore_redispatch(self):
        restore = self._restore(queue_intent=True)
        services.create_backup_activity(
            business=self.business_a,
            backup=restore.source_backup,
            restore=restore,
            event_type=events.RESTORE_WORKER_STARTED,
        )
        self.assertFalse(dispatch.restore_dispatch_eligible(restore))

    def test_22_queued_backup_reconciliation_publishes_only_eligible(self):
        eligible = self._intent_backup()
        legacy = self._backup()
        publisher = mock.Mock()
        result = reconcile_queued_backup_dispatches(
            publisher=publisher,
            eligible_before=timezone.now() + timedelta(seconds=1),
        )
        self.assertEqual(result.confirmed_count, 1)
        publisher.assert_called_once()
        self.assertTrue(BackupRecord.objects.filter(pk=legacy.pk).exists())
        self.assertTrue(BackupActivity.objects.filter(backup=eligible, event_type=events.BACKUP_REDISPATCHED).exists())

    def test_23_queued_restore_reconciliation_publishes_exact_queue_intent(self):
        restore = self._restore(queue_intent=True)
        self._restore()
        publisher = mock.Mock()
        result = reconcile_queued_restore_dispatches(
            publisher=publisher,
            eligible_before=timezone.now() + timedelta(seconds=1),
        )
        self.assertEqual(result.confirmed_count, 1)
        self.assertTrue(BackupActivity.objects.filter(restore=restore, event_type=events.RESTORE_REDISPATCHED).exists())

    def test_24_reconciliation_does_not_mutate_tenant_business_data(self):
        backup = self._intent_backup()
        before = self.business_a.name
        reconcile_queued_backup_dispatches(
            publisher=mock.Mock(), eligible_before=timezone.now() + timedelta(seconds=1)
        )
        self.business_a.refresh_from_db()
        backup.refresh_from_db()
        self.assertEqual(self.business_a.name, before)
        self.assertEqual(backup.status, BackupStatus.QUEUED)

    def test_25_reconciliation_has_no_provider_delete_boundary(self):
        source = inspect.getsource(reconcile_queued_backup_dispatches)
        self.assertNotIn("delete_stored_object", source)
        self.assertNotIn("retention", source.lower())

    def test_26_backup_dispatch_retry_is_bounded(self):
        backup = self._intent_backup()
        publisher = mock.Mock(side_effect=RuntimeError("credential-value"))
        outcome = dispatch.dispatch_backup(backup=backup, publisher=publisher)
        self.assertFalse(outcome.confirmed)
        self.assertEqual(publisher.call_count, 3)

    def test_27_dispatch_errors_are_sanitized(self):
        backup = self._intent_backup()
        dispatch.dispatch_backup(
            backup=backup,
            publisher=mock.Mock(side_effect=RuntimeError("credential-value")),
        )
        rendered = " ".join(
            BackupActivity.objects.filter(backup=backup).values_list(
                "sanitized_message", flat=True
            )
        )
        self.assertNotIn("credential-value", rendered)

    def test_28_duplicate_backup_delivery_executes_claim_once(self):
        backup = self._backup()
        executions = []

        def runtime(**kwargs):
            current = BackupRecord.objects.get(pk=backup.pk)
            if current.status != BackupStatus.QUEUED:
                raise RuntimeStateError()
            services.transition_backup(current, BackupStatus.PREPARING)
            executions.append(kwargs)
            return SimpleNamespace(
                backup_public_id=backup.public_id,
                business_public_id=self.business_a.public_id,
                final_status=BackupStatus.PREPARING,
                retention_outcome="NOT_RUN",
                retention_warning_code="",
                provider_stack_version="phase3h-test",
            )

        with (
            mock.patch("apps.backups.engine.runtime.request_backup_execution", side_effect=runtime),
            mock.patch("apps.backups.tasks.assert_real_execution_available"),
            mock.patch("apps.backups.tasks.assert_safe_async_execution_configuration"),
            _worker_request(execute_backup, queue=BACKUP_QUEUE_NAME),
        ):
            execute_backup.run(str(backup.public_id), str(self.business_a.public_id))
            with self.assertRaises(RuntimeStateError):
                execute_backup.run(str(backup.public_id), str(self.business_a.public_id))
        self.assertEqual(len(executions), 1)

    def test_29_duplicate_restore_delivery_cannot_claim_twice(self):
        restore = self._restore(queue_intent=True)
        first = claim_restore_operation(
            restore_public_id=restore.public_id,
            business_public_id=self.business_a.public_id,
        )
        second = claim_restore_operation(
            restore_public_id=restore.public_id,
            business_public_id=self.business_a.public_id,
        )
        self.assertEqual(first.state.value, "CLAIMED")
        self.assertEqual(second.state.value, "ACTIVE_OR_AMBIGUOUS")

    def test_30_stale_queued_backup_is_classified(self):
        backup = self._backup()
        self.assertEqual(classify_backup_operation(backup).category, StaleBackupCategory.STALE_QUEUED)

    def test_31_transitional_backup_is_classified_pre_durable(self):
        backup = self._backup(status=BackupStatus.SNAPSHOTTING)
        self.assertEqual(classify_backup_operation(backup).category, StaleBackupCategory.STALE_PRE_DURABLE)

    def test_32_uploading_backup_is_ambiguous(self):
        backup = self._backup(status=BackupStatus.UPLOADING)
        self.assertEqual(classify_backup_operation(backup).category, StaleBackupCategory.AMBIGUOUS_PROVIDER_STAGE)

    def test_33_verified_durable_metadata_pending_db_is_distinct(self):
        backup = self._backup(
            status=BackupStatus.VERIFYING,
            storage_backend_identifier="s3-compatible",
            opaque_object_key="nexa/tenant/backup/artifact.bin",
            storage_bucket_identifier="private-bucket",
            whole_artifact_hash="a" * 64,
        )
        self.assertEqual(classify_backup_operation(backup).category, StaleBackupCategory.DURABLE_OBJECT_VERIFIED_PENDING_DB)

    def test_34_stale_backup_reconciliation_is_classification_only(self):
        backup = self._backup(status=BackupStatus.PACKAGING)
        old = timezone.now() - timedelta(days=1)
        BackupRecord.objects.filter(pk=backup.pk).update(updated_at=old)
        rows = reconcile_stale_backup_operations(stale_before=timezone.now() - timedelta(hours=1))
        backup.refresh_from_db()
        self.assertEqual(rows[0].category, StaleBackupCategory.STALE_PRE_DURABLE)
        self.assertEqual(backup.status, BackupStatus.PACKAGING)

    def test_35_stale_restore_classification_is_preserved(self):
        restore = self._restore(status=RestoreStatus.RESTORING)
        RestoreOperation.objects.filter(pk=restore.pk).update(updated_at=timezone.now() - timedelta(days=1))
        rows = reconcile_stale_restore_operations(stale_before=timezone.now() - timedelta(hours=1))
        self.assertEqual(rows[0].category, StaleRestoreCategory.AMBIGUOUS_MUTATION)

    def test_36_expired_active_lease_does_not_trigger_destructive_replay(self):
        restore = self._restore(queue_intent=True)
        now = timezone.now()
        TenantOperationLock.objects.create(
            business=self.business_a,
            operation_kind=OperationKind.RESTORE,
            operation_public_id=restore.public_id,
            acquired_at=now - timedelta(hours=2),
            lease_expires_at=now - timedelta(hours=1),
            active=True,
        )
        self.assertFalse(dispatch.restore_dispatch_eligible(restore))

    def test_37_readiness_result_hides_credentials(self):
        with override_settings(**PRODUCTION_SETTINGS):
            result = assess_operational_readiness(attest_providers=False)
        rendered = repr(result.as_dict())
        self.assertNotIn("redis://broker.example", rendered)
        self.assertNotIn("alias/nexa-backups", rendered)

    def test_38_readiness_is_non_networking_by_default(self):
        key = mock.Mock()
        storage = mock.Mock()
        with override_settings(**PRODUCTION_SETTINGS):
            assess_operational_readiness(
                attest_providers=False,
                key_provider=key,
                storage_provider=storage,
            )
        key.health_check.assert_not_called()
        storage.health_attestation.assert_not_called()

    def test_39_kms_readiness_uses_non_destructive_health_check(self):
        key = mock.Mock()
        key.health_check.return_value = SimpleNamespace(
            provider_identifier="aws-kms-v1", reachable=True, enabled=True
        )
        storage = mock.Mock()
        storage.health_attestation.return_value = True
        with override_settings(**PRODUCTION_SETTINGS):
            result = assess_operational_readiness(
                attest_providers=True, key_provider=key, storage_provider=storage
            )
        key.health_check.assert_called_once_with()
        self.assertEqual(result.checks[0].state, ReadinessState.READY)

    def test_40_s3_readiness_never_uploads_or_deletes(self):
        key = mock.Mock()
        key.health_check.return_value = SimpleNamespace(
            provider_identifier="aws-kms-v1", reachable=True, enabled=True
        )
        storage = mock.Mock()
        storage.health_attestation.return_value = True
        with override_settings(**PRODUCTION_SETTINGS):
            assess_operational_readiness(
                attest_providers=True, key_provider=key, storage_provider=storage
            )
        storage.health_attestation.assert_called_once_with()
        self.assertFalse(any("upload" in call[0] or "delete" in call[0] for call in storage.method_calls))

    def test_41_local_kms_is_rejected_for_activation(self):
        result = assess_operational_readiness(attest_providers=False)
        check = next(item for item in result.checks if item.category == ReadinessCategory.KEY_MANAGEMENT)
        self.assertEqual(check.state, ReadinessState.NOT_READY)

    def test_42_local_storage_is_rejected_for_activation(self):
        result = assess_operational_readiness(attest_providers=False)
        check = next(item for item in result.checks if item.category == ReadinessCategory.DURABLE_STORAGE)
        self.assertEqual(check.state, ReadinessState.NOT_READY)

    def test_43_production_configuration_reaches_code_ready_in_mocks(self):
        key = mock.Mock()
        key.health_check.return_value = SimpleNamespace(
            provider_identifier="aws-kms-v1", reachable=True, enabled=True
        )
        storage = mock.Mock()
        storage.health_attestation.return_value = True
        with override_settings(**PRODUCTION_SETTINGS):
            result = assess_operational_readiness(
                attest_providers=True, key_provider=key, storage_provider=storage
            )
        self.assertTrue(result.code_ready)

    def test_44_platform_health_page_is_admin_only(self):
        self.client.force_login(self.platform_admin)
        response = self.client.get(reverse("platformadmin:backup_health"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Backup operations health")

    def test_45_owner_cannot_access_platform_health_page(self):
        self.client.force_login(self.owner_a)
        response = self.client.get(reverse("platformadmin:backup_health"))
        self.assertEqual(response.status_code, 403)

    def test_46_queue_backlog_and_stale_counts_are_db_authoritative(self):
        backup = self._backup()
        restore = self._restore(queue_intent=True)
        old = timezone.now() - timedelta(days=1)
        BackupRecord.objects.filter(pk=backup.pk).update(queued_at=old, updated_at=old)
        RestoreOperation.objects.filter(pk=restore.pk).update(created_at=old, updated_at=old)
        health = operations_health_snapshot()
        self.assertGreaterEqual(health.queued_backups, 1)
        self.assertGreaterEqual(health.queued_restores, 1)
        self.assertGreaterEqual(health.stale_backups, 1)
        self.assertGreaterEqual(health.stale_restores, 1)

    def test_47_recovery_required_is_surfaced_prominently(self):
        self._restore(status=RestoreStatus.INDETERMINATE)
        self.client.force_login(self.platform_admin)
        response = self.client.get(reverse("platformadmin:backup_health"))
        self.assertContains(response, "RECOVERY_REQUIRED")

    def test_48_beat_has_one_reconciliation_control_entry(self):
        from django.conf import settings

        entries = [
            value
            for value in settings.CELERY_BEAT_SCHEDULE.values()
            if value.get("task") == RECONCILIATION_TASK_NAME
        ]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["options"]["queue"], BACKUP_SCHEDULER_QUEUE_NAME)

    def test_49_backup_restore_and_control_queues_remain_separate(self):
        self.assertEqual(execute_backup.queue, BACKUP_QUEUE_NAME)
        self.assertEqual(execute_restore.queue, RESTORE_QUEUE_NAME)
        self.assertEqual(reconcile_backup_control_plane.queue, BACKUP_SCHEDULER_QUEUE_NAME)
        self.assertEqual(len({BACKUP_QUEUE_NAME, RESTORE_QUEUE_NAME, BACKUP_SCHEDULER_QUEUE_NAME}), 3)

    def test_50_celery_result_backend_is_not_authoritative(self):
        source = " ".join(
            inspect.getsource(value)
            for value in (
                dispatch.backup_dispatch_eligible,
                dispatch.restore_dispatch_eligible,
                reconcile_queued_backup_dispatches,
                reconcile_queued_restore_dispatches,
            )
        )
        self.assertNotIn("result_backend", source)
        self.assertNotIn("AsyncResult", source)

    def test_51_disabled_reconciliation_task_executes_nothing(self):
        with (
            mock.patch("apps.backups.reconciliation.reconcile_queued_backup_dispatches") as backups,
            mock.patch("apps.backups.reconciliation.reconcile_queued_restore_dispatches") as restores,
        ):
            result = reconcile_backup_control_plane.run()
        self.assertEqual(result, {"state": "DISABLED"})
        backups.assert_not_called()
        restores.assert_not_called()


def load_tests(loader, standard_tests, pattern):
    del standard_tests, pattern
    return loader.loadTestsFromTestCase(BackupPhase3HActivationHardeningTests)
