"""Phase 3E restart-safe restore worker and async UI boundary tests."""

from __future__ import annotations

import inspect
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from celery import Task, current_app
from celery.exceptions import Retry
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.backups import owner_services, platform_services
from apps.backups.engine import availability, events
from apps.backups.engine.restore_exceptions import (
    RestoreCompatibilityError,
    RestoreLockUnavailable,
    RestoreMutationError,
    RestoreRecoveryRequired,
    RestoreTenantMismatch,
)
from apps.backups.enums import (
    BackupScope,
    BackupStatus,
    BackupTrigger,
    CompatibilityStatus,
    IntegrityStatus,
    RestoreStatus,
)
from apps.backups.models import BackupActivity, BackupRecord, RestoreOperation
from apps.backups.owner_services import OwnerActionCapability
from apps.backups.platform_services import PlatformActionCapability
from apps.backups.restore_execution import (
    RestoreClaimState,
    StaleRestoreCategory,
    claim_restore_operation,
    reconcile_stale_restore_operations,
)
from apps.backups.tasks import (
    BACKUP_EXECUTION_TASK_NAME,
    BACKUP_QUEUE_NAME,
    BACKUP_SCHEDULER_QUEUE_NAME,
    RESTORE_EXECUTION_TASK_NAME,
    RESTORE_QUEUE_NAME,
    SCHEDULE_DISPATCH_TASK_NAME,
    assert_safe_restore_async_execution_configuration,
    check_restore_async_execution_configuration,
    execute_restore,
)

from .test_backups_phase1 import BackupPhase1TestCase

SAFE_ROUTES = {
    BACKUP_EXECUTION_TASK_NAME: {"queue": BACKUP_QUEUE_NAME},
    RESTORE_EXECUTION_TASK_NAME: {"queue": RESTORE_QUEUE_NAME},
    SCHEDULE_DISPATCH_TASK_NAME: {"queue": BACKUP_SCHEDULER_QUEUE_NAME},
}


@contextmanager
def _worker_request(task, *, queue=RESTORE_QUEUE_NAME, retries=0):
    task.push_request(
        id=f"phase3e-{uuid.uuid4().hex}",
        called_directly=False,
        is_eager=False,
        retries=retries,
        delivery_info={"routing_key": queue},
    )
    try:
        yield
    finally:
        task.pop_request()


class BackupPhase3ERestoreWorkerTests(BackupPhase1TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.set_entitlements(cls, cls.business_a, pos=True, wms=False)
        cls.set_entitlements(cls, cls.business_b, pos=True, wms=False)
        cls.platform_admin = User.objects.create_superuser(
            email="phase3e-platform@example.com",
            password="StrongPass123!",
            full_name="Phase 3E Platform Admin",
        )

    def setUp(self):
        self.client.force_login(self.owner_a)

    def _backup(self, *, business=None, **overrides):
        business = business or self.business_a
        values = self.backup_model_kwargs(
            business=business,
            scope=BackupScope.POS,
            status=BackupStatus.SUCCEEDED,
            integrity_status=IntegrityStatus.VERIFIED,
            compatibility_status=CompatibilityStatus.COMPATIBLE,
            storage_backend_identifier="private-store",
            opaque_object_key=str(uuid.uuid4()),
            whole_artifact_hash="b" * 64,
            backup_size_bytes=4096,
            completed_at=timezone.now(),
            verified_at=timezone.now(),
            **overrides,
        )
        return BackupRecord.objects.create(**values)

    def _restore(self, *, business=None, actor=None, status=RestoreStatus.QUEUED):
        business = business or self.business_a
        actor = actor or business.owner
        source = self._backup(business=business)
        return RestoreOperation.objects.create(
            business=business,
            source_backup=source,
            requested_scope=BackupScope.POS,
            requested_by=actor,
            reason="Phase 3E recovery test",
            compatibility_status=CompatibilityStatus.COMPATIBLE,
            idempotency_key=f"phase3e:{uuid.uuid4()}",
            status=status,
        )

    def _owner_preflight_session(self, restore):
        session = self.client.session
        session["backups_owner_preflight"] = {
            "business_public_id": str(restore.business.public_id),
            "backup_public_id": str(restore.source_backup.public_id),
            "restore_public_id": str(restore.public_id),
            "ready": True,
            "compatibility": "Compatible",
            "component_count": 2,
            "record_count": 8,
            "media_count": 1,
            "messages": ["Restore readiness checks passed."],
        }
        session.save()

    def _platform_preflight_session(self, restore):
        session = self.client.session
        session["backups_platform_preflight"] = {
            "business_public_id": str(restore.business.public_id),
            "backup_public_id": str(restore.source_backup.public_id),
            "restore_public_id": str(restore.public_id),
            "actor_public_id": str(self.platform_admin.public_id),
            "ready": True,
            "compatibility": "Compatible",
            "component_count": 2,
            "record_count": 8,
            "media_count": 1,
            "messages": ["Restore readiness checks passed."],
        }
        session.save()

    def _safe_settings(self):
        return override_settings(
            BACKUP_RESTORE_MUTATION_ENABLED=True,
            BACKUP_KEY_PROVIDER="aws_kms",
            BACKUP_AWS_KMS_KEY_ID="alias/nexa-backups",
            BACKUP_AWS_REGION="us-east-1",
            CELERY_BROKER_URL="redis://broker.example/1",
            CELERY_TASK_ALWAYS_EAGER=False,
            BACKUP_RESTORE_QUEUE_NAME=RESTORE_QUEUE_NAME,
            CELERY_TASK_ROUTES=SAFE_ROUTES,
            BACKUP_EXECUTION_TASK_TIME_LIMIT_SECONDS=21_900,
            BACKUP_RESTORE_TASK_SOFT_TIME_LIMIT_SECONDS=43_200,
            BACKUP_RESTORE_TASK_TIME_LIMIT_SECONDS=43_500,
        )

    @contextmanager
    def _mock_runtime(self, restore, *, preflight_error=None):
        approved = SimpleNamespace(restore_ready=True)
        stack = mock.Mock()
        stack.validated.return_value = stack
        if preflight_error is None:
            stack.preflight_coordinator.run.return_value = approved
        else:
            stack.preflight_coordinator.run.side_effect = preflight_error
        result = SimpleNamespace(restore_operation_public_id=restore.public_id)

        def complete(_request):
            RestoreOperation.objects.filter(pk=restore.pk).update(
                status=RestoreStatus.SUCCEEDED,
                completed_at=timezone.now(),
                updated_at=timezone.now(),
            )
            return result

        coordinator = mock.Mock()
        coordinator.execute.side_effect = complete
        with (
            mock.patch(
                "apps.backups.engine.restore_mutation.build_restore_runtime_stack",
                return_value=stack,
            ),
            mock.patch(
                "apps.backups.engine.restore_mutation.RestoreExecutionCoordinator",
                return_value=coordinator,
            ),
        ):
            yield stack, coordinator

    def test_execute_restore_is_registered_celery_task(self):
        self.assertIsInstance(execute_restore, Task)
        self.assertEqual(current_app.tasks[RESTORE_EXECUTION_TASK_NAME], execute_restore)

    def test_restore_task_routes_only_to_dedicated_queue(self):
        self.assertEqual(execute_restore.name, RESTORE_EXECUTION_TASK_NAME)
        self.assertEqual(execute_restore.queue, RESTORE_QUEUE_NAME)
        self.assertEqual(settings_route := SAFE_ROUTES[execute_restore.name]["queue"], RESTORE_QUEUE_NAME)
        self.assertEqual(settings_route, "nexa.restores")

    def test_restore_task_uses_early_ack_and_bounded_limits(self):
        self.assertFalse(execute_restore.acks_late)
        self.assertFalse(execute_restore.reject_on_worker_lost)
        self.assertEqual(execute_restore.max_retries, 3)
        self.assertLess(execute_restore.soft_time_limit, execute_restore.time_limit)

    def test_mutation_disabled_task_refuses_execution(self):
        restore = self._restore()
        with _worker_request(execute_restore):
            with self.assertRaises(RestoreMutationError):
                execute_restore.run(
                    str(restore.public_id),
                    str(self.business_a.public_id),
                )
        restore.refresh_from_db()
        self.assertEqual(restore.status, RestoreStatus.FAILED)

    def test_missing_broker_refuses_execution(self):
        restore = self._restore()
        with (
            override_settings(
                BACKUP_RESTORE_MUTATION_ENABLED=True,
                CELERY_BROKER_URL="",
                CELERY_TASK_ALWAYS_EAGER=False,
            ),
            _worker_request(execute_restore),
        ):
            with self.assertRaises(RestoreMutationError):
                execute_restore.run(str(restore.public_id), str(self.business_a.public_id))

    def test_eager_mode_refuses_execution(self):
        restore = self._restore()
        with (
            override_settings(
                BACKUP_RESTORE_MUTATION_ENABLED=True,
                CELERY_BROKER_URL="redis://broker.example/1",
                CELERY_TASK_ALWAYS_EAGER=True,
            ),
            _worker_request(execute_restore),
        ):
            with self.assertRaises(RestoreMutationError):
                execute_restore.run(str(restore.public_id), str(self.business_a.public_id))

    def test_wrong_worker_queue_refuses_without_changing_restore(self):
        restore = self._restore()
        with self._safe_settings(), _worker_request(execute_restore, queue="nexa.backups"):
            with self.assertRaises(ImproperlyConfigured):
                execute_restore.run(str(restore.public_id), str(self.business_a.public_id))
        restore.refresh_from_db()
        self.assertEqual(restore.status, RestoreStatus.QUEUED)

    def test_owner_final_confirmation_never_executes_inline(self):
        restore = self._restore()
        self._owner_preflight_session(restore)
        with (
            mock.patch.object(
                owner_services,
                "restore_mutation_capability",
                return_value=OwnerActionCapability(True, "Available"),
            ),
            mock.patch.object(owner_services, "_enqueue_restore"),
            mock.patch("apps.backups.engine.restore_mutation.execute_restore") as mutation,
        ):
            response = self.client.post(
                reverse("backups:restore", args=[restore.source_backup.public_id]),
                {"acknowledge_replacement": "on", "confirmation": "RESTORE"},
            )
        self.assertEqual(response.status_code, 302)
        mutation.assert_not_called()

    def test_platform_final_confirmation_never_executes_inline(self):
        self.client.force_login(self.platform_admin)
        restore = self._restore(actor=self.platform_admin)
        self._platform_preflight_session(restore)
        with (
            mock.patch.object(
                platform_services,
                "restore_mutation_capability",
                return_value=PlatformActionCapability(True, "Available"),
            ),
            mock.patch.object(platform_services, "_enqueue_restore"),
            mock.patch("apps.backups.engine.restore_mutation.execute_restore") as mutation,
        ):
            response = self.client.post(
                reverse(
                    "platformadmin:backup_restore",
                    args=[self.business_a.public_id, restore.source_backup.public_id],
                ),
                {"acknowledge_replacement": "on", "confirmation": "RESTORE"},
            )
        self.assertEqual(response.status_code, 302)
        mutation.assert_not_called()

    def test_safe_owner_confirmation_enqueues_only_public_ids(self):
        restore = self._restore()
        self._owner_preflight_session(restore)
        with (
            mock.patch.object(
                owner_services,
                "restore_mutation_capability",
                return_value=OwnerActionCapability(True, "Available"),
            ),
            mock.patch.object(owner_services, "_enqueue_restore") as enqueue,
        ):
            response = self.client.post(
                reverse("backups:restore", args=[restore.source_backup.public_id]),
                {"acknowledge_replacement": "on", "confirmation": "RESTORE"},
            )
        self.assertRedirects(
            response,
            reverse("backups:restore_status", args=[restore.public_id]),
        )
        enqueue.assert_called_once_with(
            restore_public_id=restore.public_id,
            business_public_id=self.business_a.public_id,
        )

    def test_queued_restore_can_be_claimed_once(self):
        restore = self._restore()
        claim = claim_restore_operation(
            restore_public_id=restore.public_id,
            business_public_id=self.business_a.public_id,
        )
        self.assertEqual(claim.state, RestoreClaimState.CLAIMED)
        self.assertEqual(claim.restore.status, RestoreStatus.AUTHORIZING)

    def test_duplicate_worker_cannot_claim_same_restore(self):
        restore = self._restore()
        claim_restore_operation(
            restore_public_id=restore.public_id,
            business_public_id=self.business_a.public_id,
        )
        duplicate = claim_restore_operation(
            restore_public_id=restore.public_id,
            business_public_id=self.business_a.public_id,
        )
        self.assertEqual(duplicate.state, RestoreClaimState.ACTIVE_OR_AMBIGUOUS)

    def test_already_successful_restore_is_not_rerun(self):
        restore = self._restore(status=RestoreStatus.SUCCEEDED)
        with _worker_request(execute_restore):
            result = execute_restore.run(
                str(restore.public_id),
                str(self.business_a.public_id),
            )
        self.assertEqual(result["status"], RestoreStatus.SUCCEEDED)

    def test_recovery_required_is_not_retried(self):
        restore = self._restore(status=RestoreStatus.INDETERMINATE)
        with (
            mock.patch.object(execute_restore, "retry") as retry,
            _worker_request(execute_restore),
        ):
            with self.assertRaises(RestoreRecoveryRequired):
                execute_restore.run(str(restore.public_id), str(self.business_a.public_id))
        retry.assert_not_called()

    def test_ambiguous_mutation_state_is_not_replayed(self):
        restore = self._restore(status=RestoreStatus.RESTORING)
        with _worker_request(execute_restore):
            with self.assertRaises(RestoreMutationError) as raised:
                execute_restore.run(str(restore.public_id), str(self.business_a.public_id))
        self.assertEqual(raised.exception.issue_code, "restore_replay_blocked")

    def test_failed_before_mutation_follows_explicit_retry_policy(self):
        restore = self._restore(status=RestoreStatus.FAILED)
        RestoreOperation.objects.filter(pk=restore.pk).update(
            failure_code="pre_mutation_restore_lock_unavailable"
        )
        claim = claim_restore_operation(
            restore_public_id=restore.public_id,
            business_public_id=self.business_a.public_id,
        )
        self.assertEqual(claim.state, RestoreClaimState.CLAIMED)

    def test_failed_rolled_back_retry_is_forbidden(self):
        restore = self._restore(status=RestoreStatus.ROLLED_BACK)
        claim = claim_restore_operation(
            restore_public_id=restore.public_id,
            business_public_id=self.business_a.public_id,
        )
        self.assertEqual(claim.state, RestoreClaimState.RETRY_FORBIDDEN)

    def test_lock_unavailable_retry_is_bounded(self):
        restore = self._restore()
        with (
            self._safe_settings(),
            self._mock_runtime(restore, preflight_error=RestoreLockUnavailable()),
            mock.patch.object(execute_restore, "retry", side_effect=Retry()) as retry,
            _worker_request(execute_restore, retries=0),
        ):
            with self.assertRaises(Retry):
                execute_restore.run(str(restore.public_id), str(self.business_a.public_id))
        retry.assert_called_once()

    def test_nonretryable_restore_error_does_not_retry(self):
        restore = self._restore()
        with (
            self._safe_settings(),
            self._mock_runtime(
                restore,
                preflight_error=RestoreCompatibilityError(
                    issue_code="restore_compatibility_changed"
                ),
            ),
            mock.patch.object(execute_restore, "retry") as retry,
            _worker_request(execute_restore),
        ):
            with self.assertRaises(RestoreCompatibilityError):
                execute_restore.run(str(restore.public_id), str(self.business_a.public_id))
        retry.assert_not_called()

    def test_max_retry_count_is_enforced(self):
        restore = self._restore(status=RestoreStatus.FAILED)
        RestoreOperation.objects.filter(pk=restore.pk).update(
            failure_code="pre_mutation_restore_lock_unavailable"
        )
        with (
            self._safe_settings(),
            self._mock_runtime(restore, preflight_error=RestoreLockUnavailable()),
            mock.patch.object(execute_restore, "retry") as retry,
            _worker_request(execute_restore, retries=3),
        ):
            with self.assertRaises(RestoreLockUnavailable):
                execute_restore.run(str(restore.public_id), str(self.business_a.public_id))
        retry.assert_not_called()
        restore.refresh_from_db()
        self.assertEqual(restore.failure_code, "pre_mutation_task_retry_exhausted")

    def test_worker_loss_after_preflight_remains_blocked(self):
        restore = self._restore(status=RestoreStatus.AUTHORIZING)
        claim = claim_restore_operation(
            restore_public_id=restore.public_id,
            business_public_id=self.business_a.public_id,
        )
        self.assertEqual(claim.state, RestoreClaimState.ACTIVE_OR_AMBIGUOUS)

    def test_worker_loss_after_safety_backup_remains_blocked(self):
        restore = self._restore(status=RestoreStatus.VALIDATING)
        claim = claim_restore_operation(
            restore_public_id=restore.public_id,
            business_public_id=self.business_a.public_id,
        )
        self.assertEqual(claim.state, RestoreClaimState.ACTIVE_OR_AMBIGUOUS)

    def test_worker_loss_after_mutation_cannot_blind_replay(self):
        restore = self._restore(status=RestoreStatus.VERIFYING)
        claim = claim_restore_operation(
            restore_public_id=restore.public_id,
            business_public_id=self.business_a.public_id,
        )
        self.assertEqual(claim.state, RestoreClaimState.ACTIVE_OR_AMBIGUOUS)

    def test_task_persists_sanitized_failure_and_timestamp(self):
        restore = self._restore()
        with (
            self._safe_settings(),
            mock.patch(
                "apps.backups.engine.restore_mutation.build_restore_runtime_stack",
                side_effect=RuntimeError("raw-provider-secret"),
            ),
            _worker_request(execute_restore),
        ):
            with self.assertRaises(RestoreMutationError):
                execute_restore.run(str(restore.public_id), str(self.business_a.public_id))
        restore.refresh_from_db()
        self.assertEqual(restore.status, RestoreStatus.FAILED)
        self.assertIsNotNone(restore.completed_at)
        self.assertNotIn("raw-provider-secret", restore.sanitized_failure_summary)

    def test_raw_exception_text_is_absent_from_activity(self):
        restore = self._restore()
        with (
            self._safe_settings(),
            mock.patch(
                "apps.backups.engine.restore_mutation.build_restore_runtime_stack",
                side_effect=RuntimeError("raw-provider-secret"),
            ),
            _worker_request(execute_restore),
        ):
            with self.assertRaises(RestoreMutationError):
                execute_restore.run(str(restore.public_id), str(self.business_a.public_id))
        rendered = " ".join(
            BackupActivity.objects.filter(restore=restore).values_list(
                "sanitized_message", flat=True
            )
        )
        self.assertNotIn("raw-provider-secret", rendered)

    def test_safety_backup_link_and_status_are_preserved(self):
        restore = self._restore()
        safety = self._backup(
            trigger=BackupTrigger.PRE_RESTORE_SAFETY,
            protected=True,
            retention_eligible=False,
            parent_restore_operation=restore,
        )
        restore.safety_backup = safety
        restore.status = RestoreStatus.VALIDATING
        restore.save(update_fields=["safety_backup", "status", "updated_at"])
        response = self.client.get(reverse("backups:restore_status", args=[restore.public_id]))
        self.assertContains(response, "Safety backup created")

    def test_owner_restore_status_displays_progress_safely(self):
        restore = self._restore(status=RestoreStatus.SAFETY_BACKUP)
        response = self.client.get(reverse("backups:restore_status", args=[restore.public_id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Creating safety backup")
        self.assertNotContains(response, restore.source_backup.opaque_object_key)

    def test_platform_restore_status_displays_progress_safely(self):
        self.client.force_login(self.platform_admin)
        restore = self._restore(actor=self.platform_admin, status=RestoreStatus.VERIFYING)
        response = self.client.get(
            reverse(
                "platformadmin:backup_restore_status",
                args=[self.business_a.public_id, restore.public_id],
            )
        )
        self.assertContains(response, "Verifying restored data")
        self.assertNotContains(response, restore.source_backup.opaque_object_key)

    def test_cross_tenant_owner_status_access_is_rejected(self):
        restore = self._restore(business=self.business_b)
        response = self.client.get(reverse("backups:restore_status", args=[restore.public_id]))
        self.assertEqual(response.status_code, 404)

    def test_wrong_restore_business_binding_is_rejected(self):
        restore = self._restore()
        with _worker_request(execute_restore):
            with self.assertRaises(RestoreTenantMismatch):
                execute_restore.run(
                    str(restore.public_id),
                    str(self.business_b.public_id),
                )

    def test_restore_activity_events_are_safe_and_complete(self):
        required = {
            events.RESTORE_QUEUED,
            events.RESTORE_WORKER_STARTED,
            events.RESTORE_PREFLIGHT_VALIDATED,
            events.RESTORE_SAFETY_BACKUP_COMPLETED,
            events.RESTORE_MUTATION_STARTED,
            events.RESTORE_MEDIA_COMPLETED,
            events.RESTORE_POST_VERIFICATION_COMPLETED,
            events.RESTORE_COMPLETED,
            events.RESTORE_FAILED,
            events.RESTORE_RECOVERY_REQUIRED,
        }
        self.assertEqual(len(required), 10)
        self.assertTrue(all(value.startswith("restore.") for value in required))

    def test_restore_execution_available_is_false_by_default(self):
        self.assertFalse(availability.restore_execution_available())

    def test_enabled_mutation_with_unsafe_async_config_fails_checks(self):
        with override_settings(
            BACKUP_RESTORE_MUTATION_ENABLED=True,
            CELERY_BROKER_URL="",
            CELERY_TASK_ALWAYS_EAGER=True,
            CELERY_TASK_ROUTES={},
        ):
            error_ids = {
                error.id for error in check_restore_async_execution_configuration(None)
            }
        self.assertTrue({"backups.E041", "backups.E042", "backups.E044"} <= error_ids)

    def test_safe_isolated_config_produces_restore_capability(self):
        with (
            self._safe_settings(),
            mock.patch.object(
                availability,
                "restore_runtime_configuration_ready",
                return_value=True,
            ),
        ):
            self.assertTrue(availability.restore_execution_available())
            self.assertTrue(assert_safe_restore_async_execution_configuration())

    def test_no_http_request_invokes_phase3b_mutation(self):
        for relative in ("apps/backups/views.py", "apps/backups/platform_views.py"):
            source = Path(relative).read_text(encoding="utf-8")
            self.assertNotIn("from .engine.restore_mutation import", source)
            self.assertNotIn("RestoreExecutionCoordinator", source)

    def test_no_signal_or_management_command_invokes_restore_mutation(self):
        for root in (Path("apps/backups"), Path("apps")):
            for path in root.glob("**/management/commands/*.py"):
                self.assertNotIn(
                    "RestoreExecutionCoordinator",
                    path.read_text(encoding="utf-8"),
                )
        self.assertFalse(Path("apps/backups/signals.py").exists())

    def test_support_session_does_not_add_platform_restore_override(self):
        source = inspect.getsource(owner_services.request_restore)
        self.assertNotIn("support_admin", source)
        self.assertNotIn("platform", source.lower())

    def test_stale_reconciliation_is_classification_only(self):
        restore = self._restore(status=RestoreStatus.RESTORING)
        old = timezone.now() - timedelta(days=1)
        RestoreOperation.objects.filter(pk=restore.pk).update(updated_at=old)
        before = RestoreOperation.objects.get(pk=restore.pk).status
        rows = reconcile_stale_restore_operations(
            stale_before=timezone.now() - timedelta(hours=1)
        )
        after = RestoreOperation.objects.get(pk=restore.pk).status
        self.assertEqual(before, after)
        self.assertEqual(rows[0].category, StaleRestoreCategory.AMBIGUOUS_MUTATION)

    def test_queue_event_prevents_duplicate_enqueue(self):
        restore = self._restore()
        with (
            mock.patch.object(
                owner_services,
                "restore_mutation_capability",
                return_value=OwnerActionCapability(True, "Available"),
            ),
            mock.patch.object(owner_services, "_enqueue_restore") as enqueue,
        ):
            owner_services.request_restore(
                business=self.business_a,
                backup=restore.source_backup,
                restore=restore,
                actor=self.owner_a,
            )
            owner_services.request_restore(
                business=self.business_a,
                backup=restore.source_backup,
                restore=restore,
                actor=self.owner_a,
            )
        enqueue.assert_called_once()
        self.assertEqual(
            BackupActivity.objects.filter(
                restore=restore,
                event_type=events.RESTORE_QUEUED,
            ).count(),
            1,
        )

    def test_safe_worker_execution_emits_worker_started(self):
        restore = self._restore()
        with self._safe_settings(), self._mock_runtime(restore), _worker_request(execute_restore):
            result = execute_restore.run(
                str(restore.public_id),
                str(self.business_a.public_id),
            )
        self.assertEqual(result["status"], RestoreStatus.SUCCEEDED)
        self.assertTrue(
            BackupActivity.objects.filter(
                restore=restore,
                event_type=events.RESTORE_WORKER_STARTED,
            ).exists()
        )


def load_tests(loader, standard_tests, pattern):
    del standard_tests, pattern
    return loader.loadTestsFromTestCase(BackupPhase3ERestoreWorkerTests)
