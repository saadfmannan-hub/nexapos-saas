"""Efficient, tenant- and location-scoped WMS report queries."""

from calendar import monthrange
from collections import Counter
from datetime import timedelta

from django.db.models import Count, Q, Sum

from apps.wms_alterations.models import WmsAlteration
from apps.wms_attendance.selectors import attendance_for_access
from apps.wms_core.selectors import historical_locations_for_access
from apps.wms_orders.models import WmsWorkshopOrder
from apps.wms_production.models import WmsProductionEntryLine
from apps.wms_salary import selectors as salary_selectors
from apps.wms_workforce.models import WmsEmployeeCategoryAssignment


def _month_period(report_month):
    period_end = report_month.replace(
        day=monthrange(report_month.year, report_month.month)[1]
    )
    return report_month, period_end


def _completion_percentage(completed, total):
    return round((completed / total) * 100, 1) if total else 0.0


def _orders_for_access(user_access):
    location_ids = historical_locations_for_access(user_access).values("pk")
    return (
        WmsWorkshopOrder.objects.for_business(user_access.business)
        .filter(location_id__in=location_ids)
        .select_related("location__branch")
    )


def daily_finished_orders(user_access, *, report_date, location=None):
    """Workshop orders that became Finished / Ready on the selected date."""

    orders = _orders_for_access(user_access).filter(
        status=WmsWorkshopOrder.Status.FINISHED_READY,
        finished_date=report_date,
    )
    if location is not None:
        orders = orders.filter(location=location)
    rows = list(orders.order_by("order_reference"))
    return {"rows": rows, "total_finished": len(rows)}


def daily_orders(user_access, *, report_date, location=None, status=""):
    """Order activity for one day: received or finished on the date."""

    base = _orders_for_access(user_access)
    if location is not None:
        base = base.filter(location=location)
    day_activity = base.filter(
        Q(received_date=report_date)
        | Q(
            status=WmsWorkshopOrder.Status.FINISHED_READY,
            finished_date=report_date,
        )
    )
    rows_queryset = day_activity
    if status:
        rows_queryset = rows_queryset.filter(status=status)
    rows = list(rows_queryset.order_by("-received_date", "order_reference"))

    # Summary counts ignore the status refinement so the day's totals stay
    # truthful regardless of the table filter.
    counts = base.aggregate(
        received=Count("pk", filter=Q(received_date=report_date)),
        finished=Count(
            "pk",
            filter=Q(
                status=WmsWorkshopOrder.Status.FINISHED_READY,
                finished_date=report_date,
            ),
        ),
        received_in_process=Count(
            "pk",
            filter=Q(
                received_date=report_date,
                status=WmsWorkshopOrder.Status.IN_PROCESS,
            ),
        ),
    )
    received = counts["received"] or 0
    in_process = counts["received_in_process"] or 0
    return {
        "rows": rows,
        "received": received,
        "finished": counts["finished"] or 0,
        "in_process": in_process,
        "completion_percentage": _completion_percentage(
            received - in_process, received
        ),
    }


def monthly_orders(user_access, *, report_month, location=None):
    """Month totals plus a day-wise received/finished breakdown."""

    period_start, period_end = _month_period(report_month)
    base = _orders_for_access(user_access)
    if location is not None:
        base = base.filter(location=location)
    counts = base.aggregate(
        received=Count(
            "pk",
            filter=Q(
                received_date__gte=period_start,
                received_date__lte=period_end,
            ),
        ),
        finished=Count(
            "pk",
            filter=Q(
                status=WmsWorkshopOrder.Status.FINISHED_READY,
                finished_date__gte=period_start,
                finished_date__lte=period_end,
            ),
        ),
        received_in_process=Count(
            "pk",
            filter=Q(
                received_date__gte=period_start,
                received_date__lte=period_end,
                status=WmsWorkshopOrder.Status.IN_PROCESS,
            ),
        ),
    )
    received_by_day = dict(
        base.filter(
            received_date__gte=period_start,
            received_date__lte=period_end,
        )
        .values_list("received_date")
        .annotate(total=Count("pk"))
    )
    finished_by_day = dict(
        base.filter(
            status=WmsWorkshopOrder.Status.FINISHED_READY,
            finished_date__gte=period_start,
            finished_date__lte=period_end,
        )
        .values_list("finished_date")
        .annotate(total=Count("pk"))
    )
    rows = []
    current = period_start
    while current <= period_end:
        rows.append(
            {
                "date": current,
                "received": received_by_day.get(current, 0),
                "finished": finished_by_day.get(current, 0),
            }
        )
        current += timedelta(days=1)
    received = counts["received"] or 0
    in_process = counts["received_in_process"] or 0
    return {
        "rows": rows,
        "received": received,
        "finished": counts["finished"] or 0,
        "in_process": in_process,
        "completion_percentage": _completion_percentage(
            received - in_process, received
        ),
    }


def _alterations_for_access(user_access):
    location_ids = historical_locations_for_access(user_access).values("pk")
    return (
        WmsAlteration.objects.for_business(user_access.business)
        .filter(location_id__in=location_ids)
        .select_related(
            "location__branch",
            "assigned_employee",
            "mistake_by_employee",
        )
    )


def _filtered_alterations(
    user_access,
    *,
    location=None,
    reason="",
    mistake_by_employee=None,
    assigned_employee=None,
):
    alterations = _alterations_for_access(user_access)
    if location is not None:
        alterations = alterations.filter(location=location)
    if reason:
        alterations = alterations.filter(reason=reason)
    if mistake_by_employee is not None:
        alterations = alterations.filter(mistake_by_employee=mistake_by_employee)
    if assigned_employee is not None:
        alterations = alterations.filter(assigned_employee=assigned_employee)
    return alterations


def _alteration_summary(queryset):
    counts = queryset.aggregate(
        total=Count("pk"),
        completed=Count(
            "pk",
            filter=Q(status=WmsAlteration.Status.COMPLETED),
        ),
    )
    total = counts["total"] or 0
    completed = counts["completed"] or 0
    return {
        "total": total,
        "completed": completed,
        "pending": total - completed,
        "completion_percentage": _completion_percentage(completed, total),
    }


def daily_alterations(
    user_access,
    *,
    report_date,
    location=None,
    reason="",
    mistake_by_employee=None,
    assigned_employee=None,
    status="",
):
    """The day's alteration register with its current completion state."""

    day_queryset = _filtered_alterations(
        user_access,
        location=location,
        reason=reason,
        mistake_by_employee=mistake_by_employee,
        assigned_employee=assigned_employee,
    ).filter(alteration_date=report_date)
    rows_queryset = day_queryset
    if status:
        rows_queryset = rows_queryset.filter(status=status)
    rows = list(rows_queryset.order_by("original_order_reference", "pk"))
    return {"rows": rows, **_alteration_summary(day_queryset)}


def monthly_alterations(
    user_access,
    *,
    report_month,
    location=None,
    reason="",
    employee=None,
    status="",
):
    """Month summary with day-wise, reason, and employee breakdowns."""

    period_start, period_end = _month_period(report_month)
    month_queryset = _filtered_alterations(
        user_access,
        location=location,
        reason=reason,
        assigned_employee=employee,
    ).filter(
        alteration_date__gte=period_start,
        alteration_date__lte=period_end,
    )
    if status:
        month_queryset = month_queryset.filter(status=status)

    by_day = dict(
        month_queryset.values_list("alteration_date").annotate(total=Count("pk"))
    )
    day_rows = []
    current = period_start
    while current <= period_end:
        total = by_day.get(current, 0)
        if total:
            day_rows.append({"date": current, "total": total})
        current += timedelta(days=1)

    reason_labels = dict(WmsAlteration.Reason.choices)
    reason_counter = Counter(
        dict(month_queryset.values_list("reason").annotate(total=Count("pk")))
    )
    reason_rows = [
        {"reason": reason_labels.get(code, code), "total": total}
        for code, total in reason_counter.most_common()
    ]
    most_common_reason = reason_rows[0]["reason"] if reason_rows else "—"

    employee_rows = [
        {
            "employee_code": row["assigned_employee__employee_code"],
            "employee_name": row["assigned_employee__full_name"],
            "total": row["total"],
            "completed": row["completed"],
        }
        for row in month_queryset.values(
            "assigned_employee__employee_code",
            "assigned_employee__full_name",
        )
        .annotate(
            total=Count("pk"),
            completed=Count(
                "pk",
                filter=Q(status=WmsAlteration.Status.COMPLETED),
            ),
        )
        .order_by("-total", "assigned_employee__full_name")
    ]
    return {
        "day_rows": day_rows,
        "reason_rows": reason_rows,
        "employee_rows": employee_rows,
        "most_common_reason": most_common_reason,
        **_alteration_summary(month_queryset),
    }


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
