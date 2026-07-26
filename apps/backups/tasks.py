"""Celery integration names for future phases.

No Celery task is registered in Phase 1.  In particular, importing this module
cannot create a backup, dispatch a schedule, delete retention data, or restore
tenant data.
"""

from django.conf import settings
from django.core.checks import Error, Tags, register
from django.core.exceptions import ImproperlyConfigured

BACKUP_QUEUE_NAME = "nexa.backups"
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

    if not getattr(settings, "BACKUP_ENGINE_ENABLED", ENGINE_ENABLED):
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
    return errors
