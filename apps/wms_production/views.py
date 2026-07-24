from datetime import date
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import redirect, render

from apps.core.date_ranges import business_localdate
from apps.subscriptions.access import AccessAction
from apps.wms_core.access import wms_permission_required

from . import selectors, services
from .forms import ProductionCorrectionForm, ProductionEntryForm


def _querystring_without(request, *keys):
    params = request.GET.copy()
    for key in keys:
        params.pop(key, None)
    return urlencode(params, doseq=True)


def _selected_date(request):
    raw_value = request.GET.get("date", "").strip()
    if raw_value:
        try:
            return date.fromisoformat(raw_value)
        except ValueError:
            pass
    return business_localdate(request.business)


@wms_permission_required("wms.production.view", action=AccessAction.READ)
def production_entry_list(request):
    query = request.GET.get("q", "").strip()
    production_date = _selected_date(request)
    locations = list(
        selectors.production_locations_for_access(request.wms_user_access)
    )
    employees = list(
        selectors.employees_for_production_filters(request.wms_user_access)
    )
    categories = list(
        selectors.production_categories_for_business(request.business)
    )
    location_map = {str(item.public_id): item for item in locations}
    employee_map = {str(item.public_id): item for item in employees}
    category_map = {str(item.public_id): item for item in categories}
    location_id = request.GET.get("location", "")
    employee_id = request.GET.get("employee", "")
    category_id = request.GET.get("category", "")
    if location_id not in location_map:
        location_id = ""
    if employee_id not in employee_map:
        employee_id = ""
    if category_id not in category_map:
        category_id = ""

    entries = selectors.filtered_production_entries(
        request.wms_user_access,
        query=query,
        production_date=production_date,
        employee_id=employee_id,
        location_id=location_id,
        category_id=category_id,
    )
    page = Paginator(entries, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "wms/production/index.html",
        {
            "page": page,
            "record_count": entries.count(),
            "q": query,
            "selected_date": production_date,
            "locations": locations,
            "employees": employees,
            "categories": categories,
            "location_id": location_id,
            "employee_id": employee_id,
            "category_id": category_id,
            "querystring": _querystring_without(request, "page"),
            "can_manage_production": request.wms_user_access.has_perm(
                "wms.production.manage"
            ),
            "can_correct_production": request.wms_user_access.has_perm(
                "wms.production.correct"
            ),
            "active_nav": "wms",
            "wms_active_nav": "production",
        },
    )


@wms_permission_required("wms.production.view", action=AccessAction.READ)
def production_entry_detail(request, public_id):
    entry = selectors.get_production_entry_for_access(
        request.wms_user_access,
        public_id,
    )
    return render(
        request,
        "wms/production/detail.html",
        {
            "entry": entry,
            "can_correct_production": request.wms_user_access.has_perm(
                "wms.production.correct"
            ),
            "active_nav": "wms",
            "wms_active_nav": "production",
        },
    )


@wms_permission_required("wms.production.manage", action=AccessAction.WRITE)
def production_entry_create(request):
    selected_employee = None
    if request.method == "GET":
        selected_employee = selectors.eligible_employee_for_production(
            request.wms_user_access,
            request.GET.get("employee", ""),
        )
    form = ProductionEntryForm(
        request.business,
        request.wms_user_access,
        request.POST or None,
        selected_employee=selected_employee,
    )
    selected_employee = form.selected_employee
    if request.method == "POST" and form.is_valid():
        try:
            entry = services.create_production_entry(
                business=request.business,
                location=form.cleaned_data["location"],
                employee=form.cleaned_data["employee"],
                production_date=form.cleaned_data["production_date"],
                daily_total_pieces=form.cleaned_data["daily_total_pieces"],
                notes=form.cleaned_data["notes"],
                assignment_quantities=form.assignment_quantities(),
                request=request,
            )
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            messages.success(request, "Production entry saved.")
            return redirect(
                "wms:production_entry_detail",
                public_id=entry.public_id,
            )
    return render(
        request,
        "wms/production/form.html",
        {
            "form": form,
            "entry": None,
            "is_correction": False,
            "selected_employee": selected_employee,
            "employee_options": (
                selectors.active_employees_for_production(
                    request.wms_user_access
                )
            ),
            "active_nav": "wms",
            "wms_active_nav": "production",
        },
    )


@wms_permission_required("wms.production.correct", action=AccessAction.WRITE)
def production_entry_correct(request, public_id):
    entry = selectors.get_production_entry_for_access(
        request.wms_user_access,
        public_id,
    )
    form = ProductionCorrectionForm(
        request.business,
        request.POST or None,
        instance=entry,
    )
    if request.method == "POST" and form.is_valid():
        try:
            entry = services.correct_production_entry(
                business=request.business,
                entry=entry,
                daily_total_pieces=form.cleaned_data["daily_total_pieces"],
                notes=form.cleaned_data["notes"],
                line_quantities=form.line_quantities(),
                correction_reason=form.cleaned_data["correction_reason"],
                request=request,
            )
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            messages.success(request, "Production correction saved.")
            return redirect(
                "wms:production_entry_detail",
                public_id=entry.public_id,
            )
    return render(
        request,
        "wms/production/form.html",
        {
            "form": form,
            "entry": entry,
            "is_correction": True,
            "selected_employee": entry.employee,
            "employee_options": (),
            "active_nav": "wms",
            "wms_active_nav": "production",
        },
    )
