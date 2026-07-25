"""Permission- and location-aware WMS salary workflow views."""

from datetime import date
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.core.date_ranges import business_localdate
from apps.subscriptions.access import AccessAction
from apps.wms_core.access import (
    first_permitted_wms_route,
    wms_permission_required,
)

from . import selectors, services
from .forms import SalaryCalculationForm
from .models import WmsSalary


def _querystring_without(request, *keys):
    params = request.GET.copy()
    for key in keys:
        params.pop(key, None)
    return urlencode(params, doseq=True)


def _selected_month(request):
    value = request.GET.get("month", "").strip()
    if value:
        try:
            selected = date.fromisoformat(f"{value}-01")
        except ValueError:
            pass
        else:
            return selected
    today = business_localdate(request.business)
    return today.replace(day=1)


def _post_action_redirect(request, salary=None):
    if (
        salary is not None
        and request.wms_user_access.has_perm("wms.salary.view")
    ):
        return redirect("wms:salary_detail", public_id=salary.public_id)
    route = first_permitted_wms_route(request.wms_user_access)
    if route:
        return redirect(route)
    return HttpResponse(status=204)


@wms_permission_required("wms.salary.view", action=AccessAction.READ)
def salary_list(request):
    selected_month = _selected_month(request)
    employees = list(
        selectors.salary_employees_for_access(request.wms_user_access)
    )
    locations = list(
        selectors.salary_locations_for_access(request.wms_user_access)
    )
    employee_map = {str(item.public_id): item for item in employees}
    location_map = {str(item.public_id): item for item in locations}
    employee_id = request.GET.get("employee", "")
    location_id = request.GET.get("location", "")
    status = request.GET.get("status", "")
    if employee_id not in employee_map:
        employee_id = ""
    if location_id not in location_map:
        location_id = ""
    if status not in {value for value, _label in WmsSalary.Status.choices}:
        status = ""
    salaries = selectors.filtered_salary_records(
        request.wms_user_access,
        salary_year=selected_month.year,
        salary_month=selected_month.month,
        employee_id=employee_id,
        location_id=location_id,
        status=status,
    )
    page = Paginator(salaries, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "wms/salary/index.html",
        {
            "page": page,
            "record_count": salaries.count(),
            "selected_month": selected_month,
            "employees": employees,
            "locations": locations,
            "employee_id": employee_id,
            "location_id": location_id,
            "status": status,
            "status_choices": WmsSalary.Status.choices,
            "querystring": _querystring_without(request, "page"),
            "can_calculate_salary": request.wms_user_access.has_perm(
                "wms.salary.calculate"
            ),
            "active_nav": "wms",
            "wms_active_nav": "salary",
        },
    )


@wms_permission_required("wms.salary.view", action=AccessAction.READ)
def salary_detail(request, public_id):
    salary = selectors.get_salary_for_access(
        request.wms_user_access,
        public_id,
    )
    return render(
        request,
        "wms/salary/detail.html",
        {
            "salary": salary,
            "salary_month_value": (
                f"{salary.salary_year:04d}-{salary.salary_month:02d}"
            ),
            "can_recalculate_salary": (
                salary.status == WmsSalary.Status.CALCULATED
                and request.wms_user_access.has_perm(
                    "wms.salary.calculate"
                )
            ),
            "can_finalize_salary": (
                salary.status == WmsSalary.Status.CALCULATED
                and request.wms_user_access.has_perm(
                    "wms.salary.finalize"
                )
            ),
            "active_nav": "wms",
            "wms_active_nav": "salary",
        },
    )


@wms_permission_required("wms.salary.calculate", action=AccessAction.WRITE)
def salary_calculate(request):
    initial = {}
    if request.method == "GET":
        employee_id = request.GET.get("employee", "")
        month = request.GET.get("month", "")
        if employee_id:
            initial["employee"] = employee_id
        if month:
            try:
                initial["salary_month"] = date.fromisoformat(f"{month}-01")
            except ValueError:
                pass
    form = SalaryCalculationForm(
        request.business,
        request.wms_user_access,
        request.POST or None,
        initial=initial,
    )
    if request.method == "POST" and form.is_valid():
        salary_month = form.cleaned_data["salary_month"]
        try:
            salary = services.calculate_salary(
                business=request.business,
                user_access=request.wms_user_access,
                employee=form.cleaned_data["employee"],
                salary_year=salary_month.year,
                salary_month=salary_month.month,
                request=request,
            )
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            messages.success(request, "Salary calculation saved.")
            return _post_action_redirect(request, salary)
    return render(
        request,
        "wms/salary/calculate.html",
        {
            "form": form,
            "active_nav": "wms",
            "wms_active_nav": "salary",
        },
    )


@wms_permission_required("wms.salary.finalize", action=AccessAction.WRITE)
@require_POST
def salary_finalize(request, public_id):
    salary = (
        selectors.salary_records_for_access(request.wms_user_access)
        .filter(public_id=public_id)
        .first()
    )
    if salary is None:
        raise Http404
    try:
        salary = services.finalize_salary(
            business=request.business,
            user_access=request.wms_user_access,
            salary=salary,
            request=request,
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        salary = None
    else:
        messages.success(request, "Salary finalized.")
    return _post_action_redirect(request, salary)
