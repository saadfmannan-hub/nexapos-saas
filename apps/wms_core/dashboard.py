"""Efficient, permission-aware selectors for the WMS executive dashboard."""

from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce

from apps.wms_alterations.models import WmsAlteration
from apps.wms_attendance.models import WmsAttendance
from apps.wms_orders.models import WmsWorkshopOrder
from apps.wms_production.models import (
    WmsProductionEntry,
    WmsProductionEntryLine,
)


def _allowed_location_ids(user_access):
    """Return the explicit location scope, or None when all locations are allowed."""

    location_ids = tuple(location.pk for location in user_access.allowed_locations.all())
    return location_ids or None


def _scope(queryset, location_ids, *, field="location_id"):
    if location_ids is None:
        return queryset
    return queryset.filter(**{f"{field}__in": location_ids})


def _dashboard_permissions(user_access):
    permissions = user_access.permission_set
    result = {
        "orders": "wms.orders.view" in permissions,
        "alterations": "wms.alterations.view" in permissions,
        "attendance": "wms.attendance.view" in permissions,
        "production": "wms.production.view" in permissions,
        "employees": "wms.employees.view" in permissions,
    }
    result["any_operational"] = any(
        result[key] for key in ("orders", "alterations", "attendance", "production")
    )
    return result


def _orders_dashboard(user_access, location_ids, today):
    orders = _scope(
        WmsWorkshopOrder.objects.for_business(user_access.business),
        location_ids,
    )
    counts = orders.aggregate(
        received_today=Count("pk", filter=Q(received_date=today)),
        in_progress=Count(
            "pk",
            filter=Q(status=WmsWorkshopOrder.Status.IN_PROCESS),
        ),
        finished_today=Count(
            "pk",
            filter=Q(
                status=WmsWorkshopOrder.Status.FINISHED_READY,
                finished_date=today,
            ),
        ),
    )
    recent = list(orders.select_related("location__branch").order_by("-created_at", "-pk")[:6])
    recent_finished = list(
        orders.filter(status=WmsWorkshopOrder.Status.FINISHED_READY)
        .select_related("location__branch")
        .order_by("-finished_date", "-updated_at", "-pk")[:6]
    )
    return {
        **counts,
        "recent": recent,
        "recent_finished": recent_finished,
    }


def _alterations_dashboard(user_access, location_ids):
    alterations = _scope(
        WmsAlteration.objects.for_business(user_access.business),
        location_ids,
    )
    counts = alterations.aggregate(
        pending=Count(
            "pk",
            filter=Q(
                status__in=(
                    WmsAlteration.Status.OPEN,
                    WmsAlteration.Status.IN_PROGRESS,
                )
            ),
        )
    )
    recent = list(
        alterations.select_related(
            "location__branch",
            "assigned_employee",
        ).order_by("-created_at", "-pk")[:6]
    )
    return {**counts, "recent": recent}


def _comparison(today_total, yesterday_total):
    difference = today_total - yesterday_total
    percentage = round((difference / yesterday_total) * 100, 1) if yesterday_total else None
    if difference > 0:
        direction = "up"
    elif difference < 0:
        direction = "down"
    else:
        direction = "flat"
    return {
        "today": today_total,
        "yesterday": yesterday_total,
        "difference": difference,
        "difference_label": f"{difference:+d}" if difference else "0",
        "percentage": percentage,
        "percentage_label": (f"{percentage:+.1f}%" if percentage is not None else "No baseline"),
        "direction": direction,
    }


def _production_dashboard(user_access, location_ids, today):
    month_start = today.replace(day=1)
    yesterday = today - timedelta(days=1)
    period_start = min(month_start, yesterday)
    entries = _scope(
        WmsProductionEntry.objects.for_business(user_access.business),
        location_ids,
    )

    daily_rows = list(
        entries.filter(
            production_date__gte=period_start,
            production_date__lte=today,
        )
        .values("production_date")
        .annotate(total=Coalesce(Sum("daily_total_pieces"), 0))
        .order_by("production_date")
    )
    daily_totals = {row["production_date"]: int(row["total"] or 0) for row in daily_rows}
    today_total = daily_totals.get(today, 0)
    yesterday_total = daily_totals.get(yesterday, 0)

    trend_labels = []
    trend_values = []
    current = month_start
    while current <= today:
        trend_labels.append(current.strftime("%b %d"))
        trend_values.append(daily_totals.get(current, 0))
        current += timedelta(days=1)

    lines = _scope(
        WmsProductionEntryLine.objects.for_business(user_access.business).filter(
            entry__production_date__gte=month_start,
            entry__production_date__lte=today,
        ),
        location_ids,
        field="entry__location_id",
    )
    category_rows = list(
        lines.values(
            "category_id",
            "category__name",
            "category__display_order",
        )
        .annotate(total=Coalesce(Sum("quantity"), 0))
        .order_by(
            "category__display_order",
            "category__name",
        )
    )

    performance_rows = list(
        entries.filter(
            production_date__gte=month_start,
            production_date__lte=today,
        )
        .values(
            "employee_id",
            "employee__public_id",
            "employee__employee_code",
            "employee__full_name",
        )
        .annotate(
            today_pieces=Coalesce(
                Sum(
                    "daily_total_pieces",
                    filter=Q(production_date=today),
                ),
                0,
            ),
            month_pieces=Coalesce(Sum("daily_total_pieces"), 0),
        )
    )
    for row in performance_rows:
        row["today_pieces"] = int(row["today_pieces"] or 0)
        row["month_pieces"] = int(row["month_pieces"] or 0)
    top_today = sorted(
        (row for row in performance_rows if row["today_pieces"]),
        key=lambda row: (
            -row["today_pieces"],
            -row["month_pieces"],
            row["employee__full_name"].casefold(),
        ),
    )[:5]
    top_month = sorted(
        (row for row in performance_rows if row["month_pieces"]),
        key=lambda row: (
            -row["month_pieces"],
            -row["today_pieces"],
            row["employee__full_name"].casefold(),
        ),
    )[:5]

    return {
        "total_today": today_total,
        "comparison": _comparison(today_total, yesterday_total),
        "trend": {
            "labels": trend_labels,
            "data": trend_values,
        },
        "categories": {
            "labels": [row["category__name"] for row in category_rows],
            "data": [int(row["total"] or 0) for row in category_rows],
        },
        "top_today": top_today,
        "top_month": top_month,
    }


def _overall_attendance_status(record):
    statuses = {record.morning_status, record.evening_status}
    if statuses == {WmsAttendance.Status.ABSENT}:
        return WmsAttendance.Status.ABSENT
    if WmsAttendance.Status.LATE in statuses:
        return WmsAttendance.Status.LATE
    return WmsAttendance.Status.PRESENT


def _attendance_dashboard(user_access, location_ids, today):
    month_start = today.replace(day=1)
    records = _scope(
        WmsAttendance.objects.for_business(user_access.business),
        location_ids,
    )
    today_records = list(
        records.filter(attendance_date=today)
        .select_related("employee", "location__branch")
        .order_by("employee__full_name", "employee__employee_code")
    )
    counts = {
        WmsAttendance.Status.PRESENT: 0,
        WmsAttendance.Status.LATE: 0,
        WmsAttendance.Status.ABSENT: 0,
    }
    check_ins = []
    for record in today_records:
        status = _overall_attendance_status(record)
        counts[status] += 1
        for shift, check_in in (
            ("Morning", record.morning_time_in),
            ("Evening", record.evening_time_in),
        ):
            if check_in is not None:
                check_ins.append(
                    {
                        "attendance": record,
                        "shift": shift,
                        "check_in": check_in,
                        "status": status,
                    }
                )
    check_ins.sort(
        key=lambda item: (
            item["check_in"],
            item["attendance"].employee.full_name.casefold(),
        ),
        reverse=True,
    )
    attendance_count = sum(counts.values())
    attended_count = counts[WmsAttendance.Status.PRESENT] + counts[WmsAttendance.Status.LATE]
    attendance_percentage = (
        round((attended_count / attendance_count) * 100, 1) if attendance_count else 0
    )

    punctual_rows = list(
        records.filter(
            attendance_date__gte=month_start,
            attendance_date__lte=today,
        )
        .values(
            "employee_id",
            "employee__public_id",
            "employee__employee_code",
            "employee__full_name",
        )
        .annotate(
            attendance_days=Count("pk"),
            on_time_days=Count(
                "pk",
                filter=Q(
                    morning_status=WmsAttendance.Status.PRESENT,
                    evening_status=WmsAttendance.Status.PRESENT,
                ),
            ),
            late_days=Count(
                "pk",
                filter=(
                    Q(morning_status=WmsAttendance.Status.LATE)
                    | Q(evening_status=WmsAttendance.Status.LATE)
                ),
            ),
        )
    )
    for row in punctual_rows:
        row["punctuality"] = round(
            (row["on_time_days"] / row["attendance_days"]) * 100,
            1,
        )
    punctual_rows = sorted(
        (row for row in punctual_rows if row["on_time_days"]),
        key=lambda row: (
            -row["punctuality"],
            -row["on_time_days"],
            row["late_days"],
            row["employee__full_name"].casefold(),
        ),
    )[:5]

    return {
        "present": counts[WmsAttendance.Status.PRESENT],
        "late": counts[WmsAttendance.Status.LATE],
        "absent": counts[WmsAttendance.Status.ABSENT],
        "percentage": attendance_percentage,
        "recent_check_ins": check_ins[:6],
        "most_punctual": punctual_rows,
    }


def executive_dashboard(user_access, *, today):
    """Build the tenant/location-scoped dashboard without restricted queries."""

    permissions = _dashboard_permissions(user_access)
    location_ids = _allowed_location_ids(user_access)
    return {
        "today": today,
        "month_label": today.strftime("%B %Y"),
        "permissions": permissions,
        "orders": (
            _orders_dashboard(user_access, location_ids, today) if permissions["orders"] else None
        ),
        "alterations": (
            _alterations_dashboard(user_access, location_ids)
            if permissions["alterations"]
            else None
        ),
        "production": (
            _production_dashboard(user_access, location_ids, today)
            if permissions["production"]
            else None
        ),
        "attendance": (
            _attendance_dashboard(user_access, location_ids, today)
            if permissions["attendance"]
            else None
        ),
    }
