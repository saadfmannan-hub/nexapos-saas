"""Tenant- and historical-location-scoped salary read helpers."""

from django.db.models import Exists, OuterRef, Prefetch
from django.shortcuts import get_object_or_404

from apps.wms_core.selectors import historical_locations_for_access
from apps.wms_workforce.selectors import employees_for_access

from .models import (
    WmsSalary,
    WmsSalaryDay,
    WmsSalaryLocationSnapshot,
    WmsSalaryPieceLine,
)


def salary_records_for_access(user_access):
    """Return only salaries whose complete location scope is permitted."""

    salaries = (
        WmsSalary.objects.for_business(user_access.business)
        .select_related(
            "employee",
            "calculated_by",
            "finalized_by",
        )
        .prefetch_related("location_snapshots__location__branch")
    )
    allowed_ids = user_access.allowed_location_ids
    if allowed_ids is not None:
        inaccessible = (
            WmsSalaryLocationSnapshot.objects.for_business(
                user_access.business
            )
            .filter(salary_id=OuterRef("pk"))
            .exclude(location_id__in=allowed_ids)
        )
        salaries = salaries.annotate(
            has_inaccessible_location=Exists(inaccessible)
        ).filter(has_inaccessible_location=False)
    return salaries


def filtered_salary_records(
    user_access,
    *,
    salary_year,
    salary_month,
    employee_id="",
    location_id="",
    status="",
):
    salaries = salary_records_for_access(user_access).filter(
        salary_year=salary_year,
        salary_month=salary_month,
    )
    if employee_id:
        salaries = salaries.filter(employee__public_id=employee_id)
    if location_id:
        salaries = salaries.filter(
            location_snapshots__location__public_id=location_id
        )
    if status:
        salaries = salaries.filter(status=status)
    return salaries.distinct().order_by(
        "employee_name_snapshot",
        "employee_code_snapshot",
    )


def get_salary_for_access(user_access, public_id):
    piece_lines = (
        WmsSalaryPieceLine.objects.for_business(user_access.business)
        .select_related("production_line", "production_line__assignment")
        .order_by("category_name_snapshot", "category_code_snapshot")
    )
    days = (
        WmsSalaryDay.objects.for_business(user_access.business)
        .select_related(
            "location__branch",
            "attendance",
            "production_entry",
        )
        .prefetch_related(Prefetch("piece_lines", queryset=piece_lines))
        .order_by("salary_date")
    )
    return get_object_or_404(
        salary_records_for_access(user_access).prefetch_related(
            Prefetch("days", queryset=days),
        ),
        public_id=public_id,
    )


def salary_employees_for_access(user_access):
    return employees_for_access(user_access).order_by(
        "full_name",
        "employee_code",
    )


def salary_locations_for_access(user_access):
    return historical_locations_for_access(user_access)
