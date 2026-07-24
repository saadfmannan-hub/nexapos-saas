"""Tenant- and WMS-location-scoped attendance read helpers."""

from django.db.models import Q
from django.shortcuts import get_object_or_404

from apps.wms_core.selectors import historical_locations_for_access
from apps.wms_workforce.selectors import employees_for_access

from .models import WmsAttendance


def attendance_for_access(user_access):
    location_ids = historical_locations_for_access(user_access).values("pk")
    return (
        WmsAttendance.objects.for_business(user_access.business)
        .filter(location_id__in=location_ids)
        .select_related(
            "employee",
            "location__branch",
            "created_by",
            "updated_by",
        )
    )


def filtered_attendance(
    user_access,
    *,
    query="",
    attendance_date=None,
    location_id="",
    status="",
):
    records = attendance_for_access(user_access)
    if query:
        records = records.filter(
            Q(employee__full_name__icontains=query)
            | Q(employee__employee_code__icontains=query)
            | Q(employee__mobile__icontains=query)
        )
    if attendance_date is not None:
        records = records.filter(attendance_date=attendance_date)
    if location_id:
        records = records.filter(location__public_id=location_id)
    if status:
        records = records.filter(
            Q(morning_status=status) | Q(evening_status=status)
        )
    return records.order_by("employee__full_name", "employee__employee_code")


def get_attendance_for_access(user_access, public_id):
    return get_object_or_404(
        attendance_for_access(user_access),
        public_id=public_id,
    )


def active_employees_for_attendance(user_access):
    return (
        employees_for_access(user_access)
        .filter(
            is_active=True,
            location__is_active=True,
            location__branch__is_active=True,
        )
        .order_by("full_name", "employee_code")
    )


def attendance_locations_for_access(user_access):
    return historical_locations_for_access(user_access)
