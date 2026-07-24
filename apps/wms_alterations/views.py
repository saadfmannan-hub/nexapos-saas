from datetime import date
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.subscriptions.access import AccessAction
from apps.wms_core.access import wms_permission_required

from . import selectors, services
from .forms import AlterationCorrectionForm, AlterationCreateForm
from .models import WmsAlteration


def _querystring_without(request, *keys):
    params = request.GET.copy()
    for key in keys:
        params.pop(key, None)
    return urlencode(params, doseq=True)


def _optional_date(request):
    value = request.GET.get("date", "").strip()
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return None


@wms_permission_required("wms.alterations.view", action=AccessAction.READ)
def alteration_list(request):
    query = request.GET.get("q", "").strip()
    alteration_date = _optional_date(request)
    locations = list(
        selectors.alteration_locations_for_access(request.wms_user_access)
    )
    employees = list(
        selectors.alteration_employees_for_access(request.wms_user_access)
    )
    location_map = {str(item.public_id): item for item in locations}
    employee_map = {str(item.public_id): item for item in employees}
    location_id = request.GET.get("location", "")
    employee_id = request.GET.get("employee", "")
    status = request.GET.get("status", "")
    reason = request.GET.get("reason", "")
    order_reference = request.GET.get("order_reference", "").strip()
    if location_id not in location_map:
        location_id = ""
    if employee_id not in employee_map:
        employee_id = ""
    if status not in {
        value for value, _label in WmsAlteration.Status.choices
    }:
        status = ""
    if reason not in {
        value for value, _label in WmsAlteration.Reason.choices
    }:
        reason = ""

    alterations = selectors.filtered_alterations(
        request.wms_user_access,
        query=query,
        alteration_date=alteration_date,
        employee_id=employee_id,
        location_id=location_id,
        status=status,
        reason=reason,
        order_reference=order_reference,
    )
    page = Paginator(alterations, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "wms/alterations/index.html",
        {
            "page": page,
            "record_count": alterations.count(),
            "q": query,
            "selected_date": alteration_date,
            "locations": locations,
            "employees": employees,
            "location_id": location_id,
            "employee_id": employee_id,
            "status": status,
            "reason": reason,
            "order_reference": order_reference,
            "status_choices": WmsAlteration.Status.choices,
            "reason_choices": WmsAlteration.Reason.choices,
            "querystring": _querystring_without(request, "page"),
            "can_manage_alterations": request.wms_user_access.has_perm(
                "wms.alterations.manage"
            ),
            "active_nav": "wms",
            "wms_active_nav": "alterations",
        },
    )


@wms_permission_required("wms.alterations.view", action=AccessAction.READ)
def alteration_detail(request, public_id):
    alteration = selectors.get_alteration_for_access(
        request.wms_user_access,
        public_id,
    )
    return render(
        request,
        "wms/alterations/detail.html",
        {
            "alteration": alteration,
            "can_manage_alterations": request.wms_user_access.has_perm(
                "wms.alterations.manage"
            ),
            "can_complete_alterations": (
                request.wms_user_access.has_perm(
                    "wms.alterations.complete"
                )
                and alteration.status == WmsAlteration.Status.IN_PROGRESS
            ),
            "active_nav": "wms",
            "wms_active_nav": "alterations",
        },
    )


@wms_permission_required("wms.alterations.manage", action=AccessAction.WRITE)
def alteration_create(request):
    form = AlterationCreateForm(
        request.business,
        request.wms_user_access,
        request.POST or None,
    )
    if request.method == "POST" and form.is_valid():
        try:
            alteration = services.create_alteration(
                business=request.business,
                user_access=request.wms_user_access,
                cleaned_data=form.cleaned_data,
                request=request,
            )
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            messages.success(request, "Alteration created.")
            return redirect(
                "wms:alteration_detail",
                public_id=alteration.public_id,
            )
    return render(
        request,
        "wms/alterations/form.html",
        {
            "form": form,
            "alteration": None,
            "is_correction": False,
            "active_nav": "wms",
            "wms_active_nav": "alterations",
        },
    )


@wms_permission_required("wms.alterations.manage", action=AccessAction.WRITE)
def alteration_edit(request, public_id):
    alteration = selectors.get_alteration_for_access(
        request.wms_user_access,
        public_id,
    )
    form = AlterationCorrectionForm(
        request.business,
        request.wms_user_access,
        request.POST or None,
        instance=alteration,
    )
    if request.method == "POST" and form.is_valid():
        try:
            alteration = services.correct_alteration(
                business=request.business,
                user_access=request.wms_user_access,
                alteration=alteration,
                cleaned_data=form.cleaned_data,
                request=request,
            )
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            messages.success(request, "Alteration correction saved.")
            return redirect(
                "wms:alteration_detail",
                public_id=alteration.public_id,
            )
    return render(
        request,
        "wms/alterations/form.html",
        {
            "form": form,
            "alteration": alteration,
            "is_correction": True,
            "active_nav": "wms",
            "wms_active_nav": "alterations",
        },
    )


@wms_permission_required("wms.alterations.complete", action=AccessAction.WRITE)
@require_POST
def alteration_complete(request, public_id):
    alteration = selectors.get_alteration_for_access(
        request.wms_user_access,
        public_id,
    )
    try:
        services.complete_alteration(
            business=request.business,
            user_access=request.wms_user_access,
            alteration=alteration,
            request=request,
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, "Alteration completed.")
    return redirect(
        "wms:alteration_detail",
        public_id=alteration.public_id,
    )
