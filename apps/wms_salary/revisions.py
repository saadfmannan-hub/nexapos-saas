"""Owner corrections of finalized WMS salary snapshots."""

import hashlib
import json
from collections import defaultdict

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.core.money import money
from apps.tenants.models import Business
from apps.wms_workforce.models import WmsEmployee

from . import services
from .models import WmsSalary, WmsSalaryPieceLine

COMPENSATION_FIELDS = (
    "period_start",
    "period_end",
    "employee_code_snapshot",
    "employee_name_snapshot",
    "employee_joining_date_snapshot",
    "compensation_type_snapshot",
    "fixed_monthly_salary_snapshot",
    "default_per_piece_rate_snapshot",
    "currency_code_snapshot",
    "currency_symbol_snapshot",
    "currency_precision_snapshot",
    "fixed_salary_component",
)
ATTENDANCE_FIELDS = (
    "salary_date",
    "location",
    "attendance",
    "morning_status_snapshot",
    "evening_status_snapshot",
    "morning_time_in_snapshot",
    "morning_time_out_snapshot",
    "evening_time_in_snapshot",
    "evening_time_out_snapshot",
    "worked_minutes_snapshot",
    "missing_minutes_snapshot",
)


def salary_period(salary):
    return WmsSalary.objects.for_business(salary.business).filter(
        employee_id=salary.employee_id,
        salary_year=salary.salary_year,
        salary_month=salary.salary_month,
    )


def _owner_access(business, user_access, user, request):
    actor = services._actor(user, request)
    access = services._authorized_access(
        business=business,
        user_access=user_access,
        actor=actor,
        permission_code="wms.salary.revise",
        request=request,
    )
    business = Business.objects.select_for_update().get(pk=business.pk)
    services._require_business_owner(business, actor, request)
    return business, access, actor


def _lock_salary(business, salary):
    locked = (
        WmsSalary.objects.for_business(business)
        .select_for_update()
        .filter(
            pk=salary.pk,
        )
        .first()
    )
    if locked is None:
        raise ValidationError("The salary belongs to another business.")
    return locked


def _check_locations(access, salary):
    services._validate_location_scope(
        access,
        salary.location_snapshots.values_list("location_id", flat=True),
    )


def _log(action, salary, base, actor, request):
    audit.log(
        action,
        business=salary.business,
        user=actor,
        request=request,
        module="wms",
        obj=salary,
        description=f"WMS salary revision {salary.revision_number} for {salary.period_start:%Y-%m}.",
        new_values={
            "salary_public_id": str(salary.public_id),
            "previous_salary_public_id": str(base.public_id),
            "revision_number": salary.revision_number,
            "reason": salary.revision_reason,
        },
    )


@transaction.atomic
def start_revision(*, business, user_access, salary, reason, user=None, request=None):
    business, access, actor = _owner_access(business, user_access, user, request)
    base = _lock_salary(business, salary)
    reason = (reason or "").strip()
    if not reason or len(reason) > 1000:
        raise ValidationError("Enter a correction reason of 1 to 1,000 characters.")
    period = salary_period(base)
    if (
        base.status != WmsSalary.Status.FINALIZED
        or period.filter(
            status=WmsSalary.Status.FINALIZED,
            revision_number__gt=base.revision_number,
        ).exists()
    ):
        raise ValidationError("Start the revision from the current finalized salary.")
    _check_locations(access, base)
    pending = period.filter(status=WmsSalary.Status.CALCULATED).first()
    if pending is not None:
        _check_locations(access, pending)
        return pending
    draft = WmsSalary.objects.create(
        business=business,
        employee_id=base.employee_id,
        salary_year=base.salary_year,
        salary_month=base.salary_month,
        revision_number=base.revision_number + 1,
        revision_reason=reason,
        revision_started_by=actor,
        gross_salary=base.fixed_salary_component,
        calculated_at=timezone.now(),
        **{name: getattr(base, name) for name in COMPENSATION_FIELDS},
    )
    _log("wms.salary_revision_started", draft, base, actor, request)
    return draft


def revision_base(draft):
    if not draft.revision_number or draft.status != WmsSalary.Status.CALCULATED:
        raise ValidationError("This salary revision is no longer pending.")
    base = (
        salary_period(draft)
        .filter(
            status=WmsSalary.Status.FINALIZED,
        )
        .order_by("-revision_number")
        .first()
    )
    if base is None or base.revision_number + 1 != draft.revision_number:
        raise ValidationError("The previous finalized salary could not be found.")
    return base


def _sources(draft, *, lock=False):
    if draft.compensation_type_snapshot == WmsEmployee.CompensationType.FIXED_SALARY:
        return [], []
    return services._production_sources(
        draft.business,
        draft.employee_id,
        max(draft.period_start, draft.employee_joining_date_snapshot),
        draft.period_end,
        lock=lock,
    )


def _fingerprint(entries, lines):
    # Public IDs survive logical restore, so a restored preview remains comparable.
    data = [
        [
            (str(entry.public_id), entry.production_date.isoformat(), str(entry.location.public_id))
            for entry in entries
        ],
        sorted(
            (
                str(line.public_id),
                str(line.entry.public_id),
                str(line.assignment.public_id),
                str(line.order.public_id) if line.order_id else "",
                line.order.order_reference if line.order_id else "",
                line.category_code_snapshot,
                line.category_name_snapshot,
                line.quantity,
            )
            for line in lines
        ),
    ]
    return hashlib.sha256(json.dumps(data, separators=(",", ":")).encode()).hexdigest()


def _historical_rates(base, lines):
    if not lines:
        return {}
    latest_by_line = {}
    by_line = defaultdict(set)
    by_assignment = defaultdict(set)
    saved_lines = (
        WmsSalaryPieceLine.objects.for_business(base.business)
        .filter(
            salary_day__salary__in=salary_period(base).filter(status=WmsSalary.Status.FINALIZED),
        )
        .order_by()
        .values_list(
            "production_line__public_id",
            "assignment_public_id_snapshot",
            "applied_rate",
            "rate_source",
            "salary_day__salary_id",
        )
    )
    for line_id, assignment, rate, source, salary_id in saved_lines:
        key = (line_id, assignment)
        if salary_id == base.pk:
            latest_by_line[key] = rate, source
        by_line[key].add(rate)
        by_assignment[assignment].add(rate)

    rates = {}
    for line in lines:
        assignment = line.assignment.public_id
        key = (line.public_id, assignment)
        if key in latest_by_line:
            rates[line.public_id] = latest_by_line[key]
        else:
            candidates = by_line[key] or by_assignment[assignment]
            if len(candidates) == 1:
                (rate,) = candidates
                rates[line.public_id] = rate, WmsSalaryPieceLine.RateSource.HISTORICAL
    return rates


def revision_inputs(draft, base, access):
    entries, lines = _sources(draft)
    services._validate_location_scope(access, {entry.location_id for entry in entries})
    rates = _historical_rates(base, lines)
    return _fingerprint(entries, lines), [line for line in lines if line.public_id not in rates]


def _lock_inputs(draft, access):
    # Production creation locks the employee, and correction locks the entry.
    # Take both locks before reading payable rows or approving a preview.
    WmsEmployee.objects.for_business(draft.business).select_for_update().get(pk=draft.employee_id)
    entries, lines = _sources(draft, lock=True)
    services._validate_location_scope(access, {entry.location_id for entry in entries})
    return entries, lines


def _revision_days(base, entries, lines, rates):
    piece_days = services._piece_days(entries, lines, lambda line: rates[line.public_id])
    if base.compensation_type_snapshot == WmsEmployee.CompensationType.PER_PIECE:
        return piece_days
    days = [
        {
            **{name: getattr(day, name) for name in ATTENDANCE_FIELDS},
            "production_entry": None,
            "eligible_quantity": 0,
            "daily_amount": services.ZERO,
            "piece_lines": [],
        }
        for day in base.days.select_related("location__branch", "attendance")
    ]
    services._merge_hybrid_days(days, piece_days)
    return days


@transaction.atomic
def recalculate_revision(
    *,
    business,
    user_access,
    salary,
    fingerprint,
    confirmed_rates,
    user=None,
    request=None,
):
    business, access, actor = _owner_access(business, user_access, user, request)
    draft = _lock_salary(business, salary)
    base = revision_base(draft)
    _check_locations(access, base)
    _check_locations(access, draft)
    entries, lines = _lock_inputs(draft, access)
    current_fingerprint = _fingerprint(entries, lines)
    if fingerprint != current_fingerprint:
        raise ValidationError("Payable production changed. Reload and recalculate the revision.")

    rates = _historical_rates(base, lines)
    rate_field = WmsSalaryPieceLine._meta.get_field("applied_rate")
    for line in lines:
        if line.public_id not in rates:
            value = confirmed_rates.get(line.public_id)
            if value is None:
                raise ValidationError(
                    "Confirm the rate for this salary period for every highlighted line."
                )
            rate = rate_field.clean(value, None)
            if rate < 0:
                raise ValidationError("Confirmed rates cannot be negative.")
            rates[line.public_id] = rate, WmsSalaryPieceLine.RateSource.OWNER_CONFIRMED

    days = _revision_days(base, entries, lines, rates)
    historical_locations = {
        snapshot.location_id: snapshot
        for snapshot in base.location_snapshots.select_related("location__branch")
    }
    locations = {key: snapshot.location for key, snapshot in historical_locations.items()}
    locations.update({day["location"].pk: day["location"] for day in days})
    services._validate_location_scope(access, locations)
    services._delete_draft_snapshots(business, draft)
    draft.total_eligible_quantity = sum(day["eligible_quantity"] for day in days)
    draft.production_salary_component = money(sum(day["daily_amount"] for day in days))
    draft.gross_salary = money(draft.fixed_salary_component + draft.production_salary_component)
    draft.calculated_by = actor
    draft.calculated_at = timezone.now()
    draft.production_fingerprint = current_fingerprint
    draft.save()
    services._save_breakdown(draft, locations, days, historical_locations=historical_locations)
    _log("wms.salary_revision_calculated", draft, base, actor, request)
    return draft


@transaction.atomic
def finalize_revision(*, business, user_access, salary, preview, user=None, request=None):
    business, access, actor = _owner_access(business, user_access, user, request)
    draft = _lock_salary(business, salary)
    base = revision_base(draft)
    _check_locations(access, base)
    if not draft.production_fingerprint or preview != draft.calculated_at.isoformat():
        raise ValidationError("Review the latest recalculated salary before finalizing.")
    entries, lines = _lock_inputs(draft, access)
    if draft.production_fingerprint != _fingerprint(entries, lines):
        raise ValidationError(
            "Payable production changed. Recalculate the revision before finalizing."
        )
    return services._finalize_locked_salary(draft, access, actor, request, revision=True)
