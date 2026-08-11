"""Tenant-local daily backup scheduling and durable occurrence dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.utils import timezone

from apps.subscriptions.models import Subscription

from . import dispatch as dispatching
from . import services
from .engine.events import BACKUP_SCHEDULE_DISPATCHED
from .enums import BackupScope, BackupStatus, BackupTrigger
from .models import BackupRecord, BackupSchedule

ACTIVE_BACKUP_STATUSES = (
    BackupStatus.QUEUED,
    BackupStatus.PREPARING,
    BackupStatus.SNAPSHOTTING,
    BackupStatus.PACKAGING,
    BackupStatus.UPLOADING,
    BackupStatus.VERIFYING,
)


class ScheduleDispatchError(Exception):
    """Fixed-message dispatcher failure that never embeds provider details."""

    def __init__(self):
        super().__init__("Scheduled backup dispatch failed safely.")


class ScheduleClaimState(StrEnum):
    DISPATCHED = "DISPATCHED"
    ALREADY_DISPATCHED = "ALREADY_DISPATCHED"
    DEFERRED_ACTIVE = "DEFERRED_ACTIVE"
    FUTURE = "FUTURE"
    DISABLED = "DISABLED"
    INELIGIBLE = "INELIGIBLE"
    INVALID = "INVALID"
    INITIALIZED = "INITIALIZED"


@dataclass(frozen=True, slots=True)
class ScheduleClaimResult:
    state: ScheduleClaimState
    backup_public_id: str = ""
    business_public_id: str = ""


@dataclass(frozen=True, slots=True)
class ScheduleDispatchBatchResult:
    examined_count: int
    dispatched_count: int
    already_dispatched_count: int
    deferred_active_count: int
    ineligible_count: int
    invalid_count: int
    initialized_count: int

    def as_dict(self):
        return {
            "examined_count": self.examined_count,
            "dispatched_count": self.dispatched_count,
            "already_dispatched_count": self.already_dispatched_count,
            "deferred_active_count": self.deferred_active_count,
            "ineligible_count": self.ineligible_count,
            "invalid_count": self.invalid_count,
            "initialized_count": self.initialized_count,
        }


def _normalize_aware_utc(value):
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise ScheduleDispatchError()
    return value.astimezone(UTC)


def _timezone(timezone_name):
    if type(timezone_name) is not str or not timezone_name:
        raise ScheduleDispatchError()
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ScheduleDispatchError() from None


def resolve_local_daily_occurrence(*, local_date, local_time, timezone_name):
    """Resolve a wall-clock occurrence deterministically across DST changes.

    Ambiguous times use the first chronological occurrence. A nonexistent wall
    time advances minute-by-minute to the first valid local instant.
    """

    if (
        type(local_date) is not date
        or type(local_time) is not time
        or local_time.tzinfo is not None
    ):
        raise ScheduleDispatchError()
    zone = _timezone(timezone_name)
    naive = datetime.combine(local_date, local_time)
    for minute_offset in range(181):
        candidate = naive + timedelta(minutes=minute_offset)
        valid_utc = set()
        for fold in (0, 1):
            aware = candidate.replace(tzinfo=zone, fold=fold)
            as_utc = aware.astimezone(UTC)
            round_trip = as_utc.astimezone(zone)
            if round_trip.replace(tzinfo=None) == candidate:
                valid_utc.add(as_utc)
        if valid_utc:
            return min(valid_utc)
    raise ScheduleDispatchError()


def next_daily_occurrence(*, local_time, timezone_name, after):
    """Return the first tenant-local occurrence strictly after ``after``."""

    after_utc = _normalize_aware_utc(after)
    zone = _timezone(timezone_name)
    local_date = after_utc.astimezone(zone).date()
    for day_offset in range(3):
        candidate = resolve_local_daily_occurrence(
            local_date=local_date + timedelta(days=day_offset),
            local_time=local_time,
            timezone_name=timezone_name,
        )
        if candidate > after_utc:
            return candidate
    raise ScheduleDispatchError()


def latest_due_occurrence(schedule, *, now):
    """Return at most the latest missed daily occurrence for a due schedule."""

    now_utc = _normalize_aware_utc(now)
    if schedule.next_run is None:
        return None
    next_run = _normalize_aware_utc(schedule.next_run)
    if next_run > now_utc:
        return None
    zone = _timezone(schedule.timezone_name)
    candidate_date = now_utc.astimezone(zone).date()
    candidate = resolve_local_daily_occurrence(
        local_date=candidate_date,
        local_time=schedule.local_execution_time,
        timezone_name=schedule.timezone_name,
    )
    if candidate > now_utc:
        candidate_date -= timedelta(days=1)
        candidate = resolve_local_daily_occurrence(
            local_date=candidate_date,
            local_time=schedule.local_execution_time,
            timezone_name=schedule.timezone_name,
        )
    return max(candidate, next_run)


def _schedule_is_eligible(schedule):
    business = schedule.business
    if not business.is_active or schedule.scope != BackupScope.ALL_ENABLED:
        return False
    try:
        subscription = business.subscription
    except Subscription.DoesNotExist:
        return False
    if not subscription.plan.is_active or not subscription.is_operational:
        return False
    try:
        resolution = services.resolve_requested_scope(business, BackupScope.ALL_ENABLED)
    except services.BackupServiceError:
        return False
    return bool(resolution.included_products)


def _advance_schedule(schedule, *, now, claimed_run=None):
    schedule.next_run = next_daily_occurrence(
        local_time=schedule.local_execution_time,
        timezone_name=schedule.timezone_name,
        after=now,
    )
    update_fields = ["next_run", "updated_at"]
    if claimed_run is not None:
        schedule.last_claimed_run = claimed_run
        update_fields.append("last_claimed_run")
    schedule.save(update_fields=update_fields)


@transaction.atomic
def _claim_due_schedule(*, schedule_id, now, enqueue):
    schedule = (
        BackupSchedule.objects.select_for_update()
        .select_related("business", "business__subscription__plan")
        .filter(pk=schedule_id)
        .first()
    )
    if schedule is None or not schedule.enabled:
        return ScheduleClaimResult(ScheduleClaimState.DISABLED)
    if schedule.next_run is None:
        try:
            _advance_schedule(schedule, now=now)
        except ScheduleDispatchError:
            return ScheduleClaimResult(ScheduleClaimState.INVALID)
        return ScheduleClaimResult(ScheduleClaimState.INITIALIZED)
    try:
        due = latest_due_occurrence(schedule, now=now)
    except ScheduleDispatchError:
        return ScheduleClaimResult(ScheduleClaimState.INVALID)
    if due is None:
        return ScheduleClaimResult(ScheduleClaimState.FUTURE)
    if not _schedule_is_eligible(schedule):
        try:
            _advance_schedule(schedule, now=now)
        except ScheduleDispatchError:
            return ScheduleClaimResult(ScheduleClaimState.INVALID)
        return ScheduleClaimResult(ScheduleClaimState.INELIGIBLE)
    if BackupRecord.objects.for_business(schedule.business).filter(
        status__in=ACTIVE_BACKUP_STATUSES
    ).exists():
        return ScheduleClaimResult(ScheduleClaimState.DEFERRED_ACTIVE)

    local_date = due.astimezone(_timezone(schedule.timezone_name)).date()
    idempotency_key = services.generate_idempotency_key(
        "scheduled-daily",
        schedule.business.public_id,
        local_date.isoformat(),
        due.isoformat(),
    )
    existing = BackupRecord.objects.for_business(schedule.business).filter(
        idempotency_key=idempotency_key
    ).first()
    backup = services.create_backup_request(
        business=schedule.business,
        scope=BackupScope.ALL_ENABLED,
        trigger=BackupTrigger.SCHEDULED,
        scheduled_local_date=local_date,
        idempotency_key=idempotency_key,
        system_actor=True,
    )
    _advance_schedule(schedule, now=now, claimed_run=due)
    if existing is not None:
        return ScheduleClaimResult(
            ScheduleClaimState.ALREADY_DISPATCHED,
            backup_public_id=str(backup.public_id),
            business_public_id=str(schedule.business.public_id),
        )

    dispatching.record_backup_dispatch_intent(backup)

    def publish():
        def publish_public_ids(**identifiers):
            enqueue(**{key: str(value) for key, value in identifiers.items()})

        outcome = dispatching.dispatch_backup(
            backup=backup,
            publisher=publish_public_ids,
        )
        if not outcome.confirmed:
            return
        services.create_backup_activity(
            business=schedule.business,
            backup=backup,
            event_type=BACKUP_SCHEDULE_DISPATCHED,
            sanitized_message="A due daily backup occurrence was accepted by its broker.",
            structured_metadata={
                "scheduled_local_date": local_date.isoformat(),
                "trigger": BackupTrigger.SCHEDULED,
                "system_actor": True,
            },
        )

    transaction.on_commit(publish)
    return ScheduleClaimResult(
        ScheduleClaimState.DISPATCHED,
        backup_public_id=str(backup.public_id),
        business_public_id=str(schedule.business.public_id),
    )


def dispatch_due_schedules(*, enqueue, now=None, limit=100):
    """Claim and enqueue due schedules without executing the heavy pipeline."""

    if not callable(enqueue) or type(limit) is not int or not 1 <= limit <= 1_000:
        raise ScheduleDispatchError()
    now_utc = _normalize_aware_utc(now or timezone.now())
    schedule_ids = list(
        BackupSchedule.objects.filter(enabled=True)
        .filter(next_run__lte=now_utc)
        .order_by("next_run", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    remaining = limit - len(schedule_ids)
    if remaining:
        schedule_ids.extend(
            BackupSchedule.objects.filter(enabled=True, next_run__isnull=True)
            .order_by("pk")
            .values_list("pk", flat=True)[:remaining]
        )
    results = [
        _claim_due_schedule(schedule_id=schedule_id, now=now_utc, enqueue=enqueue)
        for schedule_id in schedule_ids
    ]
    return ScheduleDispatchBatchResult(
        examined_count=len(results),
        dispatched_count=sum(
            result.state == ScheduleClaimState.DISPATCHED for result in results
        ),
        already_dispatched_count=sum(
            result.state == ScheduleClaimState.ALREADY_DISPATCHED for result in results
        ),
        deferred_active_count=sum(
            result.state == ScheduleClaimState.DEFERRED_ACTIVE for result in results
        ),
        ineligible_count=sum(
            result.state == ScheduleClaimState.INELIGIBLE for result in results
        ),
        invalid_count=sum(
            result.state == ScheduleClaimState.INVALID for result in results
        ),
        initialized_count=sum(
            result.state == ScheduleClaimState.INITIALIZED for result in results
        ),
    )


@transaction.atomic
def record_scheduled_backup_outcome(backup):
    """Update optional schedule outcome links after a terminal task result."""

    if type(backup) is not BackupRecord or backup.trigger != BackupTrigger.SCHEDULED:
        return False
    schedule = (
        BackupSchedule.objects.select_for_update()
        .filter(business_id=backup.business_id)
        .first()
    )
    if schedule is None:
        return False
    if backup.status == BackupStatus.SUCCEEDED:
        schedule.last_successful_backup = backup
        schedule.save(update_fields=["last_successful_backup", "updated_at"])
        return True
    if backup.status in {BackupStatus.FAILED, BackupStatus.CANCELLED}:
        schedule.last_failed_backup = backup
        schedule.save(update_fields=["last_failed_backup", "updated_at"])
        return True
    return False


__all__ = [
    "ACTIVE_BACKUP_STATUSES",
    "ScheduleDispatchBatchResult",
    "ScheduleDispatchError",
    "dispatch_due_schedules",
    "latest_due_occurrence",
    "next_daily_occurrence",
    "record_scheduled_backup_outcome",
    "resolve_local_daily_occurrence",
]
