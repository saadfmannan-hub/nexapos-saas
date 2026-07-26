"""Read-only Platform Admin views for Phase 1 backup metadata."""

import uuid
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from apps.tenants.models import Business

from .enums import (
    ActivitySeverity,
    BackupScope,
    BackupStatus,
    ProductOwner,
    RestoreStatus,
)
from .models import (
    BackupActivity,
    BackupRecord,
    RestoreOperation,
    TenantOperationLock,
)
from .platform_permissions import (
    PlatformBackupCapability,
    platform_backup_capability_required,
)


def _querystring_without_page(querydict) -> str:
    values = querydict.copy()
    values.pop("page", None)
    encoded = urlencode(values, doseq=True)
    return f"{encoded}&" if encoded else ""


def _public_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None


def _currently_entitled_backup_filter(prefix=""):
    """Build a fail-closed product-entitlement filter for backup metadata."""

    products = f"{prefix}included_products"
    plan = f"{prefix}business__subscription__plan__"
    return (
        Q(
            **{
                products: [ProductOwner.POS],
                f"{plan}feature_sales": True,
            }
        )
        | Q(
            **{
                products: [ProductOwner.WMS],
                f"{plan}feature_wms": True,
            }
        )
        | Q(
            **{
                products: [ProductOwner.POS, ProductOwner.WMS],
                f"{plan}feature_sales": True,
                f"{plan}feature_wms": True,
            }
        )
    )


@platform_backup_capability_required(PlatformBackupCapability.VIEW_METADATA)
@require_GET
def backup_list(request):
    """List sanitized backup metadata across tenants."""

    backups = BackupRecord.objects.select_related(
        "business",
        "business__subscription__plan",
        "created_by",
    ).filter(_currently_entitled_backup_filter())
    selected_business = None
    raw_business = request.GET.get("business", "").strip()
    if raw_business:
        business_public_id = _public_uuid(raw_business)
        selected_business = (
            Business.objects.filter(public_id=business_public_id).first()
            if business_public_id
            else None
        )
        backups = backups.filter(business=selected_business) if selected_business else backups.none()

    status = request.GET.get("status", "")
    if status in BackupStatus.values:
        backups = backups.filter(status=status)
    scope = request.GET.get("scope", "")
    if scope in BackupScope.values:
        backups = backups.filter(scope=scope)

    query = request.GET.get("q", "").strip()
    if query:
        public_id = _public_uuid(query)
        query_filter = Q(business__name__icontains=query) | Q(created_by__email__icontains=query)
        if public_id:
            query_filter |= Q(public_id=public_id)
        backups = backups.filter(query_filter)

    page_obj = Paginator(backups.order_by("-created_at"), 40).get_page(request.GET.get("page"))
    return render(
        request,
        "platformadmin/backups/list.html",
        {
            "pa_nav": "backups",
            "page_obj": page_obj,
            "selected_business": selected_business,
            "status_choices": BackupStatus.choices,
            "scope_choices": BackupScope.choices,
            "querystring": _querystring_without_page(request.GET),
        },
    )


@platform_backup_capability_required(PlatformBackupCapability.VIEW_METADATA)
@require_GET
def backup_detail(request, public_id):
    """Show one metadata record without exposing storage or key material."""

    backup = get_object_or_404(
        BackupRecord.objects.select_related(
            "business",
            "business__subscription__plan",
            "created_by",
            "parent_restore_operation",
        ).filter(_currently_entitled_backup_filter()),
        public_id=public_id,
    )
    activities = BackupActivity.objects.filter(backup=backup).select_related("actor")[:50]
    restore_operations = RestoreOperation.objects.filter(
        Q(source_backup=backup) | Q(safety_backup=backup)
    ).select_related("requested_by")[:25]
    return render(
        request,
        "platformadmin/backups/detail.html",
        {
            "pa_nav": "backups",
            "backup": backup,
            "components": backup.components.filter(
                product_category__in={
                    ProductOwner.SHARED,
                    *(backup.included_products or ()),
                }
            ).order_by("product_category", "component_key"),
            "activities": activities,
            "restore_operations": restore_operations,
        },
    )


@platform_backup_capability_required(PlatformBackupCapability.VIEW_METADATA)
@require_GET
def operation_list(request):
    """Show restore-operation and active-lock state without execution actions."""

    operations = RestoreOperation.objects.select_related(
        "business",
        "source_backup",
        "source_backup__business__subscription__plan",
        "safety_backup",
        "requested_by",
    ).filter(_currently_entitled_backup_filter("source_backup__"))
    status = request.GET.get("status", "")
    if status in RestoreStatus.values:
        operations = operations.filter(status=status)

    active_locks = TenantOperationLock.objects.filter(active=True).select_related("business")
    raw_business = request.GET.get("business", "").strip()
    if raw_business:
        business_public_id = _public_uuid(raw_business)
        if business_public_id:
            operations = operations.filter(business__public_id=business_public_id)
            active_locks = active_locks.filter(business__public_id=business_public_id)
        else:
            operations = operations.none()
            active_locks = active_locks.none()

    page_obj = Paginator(operations.order_by("-created_at"), 40).get_page(request.GET.get("page"))
    return render(
        request,
        "platformadmin/backups/operations.html",
        {
            "pa_nav": "backups",
            "page_obj": page_obj,
            "active_locks": active_locks,
            "status_choices": RestoreStatus.choices,
            "querystring": _querystring_without_page(request.GET),
        },
    )


@platform_backup_capability_required(PlatformBackupCapability.VIEW_METADATA)
@require_GET
def activity_list(request):
    """Show the append-only backup evidence stream across tenants."""

    activities = BackupActivity.objects.select_related(
        "business",
        "backup",
        "backup__business__subscription__plan",
        "restore",
        "restore__source_backup__business__subscription__plan",
        "actor",
    ).filter(
        Q(backup__isnull=True, restore__isnull=True)
        | _currently_entitled_backup_filter("backup__")
        | _currently_entitled_backup_filter("restore__source_backup__")
    )
    raw_business = request.GET.get("business", "").strip()
    if raw_business:
        business_public_id = _public_uuid(raw_business)
        activities = (
            activities.filter(business__public_id=business_public_id)
            if business_public_id
            else activities.none()
        )
    severity = request.GET.get("severity", "")
    if severity in ActivitySeverity.values:
        activities = activities.filter(severity=severity)

    page_obj = Paginator(activities.order_by("-created_at"), 50).get_page(request.GET.get("page"))
    return render(
        request,
        "platformadmin/backups/activity.html",
        {
            "pa_nav": "backups",
            "page_obj": page_obj,
            "severity_choices": ActivitySeverity.choices,
            "querystring": _querystring_without_page(request.GET),
        },
    )
