"""Pure attendance status and in-shift minute calculations."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

ABSENT = "absent"
PRESENT = "present"
LATE = "late"


@dataclass(frozen=True, slots=True)
class AttendanceCalculation:
    morning_status: str
    evening_status: str
    morning_worked_minutes: int
    evening_worked_minutes: int
    worked_minutes: int
    missing_minutes: int


def _datetime(value):
    return datetime.combine(date.min, value)


def shift_status(time_in, shift_start, grace_period_minutes):
    if time_in is None:
        return ABSENT
    present_until = _datetime(shift_start) + timedelta(
        minutes=grace_period_minutes
    )
    return PRESENT if _datetime(time_in) <= present_until else LATE


def shift_worked_minutes(time_in, time_out, shift_start, shift_end):
    if time_in is None or time_out is None:
        return 0
    worked_from = max(_datetime(time_in), _datetime(shift_start))
    worked_until = min(_datetime(time_out), _datetime(shift_end))
    if worked_until <= worked_from:
        return 0
    return int((worked_until - worked_from).total_seconds() // 60)


def shift_scheduled_minutes(shift_start: time, shift_end: time):
    return int((_datetime(shift_end) - _datetime(shift_start)).total_seconds() // 60)


def calculate_attendance(
    *,
    morning_time_in,
    morning_time_out,
    evening_time_in,
    evening_time_out,
    morning_shift_start,
    morning_shift_end,
    evening_shift_start,
    evening_shift_end,
    grace_period_minutes,
):
    morning_minutes = shift_worked_minutes(
        morning_time_in,
        morning_time_out,
        morning_shift_start,
        morning_shift_end,
    )
    evening_minutes = shift_worked_minutes(
        evening_time_in,
        evening_time_out,
        evening_shift_start,
        evening_shift_end,
    )
    worked_minutes = morning_minutes + evening_minutes
    scheduled_minutes = shift_scheduled_minutes(
        morning_shift_start,
        morning_shift_end,
    ) + shift_scheduled_minutes(
        evening_shift_start,
        evening_shift_end,
    )
    return AttendanceCalculation(
        morning_status=shift_status(
            morning_time_in,
            morning_shift_start,
            grace_period_minutes,
        ),
        evening_status=shift_status(
            evening_time_in,
            evening_shift_start,
            grace_period_minutes,
        ),
        morning_worked_minutes=morning_minutes,
        evening_worked_minutes=evening_minutes,
        worked_minutes=worked_minutes,
        missing_minutes=max(scheduled_minutes - worked_minutes, 0),
    )
