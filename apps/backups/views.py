"""Tenant-owner Backup & Restore UI for Phase 3C."""

from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.core.decorators import require_permission

from . import owner_services, selectors
from .enums import ProductOwner
from .forms import (
    BackupHistoryFilterForm,
    CreateBackupForm,
    RestoreConfirmationForm,
    RestorePreflightForm,
)

PREFLIGHT_SESSION_KEY = "backups_owner_preflight"


def _can(request, permission_code: str) -> bool:
    membership = getattr(request, "membership", None)
    return bool(membership and membership.has_perm(permission_code))


def _querystring_without_page(querydict) -> str:
    values = querydict.copy()
    values.pop("page", None)
    return urlencode(values, doseq=True)


def _common_owner_context(request):
    manual_capability = owner_services.manual_backup_capability()
    return {
        "active_nav": "backups",
        "manual_capability": manual_capability,
        "can_create": _can(request, "backups.create"),
        "can_restore": _can(request, "backups.restore"),
    }


def _mark_restore_eligibility(business, backups, *, blocked=False):
    rows = list(backups)
    eligible_ids = set(
        selectors.eligible_restore_backups(business)
        .filter(pk__in=[row.pk for row in rows])
        .values_list("pk", flat=True)
    )
    for row in rows:
        row.owner_restore_eligible = not blocked and row.pk in eligible_ids
    return rows


@require_permission("backups.view")
@require_GET
def dashboard(request):
    visible_backups = selectors.backups_for_business(request.business)
    active_backup = selectors.active_backup_exists(request.business)
    recent_backups = _mark_restore_eligibility(
        request.business,
        visible_backups[:5],
        blocked=active_backup,
    )
    latest_backup = recent_backups[0] if recent_backups else None
    latest_successful = selectors.latest_successful_backup(request.business)
    schedule = selectors.get_schedule_for_business(request.business)
    latest_automatic = visible_backups.filter(trigger="SCHEDULED").first()
    context = {
        **_common_owner_context(request),
        "latest_backup": latest_backup,
        "latest_successful": latest_successful,
        "latest_automatic": latest_automatic,
        "recent_backups": recent_backups,
        "schedule": schedule,
        "active_backup": active_backup,
    }
    context["create_form"] = CreateBackupForm(
        request.business,
        enabled=(context["can_create"] and context["manual_capability"].enabled),
    )
    return render(request, "backups/dashboard.html", context)


@require_permission("backups.view")
@require_GET
def history(request):
    visible_backups = selectors.backups_for_business(request.business)
    filter_form = BackupHistoryFilterForm(request.business, request.GET or None)
    if filter_form.is_bound:
        if filter_form.is_valid():
            for field in ("scope", "status", "trigger"):
                value = filter_form.cleaned_data[field]
                if value:
                    visible_backups = visible_backups.filter(**{field: value})
        else:
            visible_backups = visible_backups.none()

    active_backup = selectors.active_backup_exists(request.business)
    page_obj = Paginator(visible_backups, 30).get_page(request.GET.get("page"))
    page_obj.object_list = _mark_restore_eligibility(
        request.business,
        page_obj.object_list,
        blocked=active_backup,
    )
    querystring = _querystring_without_page(request.GET)
    return render(
        request,
        "backups/history.html",
        {
            **_common_owner_context(request),
            "filter_form": filter_form,
            "page_obj": page_obj,
            "querystring": f"{querystring}&" if querystring else "",
            "active_backup": active_backup,
        },
    )


@require_permission("backups.view")
@require_GET
def detail(request, public_id):
    backup = selectors.get_backup_for_business(request.business, public_id)
    visible_component_products = {
        ProductOwner.SHARED,
        *(backup.included_products or ()),
    }
    context = {
        **_common_owner_context(request),
        "backup": backup,
        "components": backup.components.filter(
            product_category__in=visible_component_products
        ).order_by("product_category", "component_key"),
        "restore_eligible": selectors.is_backup_restore_eligible(
            request.business, backup
        ),
        "active_backup": selectors.active_backup_exists(request.business),
    }
    return render(request, "backups/detail.html", context)


@require_permission("backups.view")
@require_GET
def activity(request):
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


@require_permission("backups.create")
@require_POST
def manual_backup(request):
    capability = owner_services.manual_backup_capability()
    if not capability.enabled:
        messages.warning(request, capability.message)
        return redirect("backups:dashboard")
    form = CreateBackupForm(request.business, request.POST, enabled=True)
    if not form.is_valid():
        messages.error(request, "Choose an available backup scope.")
        return redirect("backups:dashboard")
    try:
        backup = owner_services.request_manual_backup(
            business=request.business,
            actor=request.user,
            scope=form.cleaned_data["scope"],
            request=request,
        )
    except owner_services.OwnerBackupActionUnavailable as exc:
        messages.warning(request, str(exc))
        return redirect("backups:dashboard")
    messages.success(request, "Your secure backup has been queued.")
    return redirect("backups:detail", public_id=backup.public_id)


@require_permission("backups.view")
@require_permission("backups.restore")
@require_http_methods(["GET", "POST"])
def restore_preflight(request, public_id):
    backup = selectors.get_backup_for_business(request.business, public_id)
    eligible = selectors.is_backup_restore_eligible(request.business, backup)
    if request.method == "POST":
        form = RestorePreflightForm(request.POST)
        if form.is_valid() and eligible:
            try:
                outcome = owner_services.run_restore_preflight(
                    business=request.business,
                    backup=backup,
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                    request=request,
                )
            except owner_services.OwnerBackupActionUnavailable as exc:
                messages.warning(request, str(exc))
            else:
                request.session[PREFLIGHT_SESSION_KEY] = outcome.as_session_value(
                    business_public_id=request.business.public_id,
                    backup_public_id=backup.public_id,
                )
                return redirect("backups:restore", public_id=backup.public_id)
        elif not eligible:
            messages.warning(request, "This backup is not eligible for restore.")
    else:
        form = RestorePreflightForm()
    return render(
        request,
        "backups/restore_preflight.html",
        {
            "active_nav": "backups",
            "backup": backup,
            "eligible": eligible,
            "form": form,
        },
    )


def _session_preflight(request, backup):
    value = request.session.get(PREFLIGHT_SESSION_KEY)
    if not isinstance(value, dict):
        return None
    if (
        value.get("business_public_id") != str(request.business.public_id)
        or value.get("backup_public_id") != str(backup.public_id)
    ):
        return None
    restore = selectors.restores_for_business(request.business).filter(
        public_id=value.get("restore_public_id"),
        source_backup=backup,
        requested_by=request.user,
    ).first()
    if restore is None:
        return None
    return value


@require_permission("backups.view")
@require_permission("backups.restore")
@require_http_methods(["GET", "POST"])
def restore_confirmation(request, public_id):
    backup = selectors.get_backup_for_business(request.business, public_id)
    preflight = _session_preflight(request, backup)
    if preflight is None:
        messages.warning(request, "Check restore readiness before continuing.")
        return redirect("backups:restore_preflight", public_id=backup.public_id)

    mutation_capability = owner_services.restore_mutation_capability()
    form = RestoreConfirmationForm(
        request.POST if request.method == "POST" else None
    )
    response_status = 200
    if request.method == "POST":
        if not preflight.get("ready"):
            messages.warning(request, "Restore cannot start because readiness checks did not pass.")
            response_status = 409
        elif not form.is_valid():
            response_status = 400
        elif not mutation_capability.enabled:
            messages.warning(request, mutation_capability.message)
            response_status = 503
        else:  # Defensive future boundary: mutation must never be added inline here.
            messages.warning(request, "Restore could not be queued safely.")
            response_status = 503

    response = render(
        request,
        "backups/restore_confirmation.html",
        {
            "active_nav": "backups",
            "backup": backup,
            "preflight": preflight,
            "form": form,
            "mutation_capability": mutation_capability,
        },
    )
    if response_status != 200:
        response.status_code = response_status
    return response


# Compatibility name retained for callers of the earlier read-only review.
restore_review = restore_preflight
