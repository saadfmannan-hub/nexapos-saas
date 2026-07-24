"""Transactional daily-production mutations with immutable audit coverage."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.audit import services as audit
from apps.wms_core.models import WmsLocation
from apps.wms_workforce.models import (
    WmsEmployee,
    WmsEmployeeCategoryAssignment,
)

from .models import WmsProductionEntry, WmsProductionEntryLine


def _actor(user=None, request=None):
    return user or getattr(request, "user", None)


def _validate_quantity(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{label} must be a whole nonnegative number.")
    return value


def _entry_state(entry, lines=None):
    lines = list(lines if lines is not None else entry.lines.all())
    return {
        "business_public_id": str(entry.business.public_id),
        "employee_public_id": str(entry.employee.public_id),
        "location_public_id": str(entry.location.public_id),
        "production_date": entry.production_date.isoformat(),
        "daily_total_pieces": entry.daily_total_pieces,
        "notes": entry.notes,
        "is_corrected": entry.is_corrected,
        "correction_reason": entry.correction_reason,
        "category_quantities": [
            {
                "line_public_id": str(line.public_id),
                "assignment_public_id": str(line.assignment.public_id),
                "category_public_id": str(line.category.public_id),
                "category_name": line.category_name_snapshot,
                "category_code": line.category_code_snapshot,
                "quantity": line.quantity,
            }
            for line in lines
        ],
    }


@transaction.atomic
def create_production_entry(
    *,
    business,
    location,
    employee,
    production_date,
    daily_total_pieces,
    notes,
    assignment_quantities,
    user=None,
    request=None,
):
    actor = _actor(user, request)
    daily_total_pieces = _validate_quantity(
        daily_total_pieces,
        "Daily Total Pieces",
    )
    try:
        employee = (
            WmsEmployee.objects.for_business(business)
            .select_for_update()
            .select_related("location__branch")
            .get(pk=employee.pk)
        )
        location = (
            WmsLocation.objects.for_business(business)
            .select_for_update()
            .select_related("branch")
            .get(pk=location.pk)
        )
    except (WmsEmployee.DoesNotExist, WmsLocation.DoesNotExist) as exc:
        raise ValidationError(
            "The employee or WMS location belongs to another business."
        ) from exc

    if employee.location_id != location.pk:
        raise ValidationError(
            "Production location must match the employee's WMS location."
        )
    if not employee.is_active:
        raise ValidationError(
            "Inactive employees cannot receive new production."
        )
    if not location.is_active or not location.branch.is_active:
        raise ValidationError(
            "Inactive WMS locations cannot receive new production."
        )

    assignments = list(
        WmsEmployeeCategoryAssignment.objects.for_business(business)
        .select_for_update()
        .select_related("category")
        .filter(
            employee=employee,
            is_active=True,
            category__is_active=True,
        )
        .order_by("category__display_order", "category__name")
    )
    expected_ids = {str(assignment.public_id) for assignment in assignments}
    submitted_ids = set(assignment_quantities)
    if not expected_ids:
        raise ValidationError(
            "Assign at least one active production category first."
        )
    if submitted_ids != expected_ids:
        raise ValidationError(
            "Submit every active employee category exactly once."
        )

    entry = WmsProductionEntry(
        business=business,
        location=location,
        employee=employee,
        production_date=production_date,
        daily_total_pieces=daily_total_pieces,
        notes=notes,
        created_by=actor,
        updated_by=actor,
    )
    try:
        with transaction.atomic():
            entry.save()
    except IntegrityError as exc:
        raise ValidationError(
            "Production already exists for this employee on this date."
        ) from exc

    lines = []
    for assignment in assignments:
        quantity = _validate_quantity(
            assignment_quantities[str(assignment.public_id)],
            assignment.category.name,
        )
        line = WmsProductionEntryLine(
            business=business,
            entry=entry,
            assignment=assignment,
            category=assignment.category,
            category_name_snapshot=assignment.category.name,
            category_code_snapshot=assignment.category.code,
            quantity=quantity,
        )
        line.save()
        lines.append(line)

    audit.log(
        "wms.production_entry_created",
        business=business,
        user=actor,
        request=request,
        module="wms",
        obj=entry,
        description=(
            f"Production entry created for employee "
            f"'{employee.employee_code}' on {production_date.isoformat()}."
        ),
        new_values=_entry_state(entry, lines),
    )
    return entry


@transaction.atomic
def correct_production_entry(
    *,
    business,
    entry,
    daily_total_pieces,
    notes,
    line_quantities,
    correction_reason,
    user=None,
    request=None,
):
    actor = _actor(user, request)
    reason = (correction_reason or "").strip()
    if not reason:
        raise ValidationError("A correction reason is required.")
    daily_total_pieces = _validate_quantity(
        daily_total_pieces,
        "Daily Total Pieces",
    )
    try:
        entry = (
            WmsProductionEntry.objects.for_business(business)
            .select_for_update()
            .select_related("employee", "location__branch")
            .get(pk=entry.pk)
        )
    except WmsProductionEntry.DoesNotExist as exc:
        raise ValidationError(
            "The production entry belongs to another business."
        ) from exc
    lines = list(
        WmsProductionEntryLine.objects.for_business(business)
        .select_for_update()
        .select_related("assignment", "category")
        .filter(entry=entry)
        .order_by(
            "assignment__category__display_order",
            "category_name_snapshot",
        )
    )
    expected_ids = {str(line.public_id) for line in lines}
    if set(line_quantities) != expected_ids:
        raise ValidationError(
            "Submit every saved production category exactly once."
        )

    old_values = _entry_state(entry, lines)
    entry.daily_total_pieces = daily_total_pieces
    entry.notes = notes
    entry.is_corrected = True
    entry.correction_reason = reason
    entry.updated_by = actor
    entry.save()
    for line in lines:
        line.quantity = _validate_quantity(
            line_quantities[str(line.public_id)],
            line.category_name_snapshot,
        )
        line.save()
    new_values = _entry_state(entry, lines)
    description = (
        f"Production entry corrected for employee "
        f"'{entry.employee.employee_code}' on "
        f"{entry.production_date.isoformat()}."
    )
    audit.log(
        "wms.production_entry_updated",
        business=business,
        user=actor,
        request=request,
        module="wms",
        obj=entry,
        description=description,
        old_values=old_values,
        new_values=new_values,
    )
    audit.log(
        "wms.production_entry_corrected",
        business=business,
        user=actor,
        request=request,
        module="wms",
        obj=entry,
        description=description,
        old_values=old_values,
        new_values=new_values,
    )
    return entry
