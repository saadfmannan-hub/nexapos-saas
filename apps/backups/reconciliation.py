"""Non-destructive dispatch recovery and stale-operation classification."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from django.conf import settings
from django.utils import timezone

from .dispatch import (
    dispatch_backup,
    dispatch_restore,
    lock_backup_for_redispatch,
    lock_restore_for_redispatch,
)
from .enums import BackupStatus
from .models import BackupActivity, BackupRecord, TenantOperationLock


class StaleBackupCategory(StrEnum):
    STALE_QUEUED = "STALE_QUEUED"
    STALE_PRE_DURABLE = "STALE_PRE_DURABLE"
    AMBIGUOUS_PROVIDER_STAGE = "AMBIGUOUS_PROVIDER_STAGE"
    DURABLE_OBJECT_VERIFIED_PENDING_DB = "DURABLE_OBJECT_VERIFIED_PENDING_DB"
    TERMINAL = "TERMINAL"


@dataclass(frozen=True, slots=True)
class StaleBackupClassification:
    backup_public_id: uuid.UUID
    business_public_id: uuid.UUID
    category: StaleBackupCategory
    status: str
    last_updated_at: datetime
    active_tenant_lease: bool


@dataclass(frozen=True, slots=True)
class DispatchReconciliationResult:
    examined_count: int
    eligible_count: int
    confirmed_count: int
    failed_count: int

    def as_dict(self):
        return {
            "examined_count": self.examined_count,
            "eligible_count": self.eligible_count,
            "confirmed_count": self.confirmed_count,
            "failed_count": self.failed_count,
        }


_BACKUP_PRE_DURABLE = frozenset(
    {BackupStatus.PREPARING, BackupStatus.SNAPSHOTTING, BackupStatus.PACKAGING}
)
_BACKUP_TERMINAL = frozenset(
    {
        BackupStatus.SUCCEEDED,
        BackupStatus.FAILED,
        BackupStatus.CANCELLED,
        BackupStatus.DELETION_PENDING,
        BackupStatus.DELETED,
    }
)


def _aware(value, *, label):
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise ValueError(f"{label} must be an aware datetime.")
    return value


def _dispatch_cutoff(*, now, eligible_before):
    if eligible_before is not None:
        return _aware(eligible_before, label="eligible_before")
    delay = getattr(settings, "BACKUP_DISPATCH_RECONCILE_AFTER_SECONDS", 300)
    if type(delay) is not int or not 60 <= delay <= 86_400:
        delay = 300
    return now - timedelta(seconds=delay)


def _candidate_ids(*, event_type, relation, cutoff, limit):
    if type(limit) is not int or not 1 <= limit <= 1_000:
        raise ValueError("limit must be between 1 and 1000.")
    relation_id = f"{relation}_id"
    return list(
        BackupActivity.objects.filter(event_type=event_type, created_at__lte=cutoff)
        .exclude(**{f"{relation_id}__isnull": True})
        .order_by("created_at", "pk")
        .values_list(relation_id, flat=True)
        .distinct()[:limit]
    )


def reconcile_queued_backup_dispatches(
    *, publisher, now=None, eligible_before=None, limit=100
):
    """Republish only unconfirmed Phase 3H backup queue intents."""

    current_time = _aware(now or timezone.now(), label="now")
    cutoff = _dispatch_cutoff(now=current_time, eligible_before=eligible_before)
    from .engine.events import BACKUP_DISPATCH_REQUESTED

    candidate_ids = _candidate_ids(
        event_type=BACKUP_DISPATCH_REQUESTED,
        relation="backup",
        cutoff=cutoff,
        limit=limit,
    )
    eligible = confirmed = failed = 0
    for backup_id in candidate_ids:
        backup = lock_backup_for_redispatch(backup_id)
        if backup is None:
            continue
        eligible += 1
        outcome = dispatch_backup(backup=backup, publisher=publisher, redispatch=True)
        confirmed += int(outcome.confirmed)
        failed += int(not outcome.confirmed)
    return DispatchReconciliationResult(len(candidate_ids), eligible, confirmed, failed)


def reconcile_queued_restore_dispatches(
    *, publisher, now=None, eligible_before=None, limit=100
):
    """Republish exact QUEUED restores with no claim or mutation evidence."""

    current_time = _aware(now or timezone.now(), label="now")
    cutoff = _dispatch_cutoff(now=current_time, eligible_before=eligible_before)
    from .engine.events import RESTORE_QUEUED

    candidate_ids = _candidate_ids(
        event_type=RESTORE_QUEUED,
        relation="restore",
        cutoff=cutoff,
        limit=limit,
    )
    eligible = confirmed = failed = 0
    for restore_id in candidate_ids:
        restore = lock_restore_for_redispatch(restore_id)
        if restore is None:
            continue
        eligible += 1
        outcome = dispatch_restore(restore=restore, publisher=publisher, redispatch=True)
        confirmed += int(outcome.confirmed)
        failed += int(not outcome.confirmed)
    return DispatchReconciliationResult(len(candidate_ids), eligible, confirmed, failed)


def classify_backup_operation(backup, *, now=None):
    """Classify one row conservatively without changing it or provider state."""

    if type(backup) is not BackupRecord:
        raise TypeError("backup must be a BackupRecord")
    current_time = _aware(now or timezone.now(), label="now")
    if backup.status == BackupStatus.QUEUED:
        category = StaleBackupCategory.STALE_QUEUED
    elif backup.status in _BACKUP_PRE_DURABLE:
        category = StaleBackupCategory.STALE_PRE_DURABLE
    elif backup.status == BackupStatus.UPLOADING:
        category = StaleBackupCategory.AMBIGUOUS_PROVIDER_STAGE
    elif backup.status == BackupStatus.VERIFYING:
        if (
            backup.storage_backend_identifier
            and backup.opaque_object_key
            and backup.whole_artifact_hash
        ):
            category = StaleBackupCategory.DURABLE_OBJECT_VERIFIED_PENDING_DB
        else:
            category = StaleBackupCategory.AMBIGUOUS_PROVIDER_STAGE
    elif backup.status in _BACKUP_TERMINAL:
        category = StaleBackupCategory.TERMINAL
    else:
        category = StaleBackupCategory.AMBIGUOUS_PROVIDER_STAGE
    return StaleBackupClassification(
        backup_public_id=backup.public_id,
        business_public_id=backup.business.public_id,
        category=category,
        status=backup.status,
        last_updated_at=backup.updated_at,
        active_tenant_lease=TenantOperationLock.objects.filter(
            business=backup.business,
            operation_public_id=backup.public_id,
            active=True,
            lease_expires_at__gt=current_time,
        ).exists(),
    )


def reconcile_stale_backup_operations(*, stale_before=None, now=None):
    """Return classifications only; never reset rows, locks, or artifacts."""

    current_time = _aware(now or timezone.now(), label="now")
    if stale_before is None:
        threshold = getattr(settings, "BACKUP_STALE_OPERATION_SECONDS", 21_600)
        if type(threshold) is not int or not 300 <= threshold <= 604_800:
            threshold = 21_600
        stale_before = current_time - timedelta(seconds=threshold)
    cutoff = _aware(stale_before, label="stale_before")
    candidates = BackupRecord.objects.filter(updated_at__lte=cutoff).select_related("business")
    return tuple(
        classification
        for row in candidates.iterator()
        if (
            classification := classify_backup_operation(row, now=current_time)
        ).category
        != StaleBackupCategory.TERMINAL
    )


__all__ = [
    "DispatchReconciliationResult",
    "StaleBackupCategory",
    "StaleBackupClassification",
    "classify_backup_operation",
    "reconcile_queued_backup_dispatches",
    "reconcile_queued_restore_dispatches",
    "reconcile_stale_backup_operations",
]
