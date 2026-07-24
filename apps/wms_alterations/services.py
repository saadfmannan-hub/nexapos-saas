"""Transactional alteration mutations with tenant, scope, and audit checks."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.wms_core.models import WmsLocation
from apps.wms_workforce.models import WmsEmployee

from .models import WmsAlteration


def _actor(user=None, request=None):
    return user or getattr(request, "user", None)


def _validate_access_business(business, user_access):
    if user_access.business_id != business.pk:
        raise ValidationError("The WMS access belongs to another business.")


def _locked_location(business, user_access, location):
    try:
        location = (
            WmsLocation.objects.for_business(business)
            .select_for_update()
            .select_related("branch")
            .get(pk=location.pk)
        )
    except WmsLocation.DoesNotExist as exc:
        raise ValidationError(
            "The selected WMS location is unavailable."
        ) from exc
    if not user_access.can_access_location(location):
        raise ValidationError(
            "The WMS location is inactive or outside your allowed scope."
        )
    return location


def _locked_employee(
    business,
    user_access,
    employee,
    location,
    label,
):
    try:
        employee = (
            WmsEmployee.objects.for_business(business)
            .select_for_update()
            .select_related("location__branch")
            .get(pk=employee.pk)
        )
    except WmsEmployee.DoesNotExist as exc:
        raise ValidationError(f"{label} is unavailable.") from exc
    if not user_access.can_access_location(employee.location):
        raise ValidationError(
            f"{label} is outside your allowed WMS location scope."
        )
    if employee.location_id != location.pk:
        raise ValidationError(
            f"{label} must belong to the selected WMS location."
        )
    if (
        not employee.is_active
        or not employee.location.is_active
        or not employee.location.branch.is_active
    ):
        raise ValidationError(
            f"{label} must be an active employee at an active location."
        )
    return employee


def _resolved_people(
    *,
    business,
    user_access,
    location,
    assigned_employee,
    mistake_by,
    mistake_by_employee,
):
    assigned_employee = _locked_employee(
        business,
        user_access,
        assigned_employee,
        location,
        "Assigned To",
    )
    if mistake_by == WmsAlteration.MistakeBy.EMPLOYEE:
        if mistake_by_employee is None:
            raise ValidationError(
                "Select the employee responsible for the mistake."
            )
        mistake_by_employee = _locked_employee(
            business,
            user_access,
            mistake_by_employee,
            location,
            "Mistake By Employee",
        )
    else:
        mistake_by_employee = None
    return assigned_employee, mistake_by_employee


def _alteration_state(alteration):
    return {
        "business_public_id": str(alteration.business.public_id),
        "alteration_public_id": str(alteration.public_id),
        "location_public_id": str(alteration.location.public_id),
        "original_order_reference": alteration.original_order_reference,
        "alteration_reference": alteration.alteration_reference,
        "reason": alteration.reason,
        "mistake_by": alteration.mistake_by,
        "mistake_by_employee_public_id": (
            str(alteration.mistake_by_employee.public_id)
            if alteration.mistake_by_employee is not None
            else None
        ),
        "assigned_employee_public_id": str(
            alteration.assigned_employee.public_id
        ),
        "alteration_date": alteration.alteration_date.isoformat(),
        "status": alteration.status,
        "notes": alteration.notes,
        "is_corrected": alteration.is_corrected,
        "correction_reason": alteration.correction_reason,
        "completed_at": (
            alteration.completed_at.isoformat()
            if alteration.completed_at is not None
            else None
        ),
    }


@transaction.atomic
def create_alteration(
    *,
    business,
    user_access,
    cleaned_data,
    user=None,
    request=None,
):
    _validate_access_business(business, user_access)
    actor = _actor(user, request)
    location = _locked_location(
        business,
        user_access,
        cleaned_data["location"],
    )
    assigned_employee, mistake_by_employee = _resolved_people(
        business=business,
        user_access=user_access,
        location=location,
        assigned_employee=cleaned_data["assigned_employee"],
        mistake_by=cleaned_data["mistake_by"],
        mistake_by_employee=cleaned_data.get("mistake_by_employee"),
    )
    alteration = WmsAlteration(
        business=business,
        location=location,
        original_order_reference=cleaned_data[
            "original_order_reference"
        ],
        alteration_reference=cleaned_data.get("alteration_reference", ""),
        reason=cleaned_data["reason"],
        mistake_by=cleaned_data["mistake_by"],
        mistake_by_employee=mistake_by_employee,
        assigned_employee=assigned_employee,
        alteration_date=cleaned_data["alteration_date"],
        status=WmsAlteration.Status.OPEN,
        notes=cleaned_data.get("notes", ""),
        created_by=actor,
        updated_by=actor,
    )
    alteration.save()
    audit.log(
        "wms.alteration_created",
        business=business,
        user=actor,
        request=request,
        module="wms",
        obj=alteration,
        description=(
            f"Alteration created for order "
            f"'{alteration.original_order_reference}'."
        ),
        new_values=_alteration_state(alteration),
    )
    return alteration


@transaction.atomic
def correct_alteration(
    *,
    business,
    user_access,
    alteration,
    cleaned_data,
    user=None,
    request=None,
):
    _validate_access_business(business, user_access)
    actor = _actor(user, request)
    reason = (cleaned_data.get("correction_reason") or "").strip()
    if not reason:
        raise ValidationError("A correction reason is required.")
    try:
        alteration = (
            WmsAlteration.objects.for_business(business)
            .select_for_update()
            .select_related(
                "location__branch",
                "assigned_employee",
                "mistake_by_employee",
            )
            .get(pk=alteration.pk)
        )
    except WmsAlteration.DoesNotExist as exc:
        raise ValidationError(
            "The alteration belongs to another business."
        ) from exc

    old_values = _alteration_state(alteration)
    location = _locked_location(
        business,
        user_access,
        cleaned_data["location"],
    )
    assigned_employee, mistake_by_employee = _resolved_people(
        business=business,
        user_access=user_access,
        location=location,
        assigned_employee=cleaned_data["assigned_employee"],
        mistake_by=cleaned_data["mistake_by"],
        mistake_by_employee=cleaned_data.get("mistake_by_employee"),
    )
    alteration.location = location
    alteration.reason = cleaned_data["reason"]
    alteration.mistake_by = cleaned_data["mistake_by"]
    alteration.mistake_by_employee = mistake_by_employee
    alteration.assigned_employee = assigned_employee
    alteration.alteration_date = cleaned_data["alteration_date"]
    alteration.status = cleaned_data["status"]
    alteration.notes = cleaned_data.get("notes", "")
    alteration.is_corrected = True
    alteration.correction_reason = reason
    alteration.updated_by = actor
    alteration.save()
    new_values = _alteration_state(alteration)
    audit.log(
        "wms.alteration_updated",
        business=business,
        user=actor,
        request=request,
        module="wms",
        obj=alteration,
        description=(
            f"Alteration corrected for order "
            f"'{alteration.original_order_reference}'."
        ),
        old_values=old_values,
        new_values=new_values,
    )
    return alteration


@transaction.atomic
def complete_alteration(
    *,
    business,
    user_access,
    alteration,
    user=None,
    request=None,
):
    _validate_access_business(business, user_access)
    actor = _actor(user, request)
    try:
        alteration = (
            WmsAlteration.objects.for_business(business)
            .select_for_update()
            .select_related(
                "location__branch",
                "assigned_employee",
                "mistake_by_employee",
            )
            .get(pk=alteration.pk)
        )
    except WmsAlteration.DoesNotExist as exc:
        raise ValidationError(
            "The alteration belongs to another business."
        ) from exc
    if not user_access.can_access_location(alteration.location):
        raise ValidationError(
            "The alteration location is inactive or outside your scope."
        )
    if alteration.status != WmsAlteration.Status.IN_PROGRESS:
        raise ValidationError(
            "Only In Progress alterations can be completed."
        )

    old_values = _alteration_state(alteration)
    alteration.status = WmsAlteration.Status.COMPLETED
    alteration.completed_at = timezone.now()
    alteration.completed_by = actor
    alteration.updated_by = actor
    alteration.save()
    audit.log(
        "wms.alteration_completed",
        business=business,
        user=actor,
        request=request,
        module="wms",
        obj=alteration,
        description=(
            f"Alteration completed for order "
            f"'{alteration.original_order_reference}'."
        ),
        old_values=old_values,
        new_values=_alteration_state(alteration),
    )
    return alteration
