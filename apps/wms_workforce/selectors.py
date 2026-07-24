"""Tenant- and WMS-location-scoped workforce read helpers."""

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404

from apps.wms_core.selectors import historical_locations_for_access

from .models import (
    WmsEmployee,
    WmsEmployeeCategoryAssignment,
    WmsProductionCategory,
)


def employees_for_access(user_access):
    location_ids = historical_locations_for_access(user_access).values("pk")
    return (
        WmsEmployee.objects.for_business(user_access.business)
        .filter(location_id__in=location_ids)
        .select_related(
            "location__branch",
            "created_by",
            "updated_by",
        )
    )


def filtered_employees(
    user_access,
    *,
    query="",
    location_id="",
    compensation_type="",
):
    employees = employees_for_access(user_access)
    if query:
        employees = employees.filter(
            Q(full_name__icontains=query)
            | Q(employee_code__icontains=query)
            | Q(mobile__icontains=query)
        )
    if location_id:
        employees = employees.filter(location__public_id=location_id)
    if compensation_type:
        employees = employees.filter(compensation_type=compensation_type)
    return employees


def get_employee_for_access(user_access, public_id):
    return get_object_or_404(
        employees_for_access(user_access),
        public_id=public_id,
    )


def categories_for_business(business):
    return WmsProductionCategory.objects.for_business(business)


def filtered_categories(business, *, query=""):
    categories = categories_for_business(business).annotate(
        active_assignment_count=Count(
            "employee_assignments",
            filter=Q(employee_assignments__is_active=True),
        )
    )
    if query:
        categories = categories.filter(
            Q(name__icontains=query) | Q(code__icontains=query)
        )
    return categories


def get_category_for_business(business, public_id):
    return get_object_or_404(
        categories_for_business(business),
        public_id=public_id,
    )


def assignments_for_employee(employee):
    return (
        WmsEmployeeCategoryAssignment.objects.for_business(employee.business)
        .filter(employee=employee)
        .select_related("category", "created_by", "updated_by")
        .order_by("category__display_order", "category__name")
    )


def get_assignment_for_employee(employee, public_id):
    return get_object_or_404(
        assignments_for_employee(employee),
        public_id=public_id,
    )
