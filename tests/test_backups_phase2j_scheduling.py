"""Phase 2J worker-boundary and tenant-local scheduling tests."""

from __future__ import annotations

import inspect
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from celery import Task, current_app
from celery.exceptions import Retry
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.backups import services
from apps.backups.engine.availability import (
    ASYNC_EXECUTION_BOUNDARY_READY,
    OPERATIONAL_PROVIDER_STACK_READY,
    SCHEDULE_DISPATCHER_READY,
    get_engine_capability,
    real_execution_available,
)
from apps.backups.engine.exceptions import BackupEngineDisabled
from apps.backups.engine.runtime_exceptions import (
    RuntimeLockUnavailable,
    RuntimeVerificationError,
)
from apps.backups.enums import BackupScope, BackupStatus, BackupTrigger
from apps.backups.models import BackupRecord, BackupSchedule
from apps.backups.scheduling import (
    dispatch_due_schedules,
    next_daily_occurrence,
    record_scheduled_backup_outcome,
    resolve_local_daily_occurrence,
)
from apps.backups.tasks import (
    BACKUP_EXECUTION_TASK_NAME,
    BACKUP_QUEUE_NAME,
    BACKUP_SCHEDULER_QUEUE_NAME,
    RESTORE_EXECUTION_TASK_NAME,
    RESTORE_QUEUE_NAME,
    SCHEDULE_DISPATCH_TASK_NAME,
    assert_safe_async_execution_configuration,
    check_backup_async_execution_configuration,
    check_backup_task_and_schedule_configuration,
    dispatch_due_backup_schedules,
    execute_backup,
)
from apps.subscriptions.models import Plan, Subscription
from apps.tenants.services import provision_business

SAFE_ROUTES = {
    BACKUP_EXECUTION_TASK_NAME: {"queue": BACKUP_QUEUE_NAME},
    RESTORE_EXECUTION_TASK_NAME: {"queue": RESTORE_QUEUE_NAME},
    SCHEDULE_DISPATCH_TASK_NAME: {"queue": BACKUP_SCHEDULER_QUEUE_NAME},
}


@contextmanager
def _worker_request(task, *, queue, retries=0):
    task.push_request(
        id=f"phase2j-{uuid.uuid4().hex}",
        called_directly=False,
        is_eager=False,
        retries=retries,
        delivery_info={"routing_key": queue},
    )
    try:
        yield
    finally:
        task.pop_request()


class BackupSchedulingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email=f"phase2j-{uuid.uuid4().hex}@example.test",
            password="StrongPass123!",
            full_name="Phase 2J Owner",
        )
        self.business = provision_business(owner=self.owner, name="Phase 2J Tenant")
        self.business.timezone = "Asia/Muscat"
        self.business.save(update_fields=["timezone", "updated_at"])
        self.plan = Plan.objects.create(
            name=f"Phase 2J {uuid.uuid4().hex}",
            allow_trial=False,
            feature_sales=True,
            feature_wms=False,
            is_active=True,
        )
        self.subscription = Subscription.objects.get(business=self.business)
        self.subscription.plan = self.plan
        self.subscription.status = Subscription.Status.ACTIVE
        self.subscription.trial_ends_at = None
        self.subscription.current_period_end = timezone.now() + timedelta(days=30)
        self.subscription.save(
            update_fields=[
                "plan",
                "status",
                "trial_ends_at",
                "current_period_end",
                "updated_at",
            ]
        )
        self.business._state.fields_cache.pop("subscription", None)
        self.now = datetime(2026, 8, 8, 0, 0, tzinfo=UTC)

    def _schedule(self, *, enabled=True, next_run=None, local_time=time(3, 0)):
        if next_run is None:
            next_run = datetime(2026, 8, 7, 23, 0, tzinfo=UTC)
        return BackupSchedule.objects.create(
            business=self.business,
            enabled=enabled,
            timezone_name=self.business.timezone,
            local_execution_time=local_time,
            next_run=next_run,
            scope=BackupScope.ALL_ENABLED,
            created_by=self.owner,
        )

    def _dispatch(self, *, now=None):
        enqueued = []

        def enqueue(**identifiers):
            enqueued.append(identifiers)

        with self.captureOnCommitCallbacks(execute=True):
            result = dispatch_due_schedules(
                enqueue=enqueue,
                now=now or self.now,
            )
        return result, enqueued

    def _manual_backup(self):
        return services.create_backup_request(
            business=self.business,
            scope=BackupScope.POS,
            actor=self.owner,
            idempotency_key=f"manual:{uuid.uuid4().hex}",
        )

    def test_registered_tasks_and_routes_are_isolated_and_bounded(self):
        self.assertIsInstance(execute_backup, Task)
        self.assertEqual(current_app.tasks[BACKUP_EXECUTION_TASK_NAME], execute_backup)
        self.assertEqual(
            current_app.tasks[SCHEDULE_DISPATCH_TASK_NAME],
            dispatch_due_backup_schedules,
        )
        self.assertEqual(execute_backup.name, BACKUP_EXECUTION_TASK_NAME)
        self.assertEqual(execute_backup.queue, BACKUP_QUEUE_NAME)
        self.assertFalse(execute_backup.acks_late)
        self.assertFalse(execute_backup.reject_on_worker_lost)
        self.assertEqual(execute_backup.max_retries, 3)
        self.assertEqual(settings.CELERY_TASK_ROUTES, SAFE_ROUTES)
        self.assertEqual(
            settings.CELERY_BEAT_SCHEDULE["dispatch-due-backup-schedules"]["schedule"],
            300.0,
        )

    def test_async_guard_rejects_missing_broker_eager_and_wrong_route(self):
        with override_settings(CELERY_BROKER_URL="", CELERY_TASK_ALWAYS_EAGER=False):
            with self.assertRaises(ImproperlyConfigured):
                assert_safe_async_execution_configuration()
        with override_settings(
            CELERY_BROKER_URL="redis://broker.example/1",
            CELERY_TASK_ALWAYS_EAGER=True,
        ):
            with self.assertRaises(ImproperlyConfigured):
                assert_safe_async_execution_configuration()
        with override_settings(
            CELERY_BROKER_URL="redis://broker.example/1",
            CELERY_TASK_ALWAYS_EAGER=False,
            CELERY_TASK_ROUTES={},
        ):
            with self.assertRaises(ImproperlyConfigured):
                assert_safe_async_execution_configuration()

    def test_execute_task_refuses_engine_disabled_and_direct_invocation(self):
        disabled = self._manual_backup()
        with _worker_request(execute_backup, queue=BACKUP_QUEUE_NAME):
            with self.assertRaises(BackupEngineDisabled):
                execute_backup.run(str(disabled.public_id), str(self.business.public_id))
        disabled.refresh_from_db()
        self.assertEqual(disabled.status, BackupStatus.FAILED)

        direct = self._manual_backup()
        with mock.patch(
            "apps.backups.tasks.assert_real_execution_available",
            return_value=SimpleNamespace(),
        ), override_settings(
            CELERY_BROKER_URL="redis://broker.example/1",
            CELERY_TASK_ALWAYS_EAGER=False,
        ):
            with self.assertRaises(ImproperlyConfigured):
                execute_backup.run(str(direct.public_id), str(self.business.public_id))

    def test_execute_task_invokes_runtime_only_after_all_guards(self):
        backup = self._manual_backup()
        runtime_result = SimpleNamespace(
            backup_public_id=backup.public_id,
            business_public_id=self.business.public_id,
            final_status=BackupStatus.SUCCEEDED,
            retention_outcome="NO_ACTION_REQUIRED",
            retention_warning_code="",
            provider_stack_version="test-runtime",
        )
        with (
            override_settings(
                BACKUP_EXECUTION_ENGINE_ENABLED=True,
                BACKUP_ENGINE_ENABLED=True,
                CELERY_BROKER_URL="redis://broker.example/1",
                CELERY_TASK_ALWAYS_EAGER=False,
                CELERY_TASK_ROUTES=SAFE_ROUTES,
            ),
            mock.patch(
                "apps.backups.tasks.assert_real_execution_available",
                return_value=SimpleNamespace(),
            ),
            mock.patch(
                "apps.backups.engine.runtime.request_backup_execution",
                return_value=runtime_result,
            ) as runtime,
            _worker_request(execute_backup, queue=BACKUP_QUEUE_NAME),
        ):
            result = execute_backup.run(
                str(backup.public_id),
                str(self.business.public_id),
            )
        runtime.assert_called_once()
        self.assertEqual(result["backup_public_id"], str(backup.public_id))
        self.assertNotIn("stored_object", result)

    def test_task_arguments_are_only_canonical_public_identifiers(self):
        with self.assertRaises(ImproperlyConfigured):
            execute_backup.run(uuid.uuid4(), str(self.business.public_id))
        self._schedule()
        result, enqueued = self._dispatch()
        self.assertEqual(result.dispatched_count, 1)
        self.assertEqual(
            set(enqueued[0]),
            {"backup_public_id", "business_public_id"},
        )
        for value in enqueued[0].values():
            self.assertEqual(str(uuid.UUID(value)), value)

    def test_due_schedule_creates_one_system_backup_and_is_idempotent(self):
        schedule = self._schedule()
        first, first_enqueued = self._dispatch()
        second, second_enqueued = self._dispatch()
        backup = BackupRecord.objects.get(trigger=BackupTrigger.SCHEDULED)
        schedule.refresh_from_db()
        self.assertEqual(first.dispatched_count, 1)
        self.assertEqual(second.dispatched_count, 0)
        self.assertEqual(len(first_enqueued), 1)
        self.assertEqual(second_enqueued, [])
        self.assertEqual(BackupRecord.objects.filter(trigger=BackupTrigger.SCHEDULED).count(), 1)
        self.assertTrue(backup.system_actor)
        self.assertIsNone(backup.created_by)
        self.assertEqual(backup.scope, BackupScope.ALL_ENABLED)
        self.assertEqual(backup.included_products, ["POS"])
        self.assertEqual(schedule.last_claimed_run, datetime(2026, 8, 7, 23, 0, tzinfo=UTC))
        self.assertGreater(schedule.next_run, self.now)

    def test_future_disabled_inactive_and_invalid_entitlement_do_not_dispatch(self):
        future = self._schedule(next_run=self.now + timedelta(hours=1))
        result, enqueued = self._dispatch()
        self.assertEqual(result.examined_count, 0)
        self.assertEqual(enqueued, [])
        future.delete()

        self._schedule(enabled=False)
        result, _ = self._dispatch()
        self.assertEqual(result.examined_count, 0)
        BackupSchedule.objects.all().delete()

        self._schedule()
        self.business.is_active = False
        self.business.save(update_fields=["is_active", "updated_at"])
        result, _ = self._dispatch()
        self.assertEqual(result.ineligible_count, 1)
        self.assertFalse(BackupRecord.objects.exists())
        self.business.is_active = True
        self.business.save(update_fields=["is_active", "updated_at"])

        BackupSchedule.objects.all().delete()
        self._schedule()
        self.plan.feature_sales = False
        self.plan.save(update_fields=["feature_sales", "updated_at"])
        result, _ = self._dispatch()
        self.assertEqual(result.ineligible_count, 1)
        self.assertFalse(BackupRecord.objects.exists())

    def test_missed_days_collapse_to_latest_single_catch_up(self):
        self._schedule(next_run=self.now - timedelta(days=3, hours=1))
        result, enqueued = self._dispatch()
        backup = BackupRecord.objects.get()
        self.assertEqual(result.dispatched_count, 1)
        self.assertEqual(len(enqueued), 1)
        self.assertEqual(backup.scheduled_local_date.isoformat(), "2026-08-08")
        self.assertEqual(BackupRecord.objects.count(), 1)

    def test_active_manual_backup_defers_schedule_without_queued_flood(self):
        schedule = self._schedule()
        manual = self._manual_backup()
        result, enqueued = self._dispatch()
        schedule.refresh_from_db()
        self.assertEqual(result.deferred_active_count, 1)
        self.assertEqual(enqueued, [])
        self.assertEqual(BackupRecord.objects.count(), 1)
        self.assertEqual(manual.status, BackupStatus.QUEUED)
        self.assertLessEqual(schedule.next_run, self.now)

    def test_timezone_resolution_is_muscat_and_dst_safe(self):
        muscat = resolve_local_daily_occurrence(
            local_date=datetime(2026, 8, 8).date(),
            local_time=time(3, 0),
            timezone_name="Asia/Muscat",
        )
        self.assertEqual(muscat, datetime(2026, 8, 7, 23, 0, tzinfo=UTC))
        spring_gap = resolve_local_daily_occurrence(
            local_date=datetime(2026, 3, 8).date(),
            local_time=time(2, 30),
            timezone_name="America/New_York",
        )
        self.assertEqual(
            spring_gap.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).time(),
            time(3, 0),
        )
        first_fall = resolve_local_daily_occurrence(
            local_date=datetime(2026, 11, 1).date(),
            local_time=time(1, 30),
            timezone_name="America/New_York",
        )
        self.assertEqual(first_fall, datetime(2026, 11, 1, 5, 30, tzinfo=UTC))
        self.assertGreater(
            next_daily_occurrence(
                local_time=time(3, 0),
                timezone_name="Asia/Muscat",
                after=self.now,
            ),
            self.now,
        )

    def test_schedule_outcome_links_are_tenant_scoped(self):
        schedule = self._schedule()
        self._dispatch()
        backup = BackupRecord.objects.get(trigger=BackupTrigger.SCHEDULED)
        backup.status = BackupStatus.FAILED
        backup.save(update_fields=["status", "updated_at"])
        self.assertTrue(record_scheduled_backup_outcome(backup))
        schedule.refresh_from_db()
        self.assertEqual(schedule.last_failed_backup, backup)

    def test_retryable_error_retries_only_while_queued_and_is_bounded(self):
        backup = self._manual_backup()
        safe = override_settings(
            BACKUP_EXECUTION_ENGINE_ENABLED=True,
            BACKUP_ENGINE_ENABLED=True,
            CELERY_BROKER_URL="redis://broker.example/1",
            CELERY_TASK_ALWAYS_EAGER=False,
            CELERY_TASK_ROUTES=SAFE_ROUTES,
        )
        with (
            safe,
            mock.patch(
                "apps.backups.tasks.assert_real_execution_available",
                return_value=SimpleNamespace(),
            ),
            mock.patch(
                "apps.backups.engine.runtime.request_backup_execution",
                side_effect=RuntimeLockUnavailable(),
            ),
            mock.patch.object(execute_backup, "retry", side_effect=Retry()) as retry,
            _worker_request(execute_backup, queue=BACKUP_QUEUE_NAME, retries=0),
        ):
            with self.assertRaises(Retry):
                execute_backup.run(str(backup.public_id), str(self.business.public_id))
        retry.assert_called_once()
        backup.refresh_from_db()
        self.assertEqual(backup.status, BackupStatus.QUEUED)

        with (
            override_settings(
                BACKUP_EXECUTION_ENGINE_ENABLED=True,
                BACKUP_ENGINE_ENABLED=True,
                CELERY_BROKER_URL="redis://broker.example/1",
                CELERY_TASK_ALWAYS_EAGER=False,
                CELERY_TASK_ROUTES=SAFE_ROUTES,
            ),
            mock.patch(
                "apps.backups.tasks.assert_real_execution_available",
                return_value=SimpleNamespace(),
            ),
            mock.patch(
                "apps.backups.engine.runtime.request_backup_execution",
                side_effect=RuntimeLockUnavailable(),
            ),
            _worker_request(execute_backup, queue=BACKUP_QUEUE_NAME, retries=3),
        ):
            with self.assertRaises(RuntimeLockUnavailable):
                execute_backup.run(str(backup.public_id), str(self.business.public_id))
        backup.refresh_from_db()
        self.assertEqual(backup.status, BackupStatus.FAILED)
        self.assertEqual(backup.failure_code, "task_retry_exhausted")

    def test_nonretryable_error_never_calls_retry(self):
        backup = self._manual_backup()
        with (
            override_settings(
                BACKUP_EXECUTION_ENGINE_ENABLED=True,
                BACKUP_ENGINE_ENABLED=True,
                CELERY_BROKER_URL="redis://broker.example/1",
                CELERY_TASK_ALWAYS_EAGER=False,
                CELERY_TASK_ROUTES=SAFE_ROUTES,
            ),
            mock.patch(
                "apps.backups.tasks.assert_real_execution_available",
                return_value=SimpleNamespace(),
            ),
            mock.patch(
                "apps.backups.engine.runtime.request_backup_execution",
                side_effect=RuntimeVerificationError(),
            ),
            mock.patch.object(execute_backup, "retry") as retry,
            _worker_request(execute_backup, queue=BACKUP_QUEUE_NAME),
        ):
            with self.assertRaises(RuntimeVerificationError):
                execute_backup.run(str(backup.public_id), str(self.business.public_id))
        retry.assert_not_called()

    def test_dispatcher_never_runs_pipeline_inline(self):
        self._schedule()
        batch = SimpleNamespace(as_dict=lambda: {"dispatched_count": 1})
        with (
            override_settings(
                BACKUP_EXECUTION_ENGINE_ENABLED=True,
                BACKUP_ENGINE_ENABLED=True,
                CELERY_BROKER_URL="redis://broker.example/1",
                CELERY_TASK_ALWAYS_EAGER=False,
                CELERY_TASK_ROUTES=SAFE_ROUTES,
            ),
            mock.patch(
                "apps.backups.tasks.assert_real_execution_available",
                return_value=SimpleNamespace(),
            ),
            mock.patch(
                "apps.backups.scheduling.dispatch_due_schedules",
                return_value=batch,
            ) as dispatch,
            mock.patch(
                "apps.backups.engine.runtime.request_backup_execution"
            ) as runtime,
            _worker_request(
                dispatch_due_backup_schedules,
                queue=BACKUP_SCHEDULER_QUEUE_NAME,
            ),
        ):
            result = dispatch_due_backup_schedules.run()
        self.assertEqual(result, {"dispatched_count": 1})
        dispatch.assert_called_once()
        runtime.assert_not_called()

    def test_capability_and_checks_remain_fail_closed_by_default(self):
        capability = get_engine_capability()
        self.assertTrue(ASYNC_EXECUTION_BOUNDARY_READY)
        self.assertTrue(SCHEDULE_DISPATCHER_READY)
        self.assertFalse(OPERATIONAL_PROVIDER_STACK_READY)
        self.assertFalse(real_execution_available())
        self.assertFalse(capability.setting_enabled)
        self.assertFalse(capability.real_execution_available)
        self.assertEqual(check_backup_async_execution_configuration(None), [])
        self.assertEqual(check_backup_task_and_schedule_configuration(None), [])
        with override_settings(
            BACKUP_EXECUTION_ENGINE_ENABLED=True,
            BACKUP_ENGINE_ENABLED=True,
            CELERY_BROKER_URL="",
            CELERY_TASK_ALWAYS_EAGER=True,
        ):
            self.assertEqual(
                {error.id for error in check_backup_async_execution_configuration(None)},
                {"backups.E010", "backups.E011", "backups.E012"},
            )

    def test_no_http_signal_or_scheduler_surface_runs_backup_synchronously(self):
        for relative in (
            "apps/backups/views.py",
            "apps/backups/platform_views.py",
            "apps/backups/signals.py",
            "apps/backups/admin.py",
            "apps/backups/apps.py",
        ):
            path = Path(relative)
            if not path.exists():
                continue
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("execute_backup(", source)
            self.assertNotIn("request_backup_execution", source)
        task_source = inspect.getsource(dispatch_due_backup_schedules.run)
        scheduling_source = inspect.getsource(dispatch_due_schedules)
        self.assertNotIn("BackupExecutionCoordinator", task_source)
        self.assertNotIn("request_backup_execution", task_source)
        self.assertNotIn("retention", scheduling_source.lower())
