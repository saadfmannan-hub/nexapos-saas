"""Tenant- and location-scoped alteration read helpers."""

from django.db.models import Q
from django.shortcuts import get_object_or_404

from apps.wms_core.selectors import (
    historical_locations_for_access,
    locations_for_access,
)
from apps.wms_workforce.selectors import employees_for_access

from .models import WmsAlteration


def alterations_for_access(user_access):
    location_ids = historical_locations_for_access(user_access).values("pk")
    return (
        WmsAlteration.objects.for_business(user_access.business)
        .filter(location_id__in=location_ids)
        .select_related(
            "location__branch",
            "assigned_employee",
            "mistake_by_employee",
            "completed_by",
            "created_by",
            "updated_by",
        )
    )


def filtered_alterations(
    user_access,
    *,
    query="",
    alteration_date=None,
    employee_id="",
    location_id="",
    status="",
    reason="",
    order_reference="",
):
    alterations = alterations_for_access(user_access)
    if query:
        alterations = alterations.filter(
            Q(original_order_reference__icontains=query)
            | Q(alteration_reference__icontains=query)
            | Q(assigned_employee__full_name__icontains=query)
            | Q(assigned_employee__employee_code__icontains=query)
            | Q(mistake_by_employee__full_name__icontains=query)
            | Q(mistake_by_employee__employee_code__icontains=query)
            | Q(notes__icontains=query)
        )
    if alteration_date is not None:
        alterations = alterations.filter(alteration_date=alteration_date)
    if employee_id:
        alterations = alterations.filter(
            assigned_employee__public_id=employee_id
        )
    if location_id:
        alterations = alterations.filter(location__public_id=location_id)
    if status:
        alterations = alterations.filter(status=status)
    if reason:
        alterations = alterations.filter(reason=reason)
    if order_reference:
        alterations = alterations.filter(
            original_order_reference__icontains=order_reference
        )
    return alterations.order_by("-alteration_date", "-created_at")


def get_alteration_for_access(user_access, public_id):
    return get_object_or_404(
        alterations_for_access(user_access),
        public_id=public_id,
    )


def alteration_locations_for_access(user_access):
    return historical_locations_for_access(user_access)


def active_alteration_locations_for_access(user_access):
    return locations_for_access(user_access)


def alteration_employees_for_access(user_access):
    return employees_for_access(user_access).order_by(
        "full_name",
        "employee_code",
    )


def active_alteration_employees_for_access(user_access):
    return (
        employees_for_access(user_access)
        .filter(
            is_active=True,
            location__is_active=True,
            location__branch__is_active=True,
        )
        .order_by("full_name", "employee_code")
    )
