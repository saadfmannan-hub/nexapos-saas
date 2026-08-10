"""Platform Admin Backup & Restore control center through Phase 3E."""

from __future__ import annotations

import uuid
from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.tenants.models import Business

from . import platform_selectors, platform_services, selectors
from .engine.availability import get_engine_capability
from .enums import ProductOwner, RestoreStatus
from .forms import CreateBackupForm, RestoreConfirmationForm, RestorePreflightForm
from .models import BackupActivity, RestoreOperation, TenantOperationLock
from .platform_forms import PlatformActivityFilterForm, PlatformBackupFilterForm
from .platform_permissions import (
    PlatformBackupCapability,
    has_platform_backup_capability,
    platform_backup_capability_required,
)
from .restore_execution import restore_progress, restore_progress_steps

PREFLIGHT_SESSION_KEY = "backups_platform_preflight"


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


def _platform_actor(request):
    return request.platform_actor


def _capability_context(request):
    actor = _platform_actor(request)
    engine = get_engine_capability()
    restore_capability = platform_services.restore_mutation_capability()
    return {
        "platform_actor": actor,
        "can_manage_backups": has_platform_backup_capability(
            actor, PlatformBackupCapability.MANAGE_BACKUPS
        ),
        "can_approve_restore": has_platform_backup_capability(
            actor, PlatformBackupCapability.APPROVE_RESTORE
        ),
        "manual_capability": platform_services.manual_backup_capability(),
        "restore_mutation_capability": restore_capability,
        "system_capabilities": {
            "backup_execution": (
                "Available" if engine.real_execution_available else "Unavailable"
            ),
            "restore_mutation": (
                "Available"
                if restore_capability.enabled
                else "Disabled"
                if not engine.restore_mutation_setting_enabled
                else "Unavailable"
            ),
            "scheduler": (
                "Configured" if engine.async_configuration_ready else "Not active"
            ),
        },
    }


def _normalized_backup_filter_data(request):
    data = request.GET.copy()
    # Retain the Phase 1 exact-business query parameter as a compatibility
    # alias while presenting clearer independent fields in Phase 3D.
    if data.get("business") and not data.get("business_uuid"):
        data["business_uuid"] = data["business"]
    return data


@platform_backup_capability_required(PlatformBackupCapability.VIEW_METADATA)
@require_GET
def backup_list(request):
    """Render tenant-wide health KPIs and paginated backup history."""

    filter_form = PlatformBackupFilterForm(_normalized_backup_filter_data(request) or None)
    if filter_form.is_bound and filter_form.is_valid():
        filter_values = {**filter_form.cleaned_data, "q": request.GET.get("q", "")}
        backups = platform_selectors.platform_backup_list(filter_values)
    elif filter_form.is_bound:
        filter_values = {}
        backups = platform_selectors.platform_backup_list().none()
    else:
        filter_values = {}
        backups = platform_selectors.platform_backup_list()

    page_obj = Paginator(backups, 40).get_page(request.GET.get("page"))
    page_obj.object_list = platform_selectors.mark_restore_eligibility(
        page_obj.object_list
    )
    selected_business = None
    if filter_values.get("business_uuid"):
        selected_business = Business.objects.filter(
            public_id=filter_values["business_uuid"]
        ).first()
    return render(
        request,
        "platformadmin/backups/list.html",
        {
            "pa_nav": "backups",
            **_capability_context(request),
            "summary": platform_selectors.platform_backup_summary(),
            "filter_form": filter_form,
            "page_obj": page_obj,
            "selected_business": selected_business,
            "querystring": _querystring_without_page(request.GET),
        },
    )


@platform_backup_capability_required(PlatformBackupCapability.VIEW_METADATA)
@require_GET
def business_overview(request, business_public_id):
    business = platform_selectors.get_platform_business(business_public_id)
    summary = platform_selectors.business_backup_summary(business)
    page_obj = Paginator(
        platform_selectors.platform_backup_list({"business_uuid": business.public_id}),
        30,
    ).get_page(request.GET.get("page"))
    page_obj.object_list = platform_selectors.mark_restore_eligibility(
        page_obj.object_list
    )
    capabilities = _capability_context(request)
    create_form = CreateBackupForm(
        business,
        enabled=(
            capabilities["can_manage_backups"]
            and capabilities["manual_capability"].enabled
            and not summary["active_backup"]
        ),
    )
    return render(
        request,
        "platformadmin/backups/business.html",
        {
            "pa_nav": "backups",
            **capabilities,
            "business": business,
            "backup_summary": summary,
            "create_form": create_form,
            "page_obj": page_obj,
            "querystring": _querystring_without_page(request.GET),
        },
    )


@platform_backup_capability_required(PlatformBackupCapability.VIEW_METADATA)
@require_GET
def backup_detail(request, public_id):
    """Show one sanitized backup record without storage/key internals."""

    backup = platform_selectors.get_platform_backup(public_id)
    platform_selectors.mark_restore_eligibility([backup])
    visible_component_products = {ProductOwner.SHARED, *(backup.included_products or ())}
    return render(
        request,
        "platformadmin/backups/detail.html",
        {
            "pa_nav": "backups",
            **_capability_context(request),
            "backup": backup,
            "business_summary": platform_selectors.business_backup_summary(
                backup.business
            ),
            "components": backup.components.filter(
                product_category__in=visible_component_products
            ).order_by("product_category", "component_key"),
            "activities": BackupActivity.objects.filter(backup=backup)
            .select_related("actor")[:50],
            "restore_operations": RestoreOperation.objects.filter(
                business=backup.business, source_backup=backup
            ).select_related("requested_by")[:25],
        },
    )


@platform_backup_capability_required(PlatformBackupCapability.MANAGE_BACKUPS)
@require_POST
def manual_backup(request, business_public_id):
    business = platform_selectors.get_platform_business(business_public_id)
    capability = platform_services.manual_backup_capability()
    if not capability.enabled:
        messages.warning(request, capability.message)
        return redirect(
            "platformadmin:backup_business", business_public_id=business.public_id
        )
    form = CreateBackupForm(business, request.POST, enabled=True)
    if not form.is_valid():
        messages.error(request, "Choose an entitled backup scope.")
        return redirect(
            "platformadmin:backup_business", business_public_id=business.public_id
        )
    try:
        backup = platform_services.platform_request_manual_backup(
            business=business,
            actor=_platform_actor(request),
            scope=form.cleaned_data["scope"],
            request=request,
        )
    except platform_services.PlatformBackupActionUnavailable as exc:
        messages.warning(request, str(exc))
        return redirect(
            "platformadmin:backup_business", business_public_id=business.public_id
        )
    messages.success(request, "The tenant backup request was queued.")
    return redirect("platformadmin:backup_detail", public_id=backup.public_id)


@platform_backup_capability_required(PlatformBackupCapability.APPROVE_RESTORE)
@require_http_methods(["GET", "POST"])
def restore_preflight(request, business_public_id, public_id):
    business = platform_selectors.get_platform_business(business_public_id)
    backup = platform_selectors.get_platform_backup(public_id, business=business)
    eligible = selectors.is_backup_restore_eligible(business, backup)
    response_status = 200
    if request.method == "POST":
        form = RestorePreflightForm(request.POST)
        if form.is_valid() and eligible:
            try:
                outcome = platform_services.platform_run_restore_preflight(
                    business=business,
                    backup=backup,
                    actor=_platform_actor(request),
                    reason=form.cleaned_data["reason"],
                    request=request,
                )
            except platform_services.PlatformBackupActionUnavailable as exc:
                messages.warning(request, str(exc))
                response_status = 409
            else:
                request.session[PREFLIGHT_SESSION_KEY] = outcome.as_session_value(
                    business_public_id=business.public_id,
                    backup_public_id=backup.public_id,
                    actor_public_id=_platform_actor(request).public_id,
                )
                return redirect(
                    "platformadmin:backup_restore",
                    business_public_id=business.public_id,
                    public_id=backup.public_id,
                )
        elif not eligible:
            messages.warning(request, "This backup is not eligible for restore.")
            response_status = 409
        else:
            response_status = 400
    else:
        form = RestorePreflightForm()
    response = render(
        request,
        "platformadmin/backups/restore_preflight.html",
        {
            "pa_nav": "backups",
            **_capability_context(request),
            "business": business,
            "backup": backup,
            "eligible": eligible,
            "form": form,
        },
    )
    response.status_code = response_status
    return response


def _session_preflight(request, business, backup):
    value = request.session.get(PREFLIGHT_SESSION_KEY)
    actor = _platform_actor(request)
    if not isinstance(value, dict):
        return None, None
    if (
        value.get("business_public_id") != str(business.public_id)
        or value.get("backup_public_id") != str(backup.public_id)
        or value.get("actor_public_id") != str(actor.public_id)
    ):
        return None, None
    restore = RestoreOperation.objects.filter(
        public_id=value.get("restore_public_id"),
        business=business,
        source_backup=backup,
        requested_by=actor,
    ).first()
    return (value, restore) if restore is not None else (None, None)


@platform_backup_capability_required(PlatformBackupCapability.APPROVE_RESTORE)
@require_http_methods(["GET", "POST"])
def restore_confirmation(request, business_public_id, public_id):
    business = platform_selectors.get_platform_business(business_public_id)
    backup = platform_selectors.get_platform_backup(public_id, business=business)
    preflight, restore = _session_preflight(request, business, backup)
    if preflight is None:
        messages.warning(request, "Run restore preflight before continuing.")
        return redirect(
            "platformadmin:backup_preflight",
            business_public_id=business.public_id,
            public_id=backup.public_id,
        )

    mutation_capability = platform_services.restore_mutation_capability()
    form = RestoreConfirmationForm(request.POST if request.method == "POST" else None)
    response_status = 200
    if request.method == "POST":
        if not preflight.get("ready"):
            messages.warning(request, "Restore readiness checks did not pass.")
            response_status = 409
        elif not form.is_valid():
            response_status = 400
        else:
            try:
                platform_services.platform_request_restore(
                    business=business,
                    backup=backup,
                    restore=restore,
                    actor=_platform_actor(request),
                    request=request,
                )
            except platform_services.PlatformBackupActionUnavailable as exc:
                messages.warning(request, str(exc))
                response_status = 503
            else:
                request.session.pop(PREFLIGHT_SESSION_KEY, None)
                messages.success(request, "The tenant restore request was queued.")
                return redirect(
                    "platformadmin:backup_restore_status",
                    business_public_id=business.public_id,
                    restore_public_id=restore.public_id,
                )

    response = render(
        request,
        "platformadmin/backups/restore_confirmation.html",
        {
            "pa_nav": "backups",
            **_capability_context(request),
            "business": business,
            "backup": backup,
            "preflight": preflight,
            "form": form,
            "mutation_capability": mutation_capability,
        },
    )
    response.status_code = response_status
    return response


@platform_backup_capability_required(PlatformBackupCapability.VIEW_METADATA)
@require_GET
def restore_status(request, business_public_id, restore_public_id):
    business = platform_selectors.get_platform_business(business_public_id)
    restore = platform_selectors.get_platform_restore(
        restore_public_id,
        business=business,
    )
    progress = restore_progress(restore)
    return render(
        request,
        "platformadmin/backups/restore_status.html",
        {
            "pa_nav": "backups",
            **_capability_context(request),
            "business": business,
            "restore": restore,
            "backup": restore.source_backup,
            "progress": progress,
            "progress_steps": restore_progress_steps(restore),
        },
    )


@platform_backup_capability_required(PlatformBackupCapability.VIEW_METADATA)
@require_GET
def operation_list(request):
    values = {}
    business_value = request.GET.get("business", "").strip()
    if business_value:
        business_uuid = _public_uuid(business_value)
        if business_uuid is None:
            operations = platform_selectors.platform_restore_operations().none()
            active_locks = TenantOperationLock.objects.none()
        else:
            values["business_uuid"] = business_uuid
            operations = platform_selectors.platform_restore_operations(values)
            active_locks = TenantOperationLock.objects.filter(
                active=True, business__public_id=business_uuid
            ).select_related("business")
    else:
        operations = platform_selectors.platform_restore_operations()
        active_locks = TenantOperationLock.objects.filter(active=True).select_related(
            "business"
        )
    status = request.GET.get("status", "")
    if status in RestoreStatus.values:
        operations = operations.filter(status=status)
    page_obj = Paginator(operations, 40).get_page(request.GET.get("page"))
    return render(
        request,
        "platformadmin/backups/operations.html",
        {
            "pa_nav": "backups",
            **_capability_context(request),
            "page_obj": page_obj,
            "active_locks": active_locks,
            "status_choices": RestoreStatus.choices,
            "querystring": _querystring_without_page(request.GET),
        },
    )


@platform_backup_capability_required(PlatformBackupCapability.VIEW_METADATA)
@require_GET
def activity_list(request):
    filter_form = PlatformActivityFilterForm(request.GET or None)
    if filter_form.is_bound and filter_form.is_valid():
        activities = platform_selectors.platform_backup_activity(
            filter_form.cleaned_data
        )
    elif filter_form.is_bound:
        activities = platform_selectors.platform_backup_activity().none()
    else:
        activities = platform_selectors.platform_backup_activity()
    page_obj = Paginator(activities, 50).get_page(request.GET.get("page"))
    return render(
        request,
        "platformadmin/backups/activity.html",
        {
            "pa_nav": "backups",
            **_capability_context(request),
            "filter_form": filter_form,
            "page_obj": page_obj,
            "querystring": _querystring_without_page(request.GET),
        },
    )
