"""Transactional WMS attendance mutations with immutable audit history."""

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.audit import services as audit
from apps.wms_core.models import WmsSettings

from .models import WmsAttendance

ATTENDANCE_TIME_FIELDS = (
    "morning_time_in",
    "morning_time_out",
    "evening_time_in",
    "evening_time_out",
)


def _actor(user=None, request=None):
    return user or getattr(request, "user", None)


def _serialized(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _attendance_state(attendance):
    return {
        "attendance_date": attendance.attendance_date.isoformat(),
        "employee_public_id": str(attendance.employee.public_id),
        "location_public_id": str(attendance.location.public_id),
        "morning_time_in": _serialized(attendance.morning_time_in),
        "morning_time_out": _serialized(attendance.morning_time_out),
        "evening_time_in": _serialized(attendance.evening_time_in),
        "evening_time_out": _serialized(attendance.evening_time_out),
        "morning_status": attendance.morning_status,
        "evening_status": attendance.evening_status,
        "morning_worked_minutes": attendance.morning_worked_minutes,
        "evening_worked_minutes": attendance.evening_worked_minutes,
        "worked_minutes": attendance.worked_minutes,
        "missing_minutes": attendance.missing_minutes,
        "correction_flag": attendance.correction_flag,
        "correction_reason": attendance.correction_reason,
    }


@transaction.atomic
def create_attendance(
    *,
    business,
    employee,
    attendance_date,
    time_values,
    user=None,
    request=None,
):
    actor = _actor(user, request)
    if employee.business_id != business.pk:
        raise ValidationError("The WMS employee belongs to another business.")
    settings_obj = (
        WmsSettings.objects.for_business(business).select_for_update().get()
    )
    attendance = WmsAttendance(
        business=business,
        employee=employee,
        location=employee.location,
        attendance_date=attendance_date,
        morning_shift_start=settings_obj.first_shift_start,
        morning_shift_end=settings_obj.first_shift_end,
        evening_shift_start=settings_obj.second_shift_start,
        evening_shift_end=settings_obj.second_shift_end,
        grace_period_minutes=settings_obj.grace_period_minutes,
        created_by=actor,
        updated_by=actor,
    )
    for field in ATTENDANCE_TIME_FIELDS:
        setattr(attendance, field, time_values.get(field))
    attendance.save()
    audit.log(
        "wms.attendance_created",
        business=business,
        user=actor,
        request=request,
        module="wms",
        obj=attendance,
        description=(
            f"Attendance created for employee '{employee.employee_code}' "
            f"on {attendance_date.isoformat()}."
        ),
        new_values=_attendance_state(attendance),
    )
    return attendance


@transaction.atomic
def correct_attendance(
    *,
    business,
    attendance,
    time_values,
    correction_reason,
    user=None,
    request=None,
):
    actor = _actor(user, request)
    reason = (correction_reason or "").strip()
    if not reason:
        raise ValidationError("A correction reason is required.")
    attendance = (
        WmsAttendance.objects.for_business(business)
        .select_for_update()
        .select_related("employee", "location__branch")
        .get(pk=attendance.pk)
    )
    old_values = _attendance_state(attendance)
    for field in ATTENDANCE_TIME_FIELDS:
        setattr(attendance, field, time_values.get(field))
    attendance.correction_flag = True
    attendance.correction_reason = reason
    attendance.updated_by = actor
    attendance.save()
    new_values = _attendance_state(attendance)
    description = (
        f"Attendance corrected for employee '{attendance.employee.employee_code}' "
        f"on {attendance.attendance_date.isoformat()}."
    )
    audit.log(
        "wms.attendance_updated",
        business=business,
        user=actor,
        request=request,
        module="wms",
        obj=attendance,
        description=description,
        old_values=old_values,
        new_values=new_values,
    )
    audit.log(
        "wms.attendance_corrected",
        business=business,
        user=actor,
        request=request,
        module="wms",
        obj=attendance,
        description=description,
        old_values=old_values,
        new_values=new_values,
    )
    return attendance
