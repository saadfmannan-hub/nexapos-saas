"""Atomic WMS salary calculation and finalization services."""

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.core.money import money
from apps.subscriptions.access import AccessAction
from apps.tenants.models import Business
from apps.wms_attendance.models import WmsAttendance
from apps.wms_core.access import evaluate_wms_actor_access
from apps.wms_core.models import WmsLocation, WmsUserAccess
from apps.wms_production.models import (
    WmsProductionEntry,
    WmsProductionEntryLine,
)
from apps.wms_workforce.models import (
    WmsEmployee,
    WmsEmployeeCategoryAssignment,
)

from .models import (
    WmsSalary,
    WmsSalaryDay,
    WmsSalaryLocationSnapshot,
    WmsSalaryPieceLine,
)

ZERO = Decimal("0.000")


def _actor(user=None, request=None):
    return user or getattr(request, "user", None)


def _authorized_access(
    *,
    business,
    user_access,
    actor,
    permission_code,
    request=None,
):
    if user_access.business_id != business.pk:
        raise ValidationError("The WMS access belongs to another business.")
    decision = evaluate_wms_actor_access(
        actor,
        business,
        user_access.membership,
        permission_code=permission_code,
        action=AccessAction.WRITE,
        request=request,
    )
    if not decision.allowed:
        raise ValidationError(decision.message)
    try:
        access = (
            WmsUserAccess.objects.for_business(business)
            .select_for_update()
            .select_related("membership", "role")
            .prefetch_related("allowed_locations")
            .get(pk=decision.user_access.pk)
        )
    except WmsUserAccess.DoesNotExist as exc:
        raise ValidationError("Explicit WMS user access is required.") from exc
    if not access.is_active or not access.role.is_active:
        raise ValidationError("Active WMS user access is required.")
    return access


def _period_dates(salary_year, salary_month):
    if (
        isinstance(salary_year, bool)
        or isinstance(salary_month, bool)
        or not isinstance(salary_year, int)
        or not isinstance(salary_month, int)
        or salary_year < 1
        or salary_year > 9999
        or salary_month < 1
        or salary_month > 12
    ):
        raise ValidationError("Choose a valid calendar month.")
    period_start = date(salary_year, salary_month, 1)
    period_end = date(
        salary_year,
        salary_month,
        monthrange(salary_year, salary_month)[1],
    )
    return period_start, period_end


def _validate_location_scope(access, location_ids):
    allowed_ids = access.allowed_location_ids
    if allowed_ids is not None and not set(location_ids).issubset(allowed_ids):
        raise ValidationError(
            "One or more salary locations are outside the allowed scope."
        )


def _safe_salary_state(salary, *, day_count=None, location_count=None):
    return {
        "salary_public_id": str(salary.public_id),
        "period": f"{salary.salary_year:04d}-{salary.salary_month:02d}",
        "status": salary.status,
        "day_count": (
            salary.days.count() if day_count is None else day_count
        ),
        "location_count": (
            salary.location_snapshots.count()
            if location_count is None
            else location_count
        ),
    }


def _delete_draft_snapshots(business, salary):
    WmsSalaryPieceLine.objects.for_business(business).filter(
        salary_day__salary=salary
    ).delete()
    WmsSalaryDay.objects.for_business(business).filter(salary=salary).delete()
    WmsSalaryLocationSnapshot.objects.for_business(business).filter(
        salary=salary
    ).delete()


def _fixed_breakdown(
    *,
    business,
    employee,
    period_start,
    period_end,
):
    first_date = max(period_start, employee.joining_date)
    attendance_records = list(
        WmsAttendance.objects.for_business(business)
        .select_for_update()
        .select_related("location__branch")
        .filter(
            employee=employee,
            attendance_date__gte=first_date,
            attendance_date__lte=period_end,
        )
        .order_by("attendance_date")
    )
    attendance_by_date = {
        record.attendance_date: record for record in attendance_records
    }
    locations = {employee.location_id: employee.location}
    days = []
    current = first_date
    while current <= period_end:
        attendance = attendance_by_date.get(current)
        location = attendance.location if attendance else employee.location
        locations[location.pk] = location
        days.append(
            {
                "salary_date": current,
                "location": location,
                "attendance": attendance,
                "production_entry": None,
                "morning_status_snapshot": (
                    attendance.morning_status if attendance else ""
                ),
                "evening_status_snapshot": (
                    attendance.evening_status if attendance else ""
                ),
                "morning_time_in_snapshot": (
                    attendance.morning_time_in if attendance else None
                ),
                "morning_time_out_snapshot": (
                    attendance.morning_time_out if attendance else None
                ),
                "evening_time_in_snapshot": (
                    attendance.evening_time_in if attendance else None
                ),
                "evening_time_out_snapshot": (
                    attendance.evening_time_out if attendance else None
                ),
                "worked_minutes_snapshot": (
                    attendance.worked_minutes if attendance else 0
                ),
                "missing_minutes_snapshot": (
                    attendance.missing_minutes if attendance else 0
                ),
                "eligible_quantity": 0,
                "daily_amount": ZERO,
                "piece_lines": [],
            }
        )
        current += timedelta(days=1)
    return days, locations


def _piece_breakdown(
    *,
    business,
    employee,
    period_start,
    period_end,
):
    first_date = max(period_start, employee.joining_date)
    entries = list(
        WmsProductionEntry.objects.for_business(business)
        .select_for_update()
        .select_related("location__branch")
        .filter(
            employee=employee,
            production_date__gte=first_date,
            production_date__lte=period_end,
        )
        .order_by("production_date")
    )
    entry_ids = [entry.pk for entry in entries]
    lines = list(
        WmsProductionEntryLine.objects.for_business(business)
        .select_for_update()
        .select_related("entry", "assignment", "category")
        .filter(entry_id__in=entry_ids)
        .order_by(
            "entry__production_date",
            "assignment__category__display_order",
            "category_name_snapshot",
        )
    )
    assignment_ids = {line.assignment_id for line in lines}
    assignments = {
        assignment.pk: assignment
        for assignment in (
            WmsEmployeeCategoryAssignment.objects.for_business(business)
            .select_for_update()
            .filter(pk__in=assignment_ids)
        )
    }
    lines_by_entry = {}
    for line in lines:
        if line.assignment_id not in assignments:
            raise ValidationError(
                "A production assignment is unavailable for salary calculation."
            )
        if line.entry.employee_id != employee.pk:
            raise ValidationError(
                "Production salary lines must belong to the selected employee."
            )
        assignment = assignments[line.assignment_id]
        if assignment.employee_id != employee.pk:
            raise ValidationError(
                "Production assignment belongs to another employee."
            )
        rate = assignment.per_piece_rate
        rate_source = WmsSalaryPieceLine.RateSource.ASSIGNMENT
        if rate is None:
            rate = employee.default_per_piece_rate
            rate_source = WmsSalaryPieceLine.RateSource.EMPLOYEE_DEFAULT
        if rate is None or rate < 0:
            raise ValidationError(
                "Every production line requires a valid nonnegative piece rate."
            )
        quantity = line.quantity
        line_amount = money(rate * quantity)
        lines_by_entry.setdefault(line.entry_id, []).append(
            {
                "production_line": line,
                "assignment_public_id_snapshot": assignment.public_id,
                "category_name_snapshot": line.category_name_snapshot,
                "category_code_snapshot": line.category_code_snapshot,
                "rate_source": rate_source,
                "applied_rate": money(rate),
                "quantity": quantity,
                "line_amount": line_amount,
            }
        )

    locations = {employee.location_id: employee.location}
    days = []
    for entry in entries:
        locations[entry.location_id] = entry.location
        piece_lines = lines_by_entry.get(entry.pk, [])
        eligible_quantity = sum(
            (item["quantity"] for item in piece_lines),
            0,
        )
        daily_amount = money(
            sum((item["line_amount"] for item in piece_lines), ZERO)
        )
        days.append(
            {
                "salary_date": entry.production_date,
                "location": entry.location,
                "attendance": None,
                "production_entry": entry,
                "morning_status_snapshot": "",
                "evening_status_snapshot": "",
                "morning_time_in_snapshot": None,
                "morning_time_out_snapshot": None,
                "evening_time_in_snapshot": None,
                "evening_time_out_snapshot": None,
                "worked_minutes_snapshot": 0,
                "missing_minutes_snapshot": 0,
                "eligible_quantity": eligible_quantity,
                "daily_amount": daily_amount,
                "piece_lines": piece_lines,
            }
        )
    return days, locations


@transaction.atomic
def calculate_salary(
    *,
    business,
    user_access,
    employee,
    salary_year,
    salary_month,
    user=None,
    request=None,
):
    actor = _actor(user, request)
    access = _authorized_access(
        business=business,
        user_access=user_access,
        actor=actor,
        permission_code="wms.salary.calculate",
        request=request,
    )
    business = Business.objects.select_for_update().get(pk=business.pk)
    period_start, period_end = _period_dates(salary_year, salary_month)
    try:
        employee = (
            WmsEmployee.objects.for_business(business)
            .select_for_update()
            .select_related("location__branch")
            .get(pk=employee.pk)
        )
    except WmsEmployee.DoesNotExist as exc:
        raise ValidationError("The employee belongs to another business.") from exc
    if period_end < employee.joining_date:
        raise ValidationError(
            "Salary cannot be calculated for a month before joining."
        )

    salary = (
        WmsSalary.objects.for_business(business)
        .select_for_update()
        .filter(
            employee=employee,
            salary_year=salary_year,
            salary_month=salary_month,
        )
        .first()
    )
    creating = salary is None
    if salary is not None and salary.status == WmsSalary.Status.FINALIZED:
        raise ValidationError("Finalized salary cannot be recalculated.")
    old_values = None
    if salary is not None:
        old_values = _safe_salary_state(salary)

    if employee.compensation_type == WmsEmployee.CompensationType.FIXED_SALARY:
        days, locations = _fixed_breakdown(
            business=business,
            employee=employee,
            period_start=period_start,
            period_end=period_end,
        )
        gross_salary = money(employee.fixed_monthly_salary)
        total_eligible_quantity = 0
        fixed_snapshot = money(employee.fixed_monthly_salary)
        default_rate_snapshot = None
    else:
        days, locations = _piece_breakdown(
            business=business,
            employee=employee,
            period_start=period_start,
            period_end=period_end,
        )
        gross_salary = money(
            sum((item["daily_amount"] for item in days), ZERO)
        )
        total_eligible_quantity = sum(
            (item["eligible_quantity"] for item in days),
            0,
        )
        fixed_snapshot = None
        default_rate_snapshot = money(employee.default_per_piece_rate)

    _validate_location_scope(access, locations)
    if salary is None:
        salary = WmsSalary(
            business=business,
            employee=employee,
            salary_year=salary_year,
            salary_month=salary_month,
        )
    else:
        _delete_draft_snapshots(business, salary)

    calculated_at = timezone.now()
    salary.period_start = period_start
    salary.period_end = period_end
    salary.status = WmsSalary.Status.CALCULATED
    salary.employee_code_snapshot = employee.employee_code
    salary.employee_name_snapshot = employee.full_name
    salary.employee_joining_date_snapshot = employee.joining_date
    salary.compensation_type_snapshot = employee.compensation_type
    salary.fixed_monthly_salary_snapshot = fixed_snapshot
    salary.default_per_piece_rate_snapshot = default_rate_snapshot
    salary.currency_code_snapshot = business.currency_code
    salary.currency_symbol_snapshot = business.currency_symbol
    salary.currency_precision_snapshot = business.currency_precision
    salary.total_eligible_quantity = total_eligible_quantity
    salary.gross_salary = gross_salary
    salary.calculated_by = actor
    salary.calculated_at = calculated_at
    salary.finalized_by = None
    salary.finalized_at = None
    try:
        with transaction.atomic():
            salary.save()
    except IntegrityError as exc:
        raise ValidationError(
            "Salary already exists for this employee and month."
        ) from exc

    for location in sorted(
        locations.values(),
        key=lambda item: (item.branch.name, item.pk),
    ):
        WmsSalaryLocationSnapshot.objects.create(
            business=business,
            salary=salary,
            location=location,
            location_name_snapshot=location.branch.name,
            location_type_snapshot=location.location_type,
        )

    for item in days:
        piece_lines = item.pop("piece_lines")
        salary_day = WmsSalaryDay(
            business=business,
            salary=salary,
            **item,
        )
        salary_day.save()
        for line_data in piece_lines:
            piece_line = WmsSalaryPieceLine(
                business=business,
                salary_day=salary_day,
                **line_data,
            )
            piece_line.save()

    new_values = _safe_salary_state(
        salary,
        day_count=len(days),
        location_count=len(locations),
    )
    audit.log(
        "wms.salary_calculated" if creating else "wms.salary_recalculated",
        business=business,
        user=actor,
        request=request,
        module="wms",
        obj=salary,
        description=(
            f"WMS salary {'calculated' if creating else 'recalculated'} "
            f"for period {salary_year:04d}-{salary_month:02d}."
        ),
        old_values=old_values,
        new_values=new_values,
    )
    return salary


@transaction.atomic
def finalize_salary(
    *,
    business,
    user_access,
    salary,
    user=None,
    request=None,
):
    actor = _actor(user, request)
    access = _authorized_access(
        business=business,
        user_access=user_access,
        actor=actor,
        permission_code="wms.salary.finalize",
        request=request,
    )
    try:
        salary = (
            WmsSalary.objects.for_business(business)
            .select_for_update()
            .select_related("employee")
            .get(pk=salary.pk)
        )
    except WmsSalary.DoesNotExist as exc:
        raise ValidationError("The salary belongs to another business.") from exc
    locations = list(
        WmsSalaryLocationSnapshot.objects.for_business(business)
        .select_for_update()
        .filter(salary=salary)
    )
    list(
        WmsSalaryDay.objects.for_business(business)
        .select_for_update()
        .filter(salary=salary)
    )
    list(
        WmsSalaryPieceLine.objects.for_business(business)
        .select_for_update()
        .filter(salary_day__salary=salary)
    )
    _validate_location_scope(
        access,
        {location.location_id for location in locations},
    )
    if salary.status != WmsSalary.Status.CALCULATED:
        raise ValidationError("Salary has already been finalized.")

    old_values = _safe_salary_state(
        salary,
        location_count=len(locations),
    )
    salary.status = WmsSalary.Status.FINALIZED
    salary.finalized_by = actor
    salary.finalized_at = timezone.now()
    salary.save(
        update_fields=[
            "status",
            "finalized_by",
            "finalized_at",
            "updated_at",
        ]
    )
    audit.log(
        "wms.salary_finalized",
        business=business,
        user=actor,
        request=request,
        module="wms",
        obj=salary,
        description=(
            f"WMS salary finalized for period "
            f"{salary.salary_year:04d}-{salary.salary_month:02d}."
        ),
        old_values=old_values,
        new_values=_safe_salary_state(
            salary,
            location_count=len(locations),
        ),
    )
    return salary
