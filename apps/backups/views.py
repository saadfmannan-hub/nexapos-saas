"""Read-only Business Owner views for the Backup & Restore foundation."""

from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.core.decorators import require_permission

from . import selectors
from .engine.availability import real_execution_available
from .enums import (
    BackupStatus,
    CompatibilityStatus,
    IntegrityStatus,
    ProductOwner,
)
from .forms import BackupHistoryFilterForm, CreateBackupForm


def _can(request, permission_code: str) -> bool:
    membership = getattr(request, "membership", None)
    return bool(membership and membership.has_perm(permission_code))


def _health_for(latest_backup):
    if latest_backup is None:
        return (
            "warning",
            "Warning",
            "No verified backup metadata is available yet.",
        )
    if latest_backup.status == BackupStatus.FAILED or latest_backup.integrity_status in {
        IntegrityStatus.FAILED,
        IntegrityStatus.CORRUPTED,
    }:
        return (
            "failed",
            "Failed",
            "The latest visible backup record reports a failure.",
        )
    if (
        latest_backup.status == BackupStatus.SUCCEEDED
        and latest_backup.integrity_status == IntegrityStatus.VERIFIED
    ):
        return (
            "healthy",
            "Healthy",
            "The latest visible backup metadata is verified.",
        )
    return (
        "warning",
        "Warning",
        "The latest visible backup has not reached verified success.",
    )


def _querystring_without_page(querydict) -> str:
    values = querydict.copy()
    values.pop("page", None)
    return urlencode(values, doseq=True)


@require_permission("backups.view")
@require_GET
def dashboard(request):
    """Render an entitlement-filtered overview without starting any work."""

    visible_backups = selectors.backups_for_business(request.business)
    recent_backups = list(visible_backups[:5])
    latest_backup = recent_backups[0] if recent_backups else None
    health_state, health_label, health_message = _health_for(latest_backup)

    return render(
        request,
        "backups/dashboard.html",
        {
            "active_nav": "backups",
            "engine_enabled": real_execution_available(),
            "latest_backup": latest_backup,
            "recent_backups": recent_backups,
            "health_state": health_state,
            "health_label": health_label,
            "health_message": health_message,
            "schedule": selectors.get_schedule_for_business(request.business),
            "can_create": _can(request, "backups.create"),
            "create_form": CreateBackupForm(request.business),
        },
    )


@require_permission("backups.view")
@require_GET
def history(request):
    """List only records that remain visible under current product entitlement."""

    visible_backups = selectors.backups_for_business(request.business)
    filter_form = BackupHistoryFilterForm(
        request.business,
        request.GET or None,
    )
    if filter_form.is_bound:
        if filter_form.is_valid():
            scope = filter_form.cleaned_data["scope"]
            status = filter_form.cleaned_data["status"]
            trigger = filter_form.cleaned_data["trigger"]
            if scope:
                visible_backups = visible_backups.filter(scope=scope)
            if status:
                visible_backups = visible_backups.filter(status=status)
            if trigger:
                visible_backups = visible_backups.filter(trigger=trigger)
        else:
            # Invalid or no-longer-entitled scope parameters must not fall
            # through to an unfiltered history response.
            visible_backups = visible_backups.none()

    page_obj = Paginator(visible_backups, 30).get_page(request.GET.get("page"))
    querystring = _querystring_without_page(request.GET)
    return render(
        request,
        "backups/history.html",
        {
            "active_nav": "backups",
            "filter_form": filter_form,
            "page_obj": page_obj,
            "querystring": f"{querystring}&" if querystring else "",
        },
    )


@require_permission("backups.view")
@require_GET
def detail(request, public_id):
    """Show one business-scoped, currently entitled metadata record."""

    backup = selectors.get_backup_for_business(request.business, public_id)
    visible_component_products = {
        ProductOwner.SHARED,
        *(backup.included_products or ()),
    }
    return render(
        request,
        "backups/detail.html",
        {
            "active_nav": "backups",
            "engine_enabled": real_execution_available(),
            "backup": backup,
            "components": backup.components.filter(
                product_category__in=visible_component_products
            ).order_by("product_category", "component_key"),
            "can_restore": _can(request, "backups.restore"),
        },
    )


@require_permission("backups.view")
@require_GET
def activity(request):
    """Render the sanitized, append-only activity stream for this business."""

    activities = selectors.activities_for_business(request.business)
    page_obj = Paginator(activities, 30).get_page(request.GET.get("page"))
    querystring = _querystring_without_page(request.GET)
    return render(
        request,
        "backups/activity.html",
        {
            "active_nav": "backups",
            "page_obj": page_obj,
            "querystring": f"{querystring}&" if querystring else "",
        },
    )


@require_permission("backups.view")
@require_permission("backups.restore")
@require_GET
def restore_review(request, public_id):
    """Show non-operational readiness checks for an entitled backup."""

    backup = selectors.get_backup_for_business(request.business, public_id)
    restore_checks = (
        {
            "label": "Tenant and product scope",
            "passed": True,
            "detail": (
                "The record belongs to the active business and its products " "remain entitled."
            ),
        },
        {
            "label": "Backup lifecycle",
            "passed": backup.status == BackupStatus.SUCCEEDED,
            "detail": ("A future restore requires a successfully completed backup."),
        },
        {
            "label": "Integrity evidence",
            "passed": backup.integrity_status == IntegrityStatus.VERIFIED,
            "detail": (
                "A future restore requires deep verification, not a checksum " "placeholder."
            ),
        },
        {
            "label": "Application compatibility",
            "passed": backup.compatibility_status == CompatibilityStatus.COMPATIBLE,
            "detail": ("Compatibility metadata must be approved before execution."),
        },
        {
            "label": "Safety backup and execution engine",
            "passed": False,
            "detail": (
                "The Phase 1 engine cannot create the mandatory fresh, "
                "verified safety backup or mutate tenant data."
            ),
        },
    )
    return render(
        request,
        "backups/restore_review.html",
        {
            "active_nav": "backups",
            "engine_enabled": real_execution_available(),
            "backup": backup,
            "restore_checks": restore_checks,
        },
    )
