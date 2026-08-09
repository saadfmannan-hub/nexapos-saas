"""Tenant-wide, sanitized read helpers for Platform Admin Backup & Restore."""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta

from django.db.models import Count, Exists, OuterRef, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.tenants.models import Business

from . import selectors, services
from .engine.retention_policy import DAILY_FULL_KEEP_COUNT
from .enums import (
    ActivitySeverity,
    BackupStatus,
    BackupTrigger,
    CompatibilityStatus,
    IntegrityStatus,
    ProductOwner,
)
from .models import BackupActivity, BackupRecord, BackupSchedule, RestoreOperation

ACTIVE_BACKUP_STATUSES = (
    BackupStatus.QUEUED,
    BackupStatus.PREPARING,
    BackupStatus.SNAPSHOTTING,
    BackupStatus.PACKAGING,
    BackupStatus.UPLOADING,
    BackupStatus.VERIFYING,
)


def _date_bounds(queryset, values):
    current_timezone = timezone.get_current_timezone()
    date_from = values.get("date_from")
    date_to = values.get("date_to")
    if date_from:
        start = timezone.make_aware(datetime.combine(date_from, time.min), current_timezone)
        queryset = queryset.filter(created_at__gte=start)
    if date_to:
        end = timezone.make_aware(
            datetime.combine(date_to + timedelta(days=1), time.min),
            current_timezone,
        )
        queryset = queryset.filter(created_at__lt=end)
    return queryset


def _currently_entitled_q(prefix=""):
    products = f"{prefix}included_products"
    plan = f"{prefix}business__subscription__plan__"
    return (
        Q(**{products: [ProductOwner.POS], f"{plan}feature_sales": True})
        | Q(**{products: [ProductOwner.WMS], f"{plan}feature_wms": True})
        | Q(
            **{
                products: [ProductOwner.POS, ProductOwner.WMS],
                f"{plan}feature_sales": True,
                f"{plan}feature_wms": True,
            }
        )
    )


def restore_ready_queryset(queryset=None):
    """Return records that pass metadata eligibility and current entitlement."""

    queryset = queryset if queryset is not None else BackupRecord.objects.all()
    return (
        queryset.filter(
            _currently_entitled_q(),
            status=BackupStatus.SUCCEEDED,
            integrity_status=IntegrityStatus.VERIFIED,
            deleted_at__isnull=True,
        )
        .exclude(storage_backend_identifier="")
        .exclude(opaque_object_key="")
        .exclude(whole_artifact_hash="")
        .exclude(
            compatibility_status__in=(
                CompatibilityStatus.REQUIRES_UPGRADE,
                CompatibilityStatus.INCOMPATIBLE,
            )
        )
    )


def platform_backup_list(filters=None):
    """Return cross-tenant backup history with only safe relational joins."""

    values = filters or {}
    queryset = BackupRecord.objects.select_related(
        "business",
        "business__subscription__plan",
        "created_by",
        "parent_restore_operation",
    ).filter(_currently_entitled_q())
    query = str(values.get("q") or "").strip()
    if query:
        query_filter = Q(business__name__icontains=query) | Q(
            created_by__email__icontains=query
        )
        try:
            query_filter |= Q(public_id=uuid.UUID(query))
        except (TypeError, ValueError):
            pass
        queryset = queryset.filter(query_filter)
    if values.get("business_name"):
        queryset = queryset.filter(business__name__icontains=values["business_name"])
    if values.get("business_uuid"):
        queryset = queryset.filter(business__public_id=values["business_uuid"])
    if values.get("backup_uuid"):
        queryset = queryset.filter(public_id=values["backup_uuid"])
    for field in ("scope", "trigger", "status", "integrity"):
        value = values.get(field)
        if value:
            model_field = "integrity_status" if field == "integrity" else field
            queryset = queryset.filter(**{model_field: value})
    readiness = values.get("restore_readiness")
    if readiness == "ready":
        queryset = restore_ready_queryset(queryset)
    elif readiness == "not_ready":
        ready_ids = restore_ready_queryset().values("pk")
        queryset = queryset.exclude(pk__in=ready_ids)
    return _date_bounds(queryset, values).order_by("-created_at")


def platform_backup_summary():
    """Return safe global KPIs without exposing tenant or storage internals."""

    records = BackupRecord.objects.all()
    aggregates = records.aggregate(
        businesses_with_backups=Count("business", distinct=True),
        successful_backups=Count("pk", filter=Q(status=BackupStatus.SUCCEEDED)),
        failed_backups=Count("pk", filter=Q(status=BackupStatus.FAILED)),
        active_backups=Count("pk", filter=Q(status__in=ACTIVE_BACKUP_STATUSES)),
    )
    durable = (
        records.filter(
            status=BackupStatus.SUCCEEDED,
            integrity_status=IntegrityStatus.VERIFIED,
            deleted_at__isnull=True,
        )
        .exclude(storage_backend_identifier="")
        .exclude(opaque_object_key="")
        .exclude(whole_artifact_hash="")
        .aggregate(total=Sum("backup_size_bytes"))["total"]
        or 0
    )
    entitled_businesses = Business.objects.filter(is_active=True).filter(
        Q(subscription__plan__feature_sales=True)
        | Q(subscription__plan__feature_wms=True)
    )
    successful_for_business = BackupRecord.objects.filter(
        business_id=OuterRef("pk"),
        status=BackupStatus.SUCCEEDED,
        integrity_status=IntegrityStatus.VERIFIED,
    )
    tenants_without_success = (
        entitled_businesses.annotate(
            has_successful_backup=Exists(successful_for_business)
        )
        .filter(has_successful_backup=False)
        .count()
    )
    return {
        **aggregates,
        "total_durable_storage": durable,
        "tenants_without_success": tenants_without_success,
    }


def get_platform_business(public_id):
    return get_object_or_404(
        Business.objects.select_related("subscription__plan", "owner"),
        public_id=public_id,
    )


def get_platform_backup(public_id, *, business=None):
    queryset = BackupRecord.objects.select_related(
        "business",
        "business__subscription__plan",
        "created_by",
        "parent_restore_operation",
    ).filter(_currently_entitled_q())
    if business is not None:
        queryset = queryset.filter(business=business)
    return get_object_or_404(queryset, public_id=public_id)


def safe_storage_label(backup):
    if not is_durable_verified(backup):
        return "Not verified"
    if backup.storage_backend_identifier == "local-private-filesystem":
        return "Private durable storage"
    return "Configured durable storage"


def is_durable_verified(backup):
    return bool(
        backup.status == BackupStatus.SUCCEEDED
        and backup.integrity_status == IntegrityStatus.VERIFIED
        and backup.deleted_at is None
        and backup.storage_backend_identifier
        and backup.opaque_object_key
        and backup.whole_artifact_hash
    )


def mark_restore_eligibility(backups):
    rows = list(backups)
    ready_ids = set(
        restore_ready_queryset(BackupRecord.objects.filter(pk__in=[row.pk for row in rows]))
        .values_list("pk", flat=True)
    )
    active_businesses = set(
        BackupRecord.objects.filter(
            business_id__in=[row.business_id for row in rows],
            status__in=ACTIVE_BACKUP_STATUSES,
        ).values_list("business_id", flat=True)
    )
    for row in rows:
        row.platform_restore_eligible = (
            row.pk in ready_ids and row.business_id not in active_businesses
        )
        row.platform_storage_label = safe_storage_label(row)
        row.platform_durable_verified = is_durable_verified(row)
    return rows


def business_backup_summary(business):
    records = BackupRecord.objects.filter(business=business).order_by("-created_at")
    latest_attempt = records.first()
    latest_success = records.filter(
        status=BackupStatus.SUCCEEDED,
        integrity_status=IntegrityStatus.VERIFIED,
    ).first()
    schedule = (
        BackupSchedule.objects.filter(business=business)
        .select_related("last_successful_backup", "last_failed_backup", "created_by")
        .first()
    )
    scopes = tuple(services.available_backup_scopes(business))
    entitled_products = tuple(services.resolve_product_entitlements(business))
    active = records.filter(status__in=ACTIVE_BACKUP_STATUSES).first()
    retention_warning_count = BackupActivity.objects.filter(
        business=business,
        event_type__icontains="retention",
        severity__in=(ActivitySeverity.WARNING, ActivitySeverity.ERROR, ActivitySeverity.CRITICAL),
    ).count()
    return {
        "business": business,
        "entitled_products": entitled_products,
        "allowed_scopes": scopes,
        "has_backup_entitlement": bool(scopes),
        "latest_attempt": latest_attempt,
        "latest_successful": latest_success,
        "active_backup": active,
        "schedule": schedule,
        "last_scheduled_success": records.filter(
            trigger=BackupTrigger.SCHEDULED,
            status=BackupStatus.SUCCEEDED,
        ).first(),
        "last_scheduled_failure": records.filter(
            trigger=BackupTrigger.SCHEDULED,
            status=BackupStatus.FAILED,
        ).first(),
        "retention_keep_count": DAILY_FULL_KEEP_COUNT,
        "retained_count": records.filter(retention_eligible=True, deleted_at__isnull=True).count(),
        "protected_count": records.filter(protected=True, deleted_at__isnull=True).count(),
        "retention_warning_count": retention_warning_count,
        "restore_available": (
            active is None and selectors.eligible_restore_backups(business).exists()
        ),
    }


def platform_backup_activity(filters=None):
    values = filters or {}
    queryset = BackupActivity.objects.select_related(
        "business", "backup", "restore", "actor"
    ).filter(
        Q(backup__isnull=True, restore__isnull=True)
        | _currently_entitled_q("backup__")
        | _currently_entitled_q("restore__source_backup__")
    )
    business = str(values.get("business") or "").strip()
    if business:
        try:
            business_public_id = uuid.UUID(business)
        except (TypeError, ValueError):
            queryset = queryset.filter(business__name__icontains=business)
        else:
            queryset = queryset.filter(business__public_id=business_public_id)
    if values.get("event"):
        queryset = queryset.filter(event_type__icontains=values["event"])
    if values.get("severity"):
        queryset = queryset.filter(severity=values["severity"])
    return _date_bounds(queryset, values).order_by("-created_at")


def platform_restore_operations(filters=None):
    values = filters or {}
    queryset = RestoreOperation.objects.select_related(
        "business", "source_backup", "safety_backup", "requested_by"
    ).filter(_currently_entitled_q("source_backup__"))
    if values.get("business_uuid"):
        queryset = queryset.filter(business__public_id=values["business_uuid"])
    if values.get("status"):
        queryset = queryset.filter(status=values["status"])
    return queryset.order_by("-created_at")
