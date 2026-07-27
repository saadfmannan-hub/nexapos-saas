"""Safe asynchronous integration boundary for future phases.

No Celery task is registered in Phase 2A.  ``execute_backup`` is a deliberately
disabled plain function: it has no ``delay`` method, retry policy, beat entry,
or eager fallback.
"""

from django.conf import settings
from django.core.checks import Error, Tags, register
from django.core.exceptions import ImproperlyConfigured

from .engine.availability import (
    OPERATIONAL_PROVIDER_STACK_READY,
    assert_real_execution_available,
    engine_setting_enabled,
)

BACKUP_QUEUE_NAME = getattr(settings, "BACKUP_EXECUTION_QUEUE_NAME", "nexa.backups")
RESTORE_QUEUE_NAME = "nexa.restores"
VERIFICATION_QUEUE_NAME = "nexa.backup_verification"

BACKUP_EXECUTION_TASK_NAME = "apps.backups.tasks.execute_backup"
RESTORE_EXECUTION_TASK_NAME = "apps.backups.tasks.execute_restore"
SCHEDULE_DISPATCH_TASK_NAME = "apps.backups.tasks.dispatch_due_schedules"

ENGINE_ENABLED = False


def assert_safe_async_execution_configuration():
    """Fail closed before a future operational task is wired.

    Existing local settings intentionally use eager mode when Redis is absent.
    A future backup engine must call this guard and must never run in a web
    request or eager process.
    """

    if not getattr(settings, "CELERY_BROKER_URL", ""):
        raise ImproperlyConfigured(
            "Backup execution requires a dedicated Celery broker."
        )
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        raise ImproperlyConfigured(
            "Backup execution cannot run with CELERY_TASK_ALWAYS_EAGER enabled."
        )
    return True


@register(Tags.security)
def check_backup_async_execution_configuration(app_configs, **kwargs):
    """System check becomes blocking only if a future engine is enabled."""

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
    if not errors and not OPERATIONAL_PROVIDER_STACK_READY:
        errors.append(
            Error(
                "The backup engine provider stack is not operational.",
                hint=(
                    "Keep BACKUP_EXECUTION_ENGINE_ENABLED false until the "
                    "required providers are implemented and reviewed."
                ),
                id="backups.E012",
            )
        )
    return errors


def execute_backup(backup_public_id, business_public_id):
    """Fail safely if the future task entrypoint is invoked in Phase 2A.

    This is intentionally not decorated as a Celery task.  The durable record
    is marked failed when possible so a disabled invocation never remains
    ambiguous or appears successful.
    """

    from apps.backups.engine.events import ENGINE_DISABLED, EXECUTION_BLOCKED
    from apps.backups.engine.exceptions import (
        BackupEngineDisabled,
        BackupTenantMismatch,
    )
    from apps.backups.enums import ActivitySeverity, BackupStatus
    from apps.backups.models import BackupRecord
    from apps.backups.services import create_backup_activity, transition_backup
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

    try:
        assert_real_execution_available()
    except BackupEngineDisabled as exc:
        if backup.status in {
            BackupStatus.QUEUED,
            BackupStatus.PREPARING,
            BackupStatus.SNAPSHOTTING,
            BackupStatus.PACKAGING,
            BackupStatus.UPLOADING,
            BackupStatus.VERIFYING,
        }:
            backup = transition_backup(
                backup,
                BackupStatus.FAILED,
                failure_code=exc.engine_code,
                failure_summary="Backup execution was blocked; no artifact was created.",
            )
        create_backup_activity(
            business=business,
            backup=backup,
            event_type=ENGINE_DISABLED,
            severity=ActivitySeverity.WARNING,
            sanitized_message=exc.sanitized_message,
            structured_metadata={"real_execution_available": False},
        )
        create_backup_activity(
            business=business,
            backup=backup,
            event_type=EXECUTION_BLOCKED,
            severity=ActivitySeverity.WARNING,
            sanitized_message="Backup execution was blocked safely.",
            structured_metadata={"error_code": exc.engine_code},
        )
        raise

    # This remains unreachable while OPERATIONAL_PROVIDER_STACK_READY is false.
    assert_safe_async_execution_configuration()
    raise BackupEngineDisabled()
