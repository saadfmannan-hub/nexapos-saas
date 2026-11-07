"""Permission- and location-aware WMS salary workflow views."""

from datetime import date
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.date_ranges import business_localdate
from apps.subscriptions.access import AccessAction
from apps.wms_core.access import (
    first_permitted_wms_route,
    wms_permission_required,
)
from apps.wms_production.selectors import production_entries_for_access
from apps.wms_workforce.models import WmsEmployee

from . import revisions, selectors, services
from .forms import SalaryCalculationForm, SalaryRevisionCalculationForm, SalaryRevisionStartForm
from .models import WmsSalary, WmsSalaryPieceLine


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
    if salary.revision_number and salary.status == WmsSalary.Status.CALCULATED:
        _require_owner(request)
        return redirect("wms:salary_revision", public_id=salary.public_id)
    period = revisions.salary_period(salary)
    current_number = period.filter(status=WmsSalary.Status.FINALIZED).order_by(
        "-revision_number",
    ).values_list("revision_number", flat=True).first()
    can_revise = (
        salary.status == WmsSalary.Status.FINALIZED
        and salary.revision_number == current_number
        and services.is_business_owner(request.business, request.user, request)
        and request.wms_user_access.has_perm("wms.salary.revise")
    )
    history = selectors.salary_records_for_access(request.wms_user_access).filter(
        pk__in=period.filter(status=WmsSalary.Status.FINALIZED).values("pk"),
    ).order_by("-revision_number")
    salary_days = list(salary.days.all())
    return render(
        request,
        "wms/salary/detail.html",
        {
            "salary": salary,
            "salary_month_value": (
                f"{salary.salary_year:04d}-{salary.salary_month:02d}"
            ),
            "attendance_days": (
                salary_days
                if salary.compensation_type_snapshot
                in {
                    WmsEmployee.CompensationType.FIXED_SALARY,
                    WmsEmployee.CompensationType.HYBRID,
                }
                else []
            ),
            "production_days": [
                day for day in salary_days if day.production_entry_id is not None
            ],
            "can_recalculate_salary": (
                salary.status == WmsSalary.Status.CALCULATED
                and request.wms_user_access.has_perm(
                    "wms.salary.calculate"
                )
            ),
            "can_finalize_salary": (
                salary.status == WmsSalary.Status.CALCULATED
                and services.is_business_owner(request.business, request.user, request)
                and request.wms_user_access.has_perm(
                    "wms.salary.finalize"
                )
            ),
            "revision_start_form": SalaryRevisionStartForm() if can_revise else None,
            "pending_revision": (
                period.filter(status=WmsSalary.Status.CALCULATED).first() if can_revise else None
            ),
            "history": history,
            "current_revision_number": current_number,
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
    _require_owner(request)
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


def _require_owner(request):
    if not services.is_business_owner(request.business, request.user, request):
        raise PermissionDenied("Only the Business Owner can perform this salary action.")


def _pending_revision(request, public_id):
    _require_owner(request)
    salary = get_object_or_404(
        selectors.salary_records_for_access(request.wms_user_access),
        public_id=public_id,
    )
    try:
        base = revisions.revision_base(salary)
        revisions._check_locations(request.wms_user_access, base)
    except ValidationError as exc:
        raise Http404 from exc
    return salary, base


@wms_permission_required("wms.salary.revise", action=AccessAction.WRITE)
@require_POST
def salary_revision_start(request, public_id):
    _require_owner(request)
    salary = get_object_or_404(
        selectors.salary_records_for_access(request.wms_user_access),
        public_id=public_id,
    )
    form = SalaryRevisionStartForm(request.POST)
    if form.is_valid():
        try:
            draft = revisions.start_revision(
                business=request.business, user_access=request.wms_user_access,
                salary=salary, reason=form.cleaned_data["reason"], request=request,
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            return redirect("wms:salary_revision", public_id=draft.public_id)
    else:
        messages.error(request, "Enter a correction reason of 1 to 1,000 characters.")
    return redirect("wms:salary_detail", public_id=salary.public_id)


def _revision_page(request, salary, base, fingerprint, form):
    entries = production_entries_for_access(request.wms_user_access).filter(
        employee_id=salary.employee_id,
        production_date__range=(salary.period_start, salary.period_end),
    ).order_by("production_date")
    return render(request, "wms/salary/revision.html", {
        "salary": salary,
        "base": base,
        "form": form,
        "production_entries": entries,
        "can_correct_production": request.wms_user_access.has_perm("wms.production.correct"),
        "has_proposal": bool(salary.production_fingerprint),
        "proposed_lines": WmsSalaryPieceLine.objects.for_business(request.business).filter(
            salary_day__salary=salary,
        ).select_related("salary_day"),
        "stale_proposal": bool(
            salary.production_fingerprint and salary.production_fingerprint != fingerprint
        ),
        "difference": salary.gross_salary - base.gross_salary,
        "preview": salary.calculated_at.isoformat(),
        "active_nav": "wms",
        "wms_active_nav": "salary",
    })


def _revision_inputs(request, salary, base):
    try:
        return revisions.revision_inputs(salary, base, request.wms_user_access)
    except ValidationError as exc:
        raise PermissionDenied(" ".join(exc.messages)) from exc


@wms_permission_required("wms.salary.revise", action=AccessAction.READ)
def salary_revision(request, public_id):
    salary, base = _pending_revision(request, public_id)
    fingerprint, lines = _revision_inputs(request, salary, base)
    form = SalaryRevisionCalculationForm(lines=lines, initial={"fingerprint": fingerprint})
    return _revision_page(request, salary, base, fingerprint, form)


@wms_permission_required("wms.salary.revise", action=AccessAction.WRITE)
@require_POST
def salary_revision_calculate(request, public_id):
    salary, base = _pending_revision(request, public_id)
    fingerprint, lines = _revision_inputs(request, salary, base)
    if request.POST.get("fingerprint") != fingerprint:
        messages.error(request, "Payable production changed. Review it and recalculate the revision.")
        return redirect("wms:salary_revision", public_id=salary.public_id)
    form = SalaryRevisionCalculationForm(request.POST, lines=lines)
    if form.is_valid():
        try:
            revisions.recalculate_revision(
                business=request.business, user_access=request.wms_user_access, salary=salary,
                fingerprint=form.cleaned_data["fingerprint"],
                confirmed_rates=form.confirmed_rates(), request=request,
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, "Revised salary calculated. Review the proposed amount.")
        return redirect("wms:salary_revision", public_id=salary.public_id)
    return _revision_page(request, salary, base, fingerprint, form)


@wms_permission_required("wms.salary.revise", action=AccessAction.WRITE)
@require_POST
def salary_revision_finalize(request, public_id):
    salary, _base = _pending_revision(request, public_id)
    try:
        salary = revisions.finalize_revision(
            business=request.business, user_access=request.wms_user_access,
            salary=salary, preview=request.POST.get("preview"), request=request,
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("wms:salary_revision", public_id=salary.public_id)
    messages.success(request, "Salary revision finalized.")
    return redirect("wms:salary_detail", public_id=salary.public_id)
