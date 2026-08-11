"""Durable, bounded broker handoff for backup and restore public identifiers.

The append-only ``BackupActivity`` table is the Phase 3H dispatch journal. A
broker acknowledgement is useful evidence, but database state remains the
authority and worker claims remain the idempotency boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.db import transaction

from . import services
from .engine import events
from .enums import ActivitySeverity, BackupStatus, BackupTrigger, RestoreStatus
from .models import BackupActivity, BackupRecord, RestoreOperation, TenantOperationLock

DEFAULT_IMMEDIATE_ATTEMPTS = 3
DEFAULT_TOTAL_ATTEMPTS = 12


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    confirmed: bool
    attempts: int
    redispatch: bool


def _bounded_setting(name, default, *, minimum, maximum):
    value = getattr(settings, name, default)
    if type(value) is not int or not minimum <= value <= maximum:
        return default
    return value


def _create_activity(
    *, operation, event_type, message, severity=ActivitySeverity.INFO, metadata=None
):
    if type(operation) is BackupRecord:
        return services.create_backup_activity(
            business=operation.business,
            backup=operation,
            actor=operation.created_by,
            event_type=event_type,
            severity=severity,
            sanitized_message=message,
            structured_metadata=metadata or {},
        )
    if type(operation) is RestoreOperation:
        return services.create_backup_activity(
            business=operation.business,
            backup=operation.source_backup,
            restore=operation,
            actor=operation.requested_by,
            event_type=event_type,
            severity=severity,
            sanitized_message=message,
            structured_metadata=metadata or {},
        )
    raise TypeError("Dispatch journaling requires a persisted backup or restore.")


def record_backup_dispatch_intent(backup):
    """Persist one queue-intent marker without claiming broker delivery."""

    if type(backup) is not BackupRecord or backup.status != BackupStatus.QUEUED:
        raise ValueError("Only an exact queued backup can record dispatch intent.")
    if not BackupActivity.objects.filter(
        backup=backup, event_type=events.BACKUP_DISPATCH_REQUESTED
    ).exists():
        _create_activity(
            operation=backup,
            event_type=events.BACKUP_DISPATCH_REQUESTED,
            message="Backup worker delivery was requested.",
            metadata={"journal_version": 1},
        )


def _dispatch(
    *,
    operation,
    publisher,
    attempted_event,
    confirmed_event,
    failed_event,
    redispatched_event,
    redispatch,
):
    if not callable(publisher):
        raise TypeError("publisher must be callable")
    immediate_limit = _bounded_setting(
        "BACKUP_DISPATCH_MAX_IMMEDIATE_ATTEMPTS",
        DEFAULT_IMMEDIATE_ATTEMPTS,
        minimum=1,
        maximum=3,
    )
    total_limit = _bounded_setting(
        "BACKUP_DISPATCH_MAX_TOTAL_ATTEMPTS",
        DEFAULT_TOTAL_ATTEMPTS,
        minimum=3,
        maximum=100,
    )
    relation = "backup" if type(operation) is BackupRecord else "restore"
    prior_attempts = BackupActivity.objects.filter(
        **{relation: operation}, event_type=attempted_event
    ).count()
    immediate_limit = min(immediate_limit, max(0, total_limit - prior_attempts))
    if immediate_limit == 0:
        return DispatchOutcome(False, 0, bool(redispatch))
    attempts = 0
    for attempt_number in range(1, immediate_limit + 1):
        attempts += 1
        _create_activity(
            operation=operation,
            event_type=attempted_event,
            message="Broker delivery was attempted.",
            metadata={
                "attempt_in_batch": attempt_number,
                "maximum_batch_attempts": immediate_limit,
                "redispatch": bool(redispatch),
            },
        )
        try:
            publisher(
                **(
                    {
                        "backup_public_id": operation.public_id,
                        "business_public_id": operation.business.public_id,
                    }
                    if type(operation) is BackupRecord
                    else {
                        "restore_public_id": operation.public_id,
                        "business_public_id": operation.business.public_id,
                    }
                )
            )
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception:
            continue

        _create_activity(
            operation=operation,
            event_type=confirmed_event,
            message="The broker accepted the worker message.",
            metadata={"attempt_in_batch": attempt_number, "redispatch": bool(redispatch)},
        )
        if redispatch:
            _create_activity(
                operation=operation,
                event_type=redispatched_event,
                message="An eligible queued operation was safely redispatched.",
                metadata={"attempt_in_batch": attempt_number},
            )
        return DispatchOutcome(True, attempts, bool(redispatch))

    _create_activity(
        operation=operation,
        event_type=failed_event,
        severity=ActivitySeverity.WARNING,
        message="Broker delivery could not be confirmed; the durable request remains queued.",
        metadata={"attempts": attempts, "redispatch": bool(redispatch)},
    )
    return DispatchOutcome(False, attempts, bool(redispatch))


def dispatch_backup(*, backup, publisher, redispatch=False):
    if type(backup) is not BackupRecord or backup.status != BackupStatus.QUEUED:
        return DispatchOutcome(False, 0, bool(redispatch))
    return _dispatch(
        operation=backup,
        publisher=publisher,
        attempted_event=events.BACKUP_DISPATCH_ATTEMPTED,
        confirmed_event=events.BACKUP_DISPATCH_CONFIRMED,
        failed_event=events.BACKUP_DISPATCH_FAILED,
        redispatched_event=events.BACKUP_REDISPATCHED,
        redispatch=redispatch,
    )


def dispatch_restore(*, restore, publisher, redispatch=False):
    if type(restore) is not RestoreOperation or restore.status != RestoreStatus.QUEUED:
        return DispatchOutcome(False, 0, bool(redispatch))
    return _dispatch(
        operation=restore,
        publisher=publisher,
        attempted_event=events.RESTORE_DISPATCH_ATTEMPTED,
        confirmed_event=events.RESTORE_DISPATCH_CONFIRMED,
        failed_event=events.RESTORE_DISPATCH_FAILED,
        redispatched_event=events.RESTORE_REDISPATCHED,
        redispatch=redispatch,
    )


def _attempt_budget_available(*, activity_filter):
    total_limit = _bounded_setting(
        "BACKUP_DISPATCH_MAX_TOTAL_ATTEMPTS",
        DEFAULT_TOTAL_ATTEMPTS,
        minimum=3,
        maximum=100,
    )
    return activity_filter.count() < total_limit


def backup_dispatch_eligible(backup):
    """Return whether one Phase 3H queue intent is safe to republish."""

    if (
        type(backup) is not BackupRecord
        or backup.status != BackupStatus.QUEUED
        or backup.trigger == BackupTrigger.PRE_RESTORE_SAFETY
        or backup.started_at is not None
        or backup.storage_backend_identifier
        or backup.opaque_object_key
        or backup.whole_artifact_hash
    ):
        return False
    activities = BackupActivity.objects.filter(backup=backup)
    if not activities.filter(event_type=events.BACKUP_DISPATCH_REQUESTED).exists():
        return False
    if activities.filter(
        event_type__in=(
            events.BACKUP_DISPATCH_CONFIRMED,
            events.BACKUP_EXECUTION_STARTED,
            events.BACKUP_COMPLETED,
            events.BACKUP_FAILED,
        )
    ).exists():
        return False
    if TenantOperationLock.objects.filter(
        business=backup.business,
        operation_public_id=backup.public_id,
        active=True,
    ).exists():
        return False
    return _attempt_budget_available(
        activity_filter=activities.filter(event_type=events.BACKUP_DISPATCH_ATTEMPTED)
    )


def restore_dispatch_eligible(restore):
    """Allow replay only for an untouched, explicitly queued restore intent."""

    if (
        type(restore) is not RestoreOperation
        or restore.status != RestoreStatus.QUEUED
        or restore.started_at is not None
        or restore.safety_backup_id is not None
        or restore.rollback_attempted
    ):
        return False
    activities = BackupActivity.objects.filter(restore=restore)
    if not activities.filter(event_type=events.RESTORE_QUEUED).exists():
        return False
    if activities.filter(
        event_type__in=(
            events.RESTORE_DISPATCH_CONFIRMED,
            events.RESTORE_WORKER_STARTED,
            events.RESTORE_STARTED,
            events.RESTORE_MUTATION_STARTED,
            events.RESTORE_COMPLETED,
            events.RESTORE_FAILED,
            events.RESTORE_RECOVERY_REQUIRED,
        )
    ).exists():
        return False
    if TenantOperationLock.objects.filter(
        business=restore.business,
        operation_public_id=restore.public_id,
        active=True,
    ).exists():
        return False
    return _attempt_budget_available(
        activity_filter=activities.filter(event_type=events.RESTORE_DISPATCH_ATTEMPTED)
    )


@transaction.atomic
def lock_backup_for_redispatch(backup_id):
    current = BackupRecord.objects.select_for_update().filter(pk=backup_id).first()
    return current if current is not None and backup_dispatch_eligible(current) else None


@transaction.atomic
def lock_restore_for_redispatch(restore_id):
    current = (
        RestoreOperation.objects.select_for_update()
        .select_related("business", "source_backup", "requested_by")
        .filter(pk=restore_id)
        .first()
    )
    return current if current is not None and restore_dispatch_eligible(current) else None


__all__ = [
    "DispatchOutcome",
    "backup_dispatch_eligible",
    "dispatch_backup",
    "dispatch_restore",
    "lock_backup_for_redispatch",
    "lock_restore_for_redispatch",
    "record_backup_dispatch_intent",
    "restore_dispatch_eligible",
]
