"""Durable claim, progress, and reconciliation helpers for restore workers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.tenants.models import Business

from .enums import RestoreStatus
from .models import RestoreOperation, TenantOperationLock


class RestoreClaimState(StrEnum):
    CLAIMED = "CLAIMED"
    ALREADY_SUCCEEDED = "ALREADY_SUCCEEDED"
    ACTIVE_OR_AMBIGUOUS = "ACTIVE_OR_AMBIGUOUS"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RETRY_FORBIDDEN = "RETRY_FORBIDDEN"


@dataclass(frozen=True, slots=True)
class RestoreClaimResult:
    state: RestoreClaimState
    restore: RestoreOperation


class StaleRestoreCategory(StrEnum):
    STALE_QUEUED = "STALE_QUEUED"
    STALE_PRE_MUTATION = "STALE_PRE_MUTATION"
    RETRYABLE_PRE_MUTATION_FAILURE = "RETRYABLE_PRE_MUTATION_FAILURE"
    AMBIGUOUS_MUTATION = "AMBIGUOUS_MUTATION"


@dataclass(frozen=True, slots=True)
class StaleRestoreClassification:
    restore_public_id: uuid.UUID
    business_public_id: uuid.UUID
    category: StaleRestoreCategory
    status: str
    last_updated_at: datetime
    active_tenant_lease: bool


@dataclass(frozen=True, slots=True)
class RestoreProgress:
    label: str
    detail: str
    step_index: int
    terminal: bool
    successful: bool
    recovery_required: bool
    safety_backup_created: bool


_ACTIVE_OR_AMBIGUOUS = frozenset(
    {
        RestoreStatus.AUTHORIZING,
        RestoreStatus.LOCKING,
        RestoreStatus.SAFETY_BACKUP,
        RestoreStatus.VALIDATING,
        RestoreStatus.RESTORING,
        RestoreStatus.VERIFYING,
        RestoreStatus.ROLLING_BACK,
    }
)
_PRE_MUTATION_ACTIVE = frozenset(
    {
        RestoreStatus.AUTHORIZING,
        RestoreStatus.LOCKING,
        RestoreStatus.SAFETY_BACKUP,
        RestoreStatus.VALIDATING,
    }
)
_AMBIGUOUS_MUTATION = frozenset(
    {
        RestoreStatus.RESTORING,
        RestoreStatus.VERIFYING,
        RestoreStatus.ROLLING_BACK,
    }
)


def _uuid(value, *, label):
    if type(value) is not uuid.UUID:
        raise ValueError(f"{label} must be a UUID.")
    return value


def _claim_current(current, *, now):
    if current.status == RestoreStatus.SUCCEEDED:
        return RestoreClaimResult(RestoreClaimState.ALREADY_SUCCEEDED, current)
    if current.status == RestoreStatus.INDETERMINATE:
        return RestoreClaimResult(RestoreClaimState.RECOVERY_REQUIRED, current)
    if current.status in _ACTIVE_OR_AMBIGUOUS:
        return RestoreClaimResult(RestoreClaimState.ACTIVE_OR_AMBIGUOUS, current)
    if current.status == RestoreStatus.ROLLED_BACK:
        return RestoreClaimResult(RestoreClaimState.RETRY_FORBIDDEN, current)

    if current.status == RestoreStatus.QUEUED:
        changed = RestoreOperation.objects.filter(
            pk=current.pk,
            status=RestoreStatus.QUEUED,
        ).update(
            status=RestoreStatus.AUTHORIZING,
            started_at=current.started_at or now,
            updated_at=now,
        )
    elif (
        current.status == RestoreStatus.FAILED
        and current.failure_code.startswith("pre_mutation_")
        and not current.rollback_attempted
    ):
        changed = RestoreOperation.objects.filter(
            pk=current.pk,
            status=RestoreStatus.FAILED,
            failure_code=current.failure_code,
            rollback_attempted=False,
        ).update(
            status=RestoreStatus.AUTHORIZING,
            failure_code="",
            sanitized_failure_summary="",
            completed_at=None,
            updated_at=now,
        )
    else:
        return RestoreClaimResult(RestoreClaimState.RETRY_FORBIDDEN, current)

    current.refresh_from_db()
    if changed == 1 and current.status == RestoreStatus.AUTHORIZING:
        return RestoreClaimResult(RestoreClaimState.CLAIMED, current)
    return _claim_current(current, now=now)


@transaction.atomic
def claim_restore_operation(*, restore_public_id, business_public_id, now=None):
    """Claim one restore exactly once without making tenant-data changes."""

    restore_uuid = _uuid(restore_public_id, label="restore_public_id")
    business_uuid = _uuid(business_public_id, label="business_public_id")
    business = Business.objects.filter(public_id=business_uuid).first()
    if business is None:
        raise ValueError("The restore selection is unavailable.")
    current = (
        RestoreOperation.objects.select_for_update()
        .select_related("business", "source_backup", "safety_backup", "requested_by")
        .filter(public_id=restore_uuid, business=business)
        .first()
    )
    if (
        current is None
        or current.business.public_id != business_uuid
        or current.source_backup.business_id != business.pk
        or current.source_backup.tenant_public_id_snapshot != business_uuid
    ):
        raise ValueError("The restore selection is unavailable.")
    return _claim_current(current, now=now or timezone.now())


def restore_progress(operation):
    """Map the existing persistent status vocabulary to safe UI progress."""

    status = operation.status
    safety_created = operation.safety_backup_id is not None
    mapping = {
        RestoreStatus.QUEUED: (
            "Queued",
            "The restore request is waiting for its dedicated worker.",
            0,
        ),
        RestoreStatus.AUTHORIZING: (
            "Checking restore readiness",
            "The worker is revalidating the tenant and recovery point.",
            1,
        ),
        RestoreStatus.LOCKING: (
            "Checking restore readiness",
            "The worker is securing exclusive tenant access.",
            1,
        ),
        RestoreStatus.SAFETY_BACKUP: (
            "Creating safety backup",
            "A protected safety backup is being created before any replacement.",
            2,
        ),
        RestoreStatus.VALIDATING: (
            "Safety backup secured",
            "The safety backup is secured and final validation is in progress.",
            3,
        ),
        RestoreStatus.RESTORING: (
            "Restoring business data",
            "Tenant data and files are being restored within the guarded operation.",
            4,
        ),
        RestoreStatus.VERIFYING: (
            "Verifying restored data",
            "The restored business state is being independently verified.",
            6,
        ),
        RestoreStatus.SUCCEEDED: (
            "Completed",
            "The restore completed and the restored state was verified.",
            7,
        ),
        RestoreStatus.FAILED: (
            "Failed safely",
            "The restore stopped before an unsafe result could be accepted.",
            7,
        ),
        RestoreStatus.ROLLED_BACK: (
            "Failed safely",
            "The unsuccessful restore was rolled back and verified.",
            7,
        ),
        RestoreStatus.ROLLING_BACK: (
            "Recovery required",
            "The restore is in an ambiguous recovery state and will not replay.",
            7,
        ),
        RestoreStatus.INDETERMINATE: (
            "Recovery required",
            "Restore requires administrator recovery.",
            7,
        ),
    }
    label, detail, step_index = mapping.get(
        status,
        (
            "Recovery required",
            "Restore requires administrator recovery.",
            7,
        ),
    )
    terminal = status in {
        RestoreStatus.SUCCEEDED,
        RestoreStatus.FAILED,
        RestoreStatus.ROLLED_BACK,
        RestoreStatus.INDETERMINATE,
    }
    return RestoreProgress(
        label=label,
        detail=detail,
        step_index=step_index,
        terminal=terminal,
        successful=status == RestoreStatus.SUCCEEDED,
        recovery_required=status
        in {RestoreStatus.ROLLING_BACK, RestoreStatus.INDETERMINATE},
        safety_backup_created=safety_created,
    )


def restore_progress_steps(operation):
    progress = restore_progress(operation)
    labels = (
        "Queued",
        "Checking restore readiness",
        "Creating safety backup",
        "Safety backup secured",
        "Restoring business data",
        "Restoring files",
        "Verifying restored data",
        "Completed",
    )
    failed = operation.status in {
        RestoreStatus.FAILED,
        RestoreStatus.ROLLED_BACK,
        RestoreStatus.ROLLING_BACK,
        RestoreStatus.INDETERMINATE,
    }
    return tuple(
        {
            "label": label,
            "state": (
                "current"
                if index == progress.step_index
                else "complete"
                if index < progress.step_index and not failed
                else "pending"
            ),
        }
        for index, label in enumerate(labels)
    )


def reconcile_stale_restore_operations(*, stale_before=None, now=None):
    """Classify stale jobs without changing status or enqueuing any work."""

    current_time = now or timezone.now()
    if stale_before is None:
        lease_seconds = getattr(
            settings,
            "BACKUP_EXECUTION_LOCK_LEASE_SECONDS",
            21_600,
        )
        stale_before = current_time - timedelta(seconds=max(int(lease_seconds), 300))
    if not isinstance(stale_before, datetime) or timezone.is_naive(stale_before):
        raise ValueError("stale_before must be an aware datetime.")

    candidate_statuses = {
        RestoreStatus.QUEUED,
        *_PRE_MUTATION_ACTIVE,
        *_AMBIGUOUS_MUTATION,
        RestoreStatus.FAILED,
    }
    rows = RestoreOperation.objects.filter(
        status__in=candidate_statuses,
        updated_at__lte=stale_before,
    ).select_related("business")
    classifications = []
    for row in rows.iterator():
        if row.status == RestoreStatus.QUEUED:
            category = StaleRestoreCategory.STALE_QUEUED
        elif row.status in _PRE_MUTATION_ACTIVE:
            category = StaleRestoreCategory.STALE_PRE_MUTATION
        elif row.status in _AMBIGUOUS_MUTATION:
            category = StaleRestoreCategory.AMBIGUOUS_MUTATION
        elif row.failure_code.startswith("pre_mutation_") and not row.rollback_attempted:
            category = StaleRestoreCategory.RETRYABLE_PRE_MUTATION_FAILURE
        else:
            continue
        classifications.append(
            StaleRestoreClassification(
                restore_public_id=row.public_id,
                business_public_id=row.business.public_id,
                category=category,
                status=row.status,
                last_updated_at=row.updated_at,
                active_tenant_lease=TenantOperationLock.objects.filter(
                    business=row.business,
                    operation_public_id=row.public_id,
                    active=True,
                    lease_expires_at__gt=current_time,
                ).exists(),
            )
        )
    return tuple(classifications)


__all__ = [
    "RestoreClaimResult",
    "RestoreClaimState",
    "RestoreProgress",
    "StaleRestoreCategory",
    "StaleRestoreClassification",
    "claim_restore_operation",
    "reconcile_stale_restore_operations",
    "restore_progress",
    "restore_progress_steps",
]
