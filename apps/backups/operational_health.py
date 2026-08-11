"""Database-derived backup control-plane health with no broker/provider calls."""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.db.models import Max, Min
from django.utils import timezone

from .enums import BackupStatus, BackupTrigger, RestoreStatus
from .models import BackupRecord, BackupSchedule, RestoreOperation, TenantOperationLock
from .reconciliation import reconcile_stale_backup_operations
from .restore_execution import reconcile_stale_restore_operations


@dataclass(frozen=True, slots=True)
class OperationsHealthSnapshot:
    generated_at: object
    queued_backups: int
    oldest_queued_backup_age_seconds: int
    active_backups: int
    failed_backups: int
    stale_backups: int
    queued_restores: int
    oldest_queued_restore_age_seconds: int
    stale_restores: int
    recovery_required_restores: int
    last_scheduler_claim: object | None
    last_successful_scheduled_backup: object | None
    leases: tuple[dict, ...]
    alerts: tuple[str, ...]


_ACTIVE_BACKUP = (
    BackupStatus.PREPARING,
    BackupStatus.SNAPSHOTTING,
    BackupStatus.PACKAGING,
    BackupStatus.UPLOADING,
    BackupStatus.VERIFYING,
)


def _age_seconds(now, value):
    if value is None:
        return 0
    return max(0, int((now - value).total_seconds()))


def _threshold(name, default, *, minimum=0):
    value = getattr(settings, name, default)
    return value if type(value) is int and value >= minimum else default


def operations_health_snapshot(*, now=None):
    """Aggregate safe operational signals from authoritative database state."""

    current_time = now or timezone.now()
    queued_backups = BackupRecord.objects.filter(status=BackupStatus.QUEUED)
    queued_restores = RestoreOperation.objects.filter(status=RestoreStatus.QUEUED)
    oldest_backup = queued_backups.aggregate(value=Min("queued_at"))["value"]
    oldest_restore = queued_restores.aggregate(value=Min("created_at"))["value"]
    failed_backups = BackupRecord.objects.filter(status=BackupStatus.FAILED).count()
    stale_backups = reconcile_stale_backup_operations(now=current_time)
    stale_restores = reconcile_stale_restore_operations(now=current_time)
    recovery_required = RestoreOperation.objects.filter(
        status__in=(RestoreStatus.ROLLING_BACK, RestoreStatus.INDETERMINATE)
    ).count()
    last_claim = BackupSchedule.objects.aggregate(value=Max("last_claimed_run"))["value"]
    last_success = BackupRecord.objects.filter(
        trigger=BackupTrigger.SCHEDULED,
        status=BackupStatus.SUCCEEDED,
    ).aggregate(value=Max("completed_at"))["value"]

    lease_rows = []
    for lease in TenantOperationLock.objects.select_related("business").filter(
        active=True
    )[:100]:
        lease_rows.append(
            {
                "business_public_id": str(lease.business.public_id),
                "operation_public_id": str(lease.operation_public_id),
                "operation_kind": str(lease.operation_kind),
                "state": "ACTIVE"
                if lease.lease_expires_at > current_time
                else "EXPIRED",
                "age_seconds": _age_seconds(current_time, lease.acquired_at),
            }
        )

    queued_backup_age = _age_seconds(current_time, oldest_backup)
    queued_restore_age = _age_seconds(current_time, oldest_restore)
    alerts = []
    if queued_backup_age >= _threshold("BACKUP_QUEUED_AGE_WARNING_SECONDS", 900):
        alerts.append("Queued backup age exceeds its warning threshold.")
    if queued_restore_age >= _threshold(
        "BACKUP_RESTORE_QUEUED_AGE_WARNING_SECONDS", 900
    ):
        alerts.append("Queued restore age exceeds its warning threshold.")
    if failed_backups >= _threshold("BACKUP_FAILED_COUNT_WARNING", 1, minimum=1):
        alerts.append("Failed backup count meets its warning threshold.")
    if stale_backups or stale_restores:
        alerts.append("Stale backup or restore operations require review.")
    if recovery_required:
        alerts.append("RECOVERY_REQUIRED restore operations need immediate review.")

    return OperationsHealthSnapshot(
        generated_at=current_time,
        queued_backups=queued_backups.count(),
        oldest_queued_backup_age_seconds=queued_backup_age,
        active_backups=BackupRecord.objects.filter(status__in=_ACTIVE_BACKUP).count(),
        failed_backups=failed_backups,
        stale_backups=len(stale_backups),
        queued_restores=queued_restores.count(),
        oldest_queued_restore_age_seconds=queued_restore_age,
        stale_restores=len(stale_restores),
        recovery_required_restores=recovery_required,
        last_scheduler_claim=last_claim,
        last_successful_scheduled_backup=last_success,
        leases=tuple(lease_rows),
        alerts=tuple(alerts),
    )


__all__ = ["OperationsHealthSnapshot", "operations_health_snapshot"]
