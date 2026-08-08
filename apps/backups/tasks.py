"""Phase 2J Celery-only backup execution and lightweight schedule dispatch."""

from __future__ import annotations

import random
import uuid

from celery import shared_task
from django.conf import settings
from django.core.checks import Error, Tags, register
from django.core.exceptions import ImproperlyConfigured

from .engine.availability import (
    OPERATIONAL_PROVIDER_STACK_READY,
    assert_real_execution_available,
    engine_setting_enabled,
)

BACKUP_QUEUE_NAME = "nexa.backups"
BACKUP_SCHEDULER_QUEUE_NAME = "nexa.backup_scheduling"
RESTORE_QUEUE_NAME = "nexa.restores"
VERIFICATION_QUEUE_NAME = "nexa.backup_verification"

BACKUP_EXECUTION_TASK_NAME = "apps.backups.tasks.execute_backup"
RESTORE_EXECUTION_TASK_NAME = "apps.backups.tasks.execute_restore"
SCHEDULE_DISPATCH_TASK_NAME = "apps.backups.tasks.dispatch_due_backup_schedules"

def _configured_route(task_name):
    routes = getattr(settings, "CELERY_TASK_ROUTES", {})
    if not isinstance(routes, dict):
        return None
    route = routes.get(task_name)
    if not isinstance(route, dict):
        return None
    return route


def assert_safe_async_execution_configuration():
    """Require a broker, non-eager delivery, and the exact isolated queue."""

    if not getattr(settings, "CELERY_BROKER_URL", ""):
        raise ImproperlyConfigured(
            "Backup execution requires a dedicated Celery broker."
        )
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        raise ImproperlyConfigured(
            "Backup execution cannot run with CELERY_TASK_ALWAYS_EAGER enabled."
        )
    if getattr(settings, "BACKUP_EXECUTION_QUEUE_NAME", "") != BACKUP_QUEUE_NAME:
        raise ImproperlyConfigured("The dedicated backup queue is not configured safely.")
    route = _configured_route(BACKUP_EXECUTION_TASK_NAME)
    if route is None or route.get("queue") != BACKUP_QUEUE_NAME:
        raise ImproperlyConfigured("The backup execution task route is not isolated.")
    return True


def assert_worker_execution_context(task_request, *, expected_queue):
    """Reject direct, eager, or incorrectly routed task invocation."""

    delivery_info = getattr(task_request, "delivery_info", None)
    if (
        task_request is None
        or getattr(task_request, "called_directly", True) is not False
        or getattr(task_request, "is_eager", True) is not False
        or not isinstance(delivery_info, dict)
        or delivery_info.get("routing_key") != expected_queue
    ):
        raise ImproperlyConfigured(
            "Backup work may run only in its dedicated Celery worker queue."
        )
    return True


@register(Tags.security)
def check_backup_task_and_schedule_configuration(app_configs, **kwargs):
    """Validate bounded task limits and the fixed Beat dispatcher definition."""

    errors = []
    cadence = getattr(settings, "BACKUP_SCHEDULE_DISPATCH_INTERVAL_SECONDS", None)
    soft_limit = getattr(settings, "BACKUP_EXECUTION_TASK_SOFT_TIME_LIMIT_SECONDS", None)
    hard_limit = getattr(settings, "BACKUP_EXECUTION_TASK_TIME_LIMIT_SECONDS", None)
    if type(cadence) is not int or not 60 <= cadence <= 3_600:
        errors.append(
            Error(
                "The backup schedule dispatcher cadence is invalid.",
                hint="Use a bounded 60 to 3600 second dispatcher interval.",
                id="backups.E037",
            )
        )
    if (
        type(soft_limit) is not int
        or type(hard_limit) is not int
        or not 3_600 <= soft_limit < hard_limit <= 90_000
    ):
        errors.append(
            Error(
                "The backup worker time limits are invalid.",
                hint="Keep a bounded hard limit above the long-running soft limit.",
                id="backups.E038",
            )
        )
    beat = getattr(settings, "CELERY_BEAT_SCHEDULE", {})
    entry = beat.get("dispatch-due-backup-schedules") if isinstance(beat, dict) else None
    expected_cadence = float(cadence) if type(cadence) is int else None
    if (
        not isinstance(entry, dict)
        or entry.get("task") != SCHEDULE_DISPATCH_TASK_NAME
        or entry.get("schedule") != expected_cadence
        or not isinstance(entry.get("options"), dict)
        or entry["options"].get("queue") != BACKUP_SCHEDULER_QUEUE_NAME
    ):
        errors.append(
            Error(
                "The fixed backup Beat dispatcher entry is not configured safely.",
                id="backups.E039",
            )
        )
    return errors


@register(Tags.security)
def check_backup_async_execution_configuration(app_configs, **kwargs):
    """Block engine enablement unless every asynchronous boundary is safe."""

    if not engine_setting_enabled():
        return []
    errors = []
    if not getattr(settings, "CELERY_BROKER_URL", ""):
        errors.append(
            Error(
                "The backup engine requires a dedicated Celery broker.",
                id="backups.E010",
            )
        )
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        errors.append(
            Error(
                "The backup engine cannot run in Celery eager mode.",
                hint="Use a dedicated worker and keep backup work out of Gunicorn.",
                id="backups.E011",
            )
        )
    if not OPERATIONAL_PROVIDER_STACK_READY:
        errors.append(
            Error(
                "The backup engine provider stack is not operational.",
                hint=(
                    "The worker and scheduler boundaries exist, but restart-persistent "
                    "historical durable attestation plus production KMS and storage "
                    "safeguards remain incomplete. Keep "
                    "BACKUP_EXECUTION_ENGINE_ENABLED false."
                ),
                id="backups.E012",
            )
        )
    if getattr(settings, "BACKUP_EXECUTION_QUEUE_NAME", "") != BACKUP_QUEUE_NAME:
        errors.append(
            Error(
                "The heavy backup task must use the nexa.backups queue.",
                id="backups.E034",
            )
        )
    execution_route = _configured_route(BACKUP_EXECUTION_TASK_NAME)
    if execution_route is None or execution_route.get("queue") != BACKUP_QUEUE_NAME:
        errors.append(
            Error(
                "The heavy backup task route is not isolated.",
                id="backups.E035",
            )
        )
    dispatch_route = _configured_route(SCHEDULE_DISPATCH_TASK_NAME)
    if (
        dispatch_route is None
        or dispatch_route.get("queue") != BACKUP_SCHEDULER_QUEUE_NAME
    ):
        errors.append(
            Error(
                "The backup dispatcher task route is not configured safely.",
                id="backups.E036",
            )
        )
    return errors


def _public_uuid(value):
    if type(value) is not str:
        raise ImproperlyConfigured("Backup task identifiers must be public UUID strings.")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ImproperlyConfigured(
            "Backup task identifiers must be public UUID strings."
        ) from None
    if str(parsed) != value:
        raise ImproperlyConfigured("Backup task identifiers must use canonical UUID form.")
    return parsed


def _resolve_backup(*, backup_public_id, business_public_id):
    from apps.backups.engine.exceptions import BackupTenantMismatch
    from apps.backups.models import BackupRecord
    from apps.tenants.models import Business

    business = Business.objects.filter(public_id=business_public_id).first()
    if business is None:
        raise BackupTenantMismatch()
    backup = (
        BackupRecord.objects.for_business(business)
        .filter(public_id=backup_public_id)
        .first()
    )
    if backup is None:
        raise BackupTenantMismatch()
    return business, backup


def _mark_execution_blocked(*, business, backup, error_code, message):
    from apps.backups.engine.events import EXECUTION_BLOCKED
    from apps.backups.enums import ActivitySeverity, BackupStatus
    from apps.backups.services import create_backup_activity, transition_backup

    if backup.status == BackupStatus.QUEUED:
        backup = transition_backup(
            backup,
            BackupStatus.FAILED,
            failure_code=error_code,
            failure_summary="Backup execution was blocked; no artifact was created.",
        )
    create_backup_activity(
        business=business,
        backup=backup,
        event_type=EXECUTION_BLOCKED,
        severity=ActivitySeverity.WARNING,
        sanitized_message=message,
        structured_metadata={"error_code": error_code},
    )
    return backup


def _retry_countdown(retries):
    base = min(900, 60 * (2 ** min(max(int(retries), 0), 4)))
    return base + random.SystemRandom().randint(0, min(30, base // 2))


def _safe_execution_result(result):
    return {
        "backup_public_id": str(result.backup_public_id),
        "business_public_id": str(result.business_public_id),
        "status": str(result.final_status),
        "retention_outcome": str(result.retention_outcome),
        "retention_warning_code": result.retention_warning_code,
        "provider_stack_version": result.provider_stack_version,
    }


@shared_task(
    bind=True,
    name=BACKUP_EXECUTION_TASK_NAME,
    queue=BACKUP_QUEUE_NAME,
    max_retries=3,
    acks_late=False,
    reject_on_worker_lost=False,
    soft_time_limit=getattr(settings, "BACKUP_EXECUTION_TASK_SOFT_TIME_LIMIT_SECONDS", 21_600),
    time_limit=getattr(settings, "BACKUP_EXECUTION_TASK_TIME_LIMIT_SECONDS", 21_900),
)
def execute_backup(self, backup_public_id, business_public_id):
    """Run one durable request only in the isolated long-running worker."""

    from apps.backups.engine.exceptions import BackupEngineDisabled
    from apps.backups.engine.runtime import request_backup_execution
    from apps.backups.engine.runtime_exceptions import RuntimeEngineError
    from apps.backups.enums import BackupStatus
    from apps.backups.scheduling import record_scheduled_backup_outcome

    backup_uuid = _public_uuid(backup_public_id)
    business_uuid = _public_uuid(business_public_id)
    business, backup = _resolve_backup(
        backup_public_id=backup_uuid,
        business_public_id=business_uuid,
    )
    try:
        assert_worker_execution_context(self.request, expected_queue=BACKUP_QUEUE_NAME)
        assert_real_execution_available()
        assert_safe_async_execution_configuration()
    except BackupEngineDisabled as exc:
        backup = _mark_execution_blocked(
            business=business,
            backup=backup,
            error_code=exc.engine_code,
            message=exc.sanitized_message,
        )
        record_scheduled_backup_outcome(backup)
        raise
    except ImproperlyConfigured:
        backup = _mark_execution_blocked(
            business=business,
            backup=backup,
            error_code="async_configuration_unsafe",
            message="Backup execution was blocked by its asynchronous safety guard.",
        )
        record_scheduled_backup_outcome(backup)
        raise

    try:
        result = request_backup_execution(
            backup_public_id=backup_uuid,
            business_public_id=business_uuid,
            worker_task_identifier=str(self.request.id or "")[:255],
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except RuntimeEngineError as exc:
        backup.refresh_from_db()
        retries = int(getattr(self.request, "retries", 0) or 0)
        if exc.retryable and backup.status == BackupStatus.QUEUED and retries < self.max_retries:
            raise self.retry(
                exc=exc,
                args=(),
                kwargs={
                    "backup_public_id": str(backup_uuid),
                    "business_public_id": str(business_uuid),
                },
                countdown=_retry_countdown(retries),
                max_retries=self.max_retries,
            ) from exc
        if exc.retryable and backup.status == BackupStatus.QUEUED:
            backup = _mark_execution_blocked(
                business=business,
                backup=backup,
                error_code="task_retry_exhausted",
                message="Backup execution exhausted its bounded worker retries.",
            )
        record_scheduled_backup_outcome(backup)
        raise

    backup.refresh_from_db()
    record_scheduled_backup_outcome(backup)
    return _safe_execution_result(result)


def _enqueue_scheduled_backup(*, backup_public_id, business_public_id):
    execute_backup.apply_async(
        kwargs={
            "backup_public_id": backup_public_id,
            "business_public_id": business_public_id,
        },
        queue=BACKUP_QUEUE_NAME,
        retry=True,
        retry_policy={
            "max_retries": 3,
            "interval_start": 0,
            "interval_step": 1,
            "interval_max": 5,
        },
    )


@shared_task(
    bind=True,
    name=SCHEDULE_DISPATCH_TASK_NAME,
    queue=BACKUP_SCHEDULER_QUEUE_NAME,
    acks_late=False,
    reject_on_worker_lost=False,
    soft_time_limit=240,
    time_limit=270,
)
def dispatch_due_backup_schedules(self):
    """Find due schedules and enqueue durable identifiers, never backup work."""

    if not engine_setting_enabled():
        return {"state": "DISABLED", "examined_count": 0, "dispatched_count": 0}
    assert_worker_execution_context(
        self.request,
        expected_queue=BACKUP_SCHEDULER_QUEUE_NAME,
    )
    assert_real_execution_available()
    assert_safe_async_execution_configuration()
    from apps.backups.scheduling import dispatch_due_schedules

    return dispatch_due_schedules(enqueue=_enqueue_scheduled_backup).as_dict()


__all__ = [
    "BACKUP_EXECUTION_TASK_NAME",
    "BACKUP_QUEUE_NAME",
    "BACKUP_SCHEDULER_QUEUE_NAME",
    "SCHEDULE_DISPATCH_TASK_NAME",
    "assert_safe_async_execution_configuration",
    "assert_worker_execution_context",
    "dispatch_due_backup_schedules",
    "execute_backup",
]
