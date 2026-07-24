from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.subscriptions.access import AccessAction
from apps.wms_core.access import wms_permission_required
from apps.wms_core.selectors import historical_locations_for_access

from . import selectors, services
from .forms import (
    WmsAssignmentForm,
    WmsAssignmentRateForm,
    WmsEmployeeForm,
    WmsProductionCategoryForm,
)
from .models import WmsEmployee


def _querystring_without(request, *keys):
    params = request.GET.copy()
    for key in keys:
        params.pop(key, None)
    return urlencode(params, doseq=True)


@wms_permission_required("wms.employees.view", action=AccessAction.READ)
def employee_list(request):
    q = request.GET.get("q", "").strip()
    locations = list(
        historical_locations_for_access(request.wms_user_access)
    )
    location_map = {str(location.public_id): location for location in locations}
    location_id = request.GET.get("location", "")
    if location_id not in location_map:
        location_id = ""
    compensation_type = request.GET.get("compensation_type", "")
    valid_compensation_types = {
        value for value, _label in WmsEmployee.CompensationType.choices
    }
    if compensation_type not in valid_compensation_types:
        compensation_type = ""

    employees = selectors.filtered_employees(
        request.wms_user_access,
        query=q,
        location_id=location_id,
        compensation_type=compensation_type,
    )
    active_employees = employees.filter(is_active=True).order_by(
        "full_name",
        "employee_code",
    )
    inactive_employees = employees.filter(is_active=False).order_by(
        "full_name",
        "employee_code",
    )
    active_page = Paginator(active_employees, 25).get_page(
        request.GET.get("active_page")
    )
    inactive_page = Paginator(inactive_employees, 25).get_page(
        request.GET.get("inactive_page")
    )
    return render(
        request,
        "wms/employees/index.html",
        {
            "active_page": active_page,
            "inactive_page": inactive_page,
            "active_count": active_employees.count(),
            "inactive_count": inactive_employees.count(),
            "q": q,
            "locations": locations,
            "location_id": location_id,
            "compensation_type": compensation_type,
            "compensation_choices": WmsEmployee.CompensationType.choices,
            "querystring": _querystring_without(
                request,
                "active_page",
                "inactive_page",
            ),
            "can_manage_employees": request.wms_user_access.has_perm(
                "wms.employees.manage"
            ),
            "active_nav": "wms",
            "wms_active_nav": "employees",
        },
    )


def _employee_detail_context(request, employee, *, assignment_form=None):
    assignments = selectors.assignments_for_employee(employee)
    can_manage = request.wms_user_access.has_perm("wms.employees.manage")
    if assignment_form is None and can_manage and employee.is_active:
        assignment_form = WmsAssignmentForm(
            request.business,
            employee,
        )
    return {
        "employee": employee,
        "active_assignments": assignments.filter(is_active=True),
        "inactive_assignments": assignments.filter(is_active=False),
        "assignment_form": assignment_form,
        "can_manage_employees": can_manage,
        "active_nav": "wms",
        "wms_active_nav": "employees",
    }


@wms_permission_required("wms.employees.view", action=AccessAction.READ)
def employee_detail(request, public_id):
    employee = selectors.get_employee_for_access(
        request.wms_user_access,
        public_id,
    )
    return render(
        request,
        "wms/employees/detail.html",
        _employee_detail_context(request, employee),
    )


@wms_permission_required("wms.employees.manage", action=AccessAction.WRITE)
def employee_form(request, public_id=None):
    instance = (
        selectors.get_employee_for_access(
            request.wms_user_access,
            public_id,
        )
        if public_id
        else None
    )
    form = WmsEmployeeForm(
        request.business,
        request.wms_user_access,
        request.POST or None,
        instance=instance,
    )
    if request.method == "POST" and form.is_valid():
        employee = services.save_employee(
            business=request.business,
            cleaned_data=form.cleaned_data,
            instance=instance,
            request=request,
        )
        messages.success(request, "Employee saved.")
        return redirect("wms:employee_detail", public_id=employee.public_id)
    return render(
        request,
        "wms/employees/form.html",
        {
            "form": form,
            "employee": instance,
            "active_nav": "wms",
            "wms_active_nav": "employees",
        },
    )


@wms_permission_required("wms.employees.manage", action=AccessAction.WRITE)
@require_POST
def employee_status(request, public_id, action):
    employee = selectors.get_employee_for_access(
        request.wms_user_access,
        public_id,
    )
    if action not in {"activate", "deactivate"}:
        return redirect("wms:employee_detail", public_id=employee.public_id)
    try:
        services.set_employee_active(
            business=request.business,
            employee=employee,
            is_active=action == "activate",
            request=request,
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("wms:employee_detail", public_id=employee.public_id)
    messages.success(
        request,
        f"Employee {'activated' if action == 'activate' else 'deactivated'}.",
    )
    return redirect("wms:employee_detail", public_id=employee.public_id)


@wms_permission_required("wms.employees.manage", action=AccessAction.WRITE)
@require_POST
def assignment_add(request, employee_public_id):
    employee = selectors.get_employee_for_access(
        request.wms_user_access,
        employee_public_id,
    )
    form = WmsAssignmentForm(
        request.business,
        employee,
        request.POST,
    )
    if form.is_valid():
        services.save_assignment(
            business=request.business,
            employee=employee,
            category=form.cleaned_data["category"],
            per_piece_rate=form.cleaned_data["per_piece_rate"],
            request=request,
        )
        messages.success(request, "Production category assigned.")
        return redirect("wms:employee_detail", public_id=employee.public_id)
    return render(
        request,
        "wms/employees/detail.html",
        _employee_detail_context(
            request,
            employee,
            assignment_form=form,
        ),
        status=400,
    )


@wms_permission_required("wms.employees.manage", action=AccessAction.WRITE)
@require_POST
def assignment_rate(
    request,
    employee_public_id,
    assignment_public_id,
):
    employee = selectors.get_employee_for_access(
        request.wms_user_access,
        employee_public_id,
    )
    assignment = selectors.get_assignment_for_employee(
        employee,
        assignment_public_id,
    )
    form = WmsAssignmentRateForm(employee, request.POST)
    if form.is_valid():
        services.save_assignment(
            business=request.business,
            employee=employee,
            category=assignment.category,
            per_piece_rate=form.cleaned_data["per_piece_rate"],
            instance=assignment,
            request=request,
        )
        messages.success(request, "Category rate updated.")
    else:
        messages.error(request, "Enter a valid nonnegative rate.")
    return redirect("wms:employee_detail", public_id=employee.public_id)


@wms_permission_required("wms.employees.manage", action=AccessAction.WRITE)
@require_POST
def assignment_status(
    request,
    employee_public_id,
    assignment_public_id,
    action,
):
    employee = selectors.get_employee_for_access(
        request.wms_user_access,
        employee_public_id,
    )
    assignment = selectors.get_assignment_for_employee(
        employee,
        assignment_public_id,
    )
    if action not in {"activate", "deactivate"}:
        return redirect("wms:employee_detail", public_id=employee.public_id)
    try:
        services.set_assignment_active(
            business=request.business,
            assignment=assignment,
            is_active=action == "activate",
            request=request,
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("wms:employee_detail", public_id=employee.public_id)
    messages.success(
        request,
        (
            "Category assignment activated."
            if action == "activate"
            else "Category assignment deactivated."
        ),
    )
    return redirect("wms:employee_detail", public_id=employee.public_id)


@wms_permission_required("wms.categories.view", action=AccessAction.READ)
def category_list(request):
    q = request.GET.get("q", "").strip()
    categories = selectors.filtered_categories(request.business, query=q)
    active_categories = categories.filter(is_active=True).order_by(
        "display_order",
        "name",
    )
    inactive_categories = categories.filter(is_active=False).order_by(
        "display_order",
        "name",
    )
    active_page = Paginator(active_categories, 25).get_page(
        request.GET.get("active_page")
    )
    inactive_page = Paginator(inactive_categories, 25).get_page(
        request.GET.get("inactive_page")
    )
    return render(
        request,
        "wms/categories/index.html",
        {
            "active_page": active_page,
            "inactive_page": inactive_page,
            "active_count": active_categories.count(),
            "inactive_count": inactive_categories.count(),
            "q": q,
            "querystring": _querystring_without(
                request,
                "active_page",
                "inactive_page",
            ),
            "can_manage_categories": request.wms_user_access.has_perm(
                "wms.categories.manage"
            ),
            "active_nav": "wms",
            "wms_active_nav": "categories",
        },
    )


@wms_permission_required("wms.categories.manage", action=AccessAction.WRITE)
def category_form(request, public_id=None):
    instance = (
        selectors.get_category_for_business(request.business, public_id)
        if public_id
        else None
    )
    form = WmsProductionCategoryForm(
        request.business,
        request.POST or None,
        instance=instance,
    )
    if request.method == "POST" and form.is_valid():
        services.save_category(
            business=request.business,
            cleaned_data=form.cleaned_data,
            instance=instance,
            request=request,
        )
        messages.success(request, "Production category saved.")
        return redirect("wms:category_list")
    return render(
        request,
        "wms/categories/form.html",
        {
            "form": form,
            "category": instance,
            "active_nav": "wms",
            "wms_active_nav": "categories",
        },
    )


@wms_permission_required("wms.categories.manage", action=AccessAction.WRITE)
@require_POST
def category_status(request, public_id, action):
    category = selectors.get_category_for_business(
        request.business,
        public_id,
    )
    if action not in {"activate", "deactivate"}:
        return redirect("wms:category_list")
    services.set_category_active(
        business=request.business,
        category=category,
        is_active=action == "activate",
        request=request,
    )
    messages.success(
        request,
        (
            "Production category activated."
            if action == "activate"
            else "Production category deactivated."
        ),
    )
    return redirect("wms:category_list")
