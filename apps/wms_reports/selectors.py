"""Efficient, tenant- and location-scoped WMS report queries."""

from calendar import monthrange
from datetime import timedelta

from django.db.models import Sum

from apps.wms_attendance.selectors import attendance_for_access
from apps.wms_core.selectors import historical_locations_for_access
from apps.wms_production.models import WmsProductionEntryLine
from apps.wms_salary import selectors as salary_selectors
from apps.wms_workforce.models import WmsEmployeeCategoryAssignment


def _production_lines_for_access(user_access):
    location_ids = historical_locations_for_access(user_access).values("pk")
    return WmsProductionEntryLine.objects.for_business(user_access.business).filter(
        entry__location_id__in=location_ids
    )


def daily_production(
    user_access,
    *,
    report_date,
    location=None,
    employee=None,
    category=None,
):
    """Return employee/category totals from stored Phase 4 production lines."""

    lines = _production_lines_for_access(user_access).filter(
        entry__production_date=report_date,
    )
    if location is not None:
        lines = lines.filter(entry__location=location)
    if employee is not None:
        lines = lines.filter(entry__employee=employee)
    if category is not None:
        lines = lines.filter(category=category)
    values = list(
        lines.values(
            "entry__employee_id",
            "entry__employee__employee_code",
            "entry__employee__full_name",
            "entry__location_id",
            "entry__location__branch__name",
            "entry__daily_total_pieces",
            "category_id",
            "category__name",
            "category__display_order",
        )
        .annotate(quantity=Sum("quantity"))
        .order_by(
            "entry__employee__full_name",
            "entry__employee__employee_code",
            "category__display_order",
            "category__name",
        )
    )
    categories = {}
    employees = {}
    for item in values:
        category_key = item["category_id"]
        categories[category_key] = {
            "id": category_key,
            "name": item["category__name"],
            "order": item["category__display_order"],
        }
        employee_key = (
            item["entry__employee_id"],
            item["entry__location_id"],
        )
        row = employees.setdefault(
            employee_key,
            {
                "employee_code": item["entry__employee__employee_code"],
                "employee_name": item["entry__employee__full_name"],
                "location_name": item["entry__location__branch__name"],
                "quantities": {},
                "total": item["entry__daily_total_pieces"],
            },
        )
        row["quantities"][category_key] = item["quantity"] or 0

    category_list = sorted(
        categories.values(),
        key=lambda item: (item["order"], item["name"].casefold()),
    )
    category_totals = [0] * len(category_list)
    rows = []
    for employee_row in employees.values():
        quantities = [employee_row["quantities"].get(item["id"], 0) for item in category_list]
        for index, quantity in enumerate(quantities):
            category_totals[index] += quantity
        rows.append(
            {
                **employee_row,
                "values": quantities,
            }
        )
    return {
        "categories": category_list,
        "rows": rows,
        "category_totals": category_totals,
        "grand_total": sum(row["total"] for row in rows),
    }


def monthly_employee_production(
    user_access,
    *,
    report_month,
    employee,
    location=None,
):
    """Return a full calendar month with assigned category production totals."""

    period_end = report_month.replace(day=monthrange(report_month.year, report_month.month)[1])
    lines = _production_lines_for_access(user_access).filter(
        entry__employee=employee,
        entry__production_date__gte=report_month,
        entry__production_date__lte=period_end,
    )
    if location is not None:
        lines = lines.filter(entry__location=location)
    values = list(
        lines.values(
            "entry__production_date",
            "entry__daily_total_pieces",
            "category_id",
            "category__name",
            "category__display_order",
        )
        .annotate(quantity=Sum("quantity"))
        .order_by(
            "entry__production_date",
            "category__display_order",
            "category__name",
        )
    )

    categories = {
        item.category_id: {
            "id": item.category_id,
            "name": item.category.name,
            "order": item.category.display_order,
        }
        for item in (
            WmsEmployeeCategoryAssignment.objects.for_business(user_access.business)
            .filter(
                employee=employee,
                is_active=True,
                category__is_active=True,
            )
            .select_related("category")
        )
    }
    quantities_by_day = {}
    totals_by_day = {}
    for item in values:
        category_key = item["category_id"]
        categories.setdefault(
            category_key,
            {
                "id": category_key,
                "name": item["category__name"],
                "order": item["category__display_order"],
            },
        )
        quantities_by_day.setdefault(item["entry__production_date"], {})[category_key] = (
            item["quantity"] or 0
        )
        totals_by_day[item["entry__production_date"]] = item["entry__daily_total_pieces"]

    category_list = sorted(
        categories.values(),
        key=lambda item: (item["order"], item["name"].casefold()),
    )
    category_totals = [0] * len(category_list)
    rows = []
    current = report_month
    while current <= period_end:
        day_values = quantities_by_day.get(current, {})
        quantities = [day_values.get(category["id"], 0) for category in category_list]
        for index, quantity in enumerate(quantities):
            category_totals[index] += quantity
        rows.append(
            {
                "date": current,
                "values": quantities,
                "total": totals_by_day.get(current, 0),
            }
        )
        current += timedelta(days=1)
    return {
        "categories": category_list,
        "rows": rows,
        "category_totals": category_totals,
        "grand_total": sum(row["total"] for row in rows),
    }


def attendance_summary_records(
    user_access,
    *,
    date_from,
    date_to,
    location=None,
    employee_status="",
):
    records = attendance_for_access(user_access).filter(
        attendance_date__gte=date_from,
        attendance_date__lte=date_to,
    )
    if location is not None:
        records = records.filter(location=location)
    if employee_status == "active":
        records = records.filter(employee__is_active=True)
    elif employee_status == "inactive":
        records = records.filter(employee__is_active=False)
    return records.order_by(
        "attendance_date",
        "employee__full_name",
        "employee__employee_code",
    )


def individual_attendance_records(
    user_access,
    *,
    report_month,
    employee,
):
    period_end = report_month.replace(day=monthrange(report_month.year, report_month.month)[1])
    return (
        attendance_for_access(user_access)
        .filter(
            employee=employee,
            attendance_date__gte=report_month,
            attendance_date__lte=period_end,
        )
        .order_by("attendance_date")
    )


def salary_records(
    user_access,
    *,
    report_month,
    employee=None,
    location=None,
    salary_type="",
    status="",
):
    records = salary_selectors.filtered_salary_records(
        user_access,
        salary_year=report_month.year,
        salary_month=report_month.month,
        employee_id=str(employee.public_id) if employee is not None else "",
        location_id=str(location.public_id) if location is not None else "",
        status=status,
    )
    if salary_type:
        records = records.filter(compensation_type_snapshot=salary_type)
    return records
