"""Tenant- and WMS-location-scoped production-entry read helpers."""

from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404

from apps.wms_core.selectors import (
    historical_locations_for_access,
    locations_for_access,
)
from apps.wms_orders.models import WmsWorkshopOrder
from apps.wms_workforce.models import WmsEmployeeCategoryAssignment
from apps.wms_workforce.selectors import (
    categories_for_business,
    employees_for_access,
)

from .models import WmsProductionEntry, WmsProductionEntryLine


def production_entries_for_access(user_access):
    location_ids = historical_locations_for_access(user_access).values("pk")
    lines = WmsProductionEntryLine.objects.for_business(
        user_access.business
    ).filter(is_removed=False).select_related("order", "assignment", "category")
    return (
        WmsProductionEntry.objects.for_business(user_access.business)
        .filter(location_id__in=location_ids)
        .select_related(
            "employee",
            "location__branch",
            "created_by",
            "updated_by",
        )
        .prefetch_related(Prefetch("lines", queryset=lines))
    )


def filtered_production_entries(
    user_access,
    *,
    query="",
    production_date=None,
    employee_id="",
    location_id="",
    category_id="",
):
    entries = production_entries_for_access(user_access)
    if query:
        entries = entries.filter(
            Q(employee__full_name__icontains=query)
            | Q(employee__employee_code__icontains=query)
            | Q(
                lines__is_removed=False,
                lines__order__order_reference__icontains=query,
            )
        )
    if production_date is not None:
        entries = entries.filter(production_date=production_date)
    if employee_id:
        entries = entries.filter(employee__public_id=employee_id)
    if location_id:
        entries = entries.filter(location__public_id=location_id)
    if category_id:
        entries = entries.filter(
            lines__category__public_id=category_id,
            lines__is_removed=False,
        )
    return entries.distinct().order_by(
        "-production_date",
        "employee__full_name",
        "employee__employee_code",
    )


def get_production_entry_for_access(user_access, public_id):
    return get_object_or_404(
        production_entries_for_access(user_access),
        public_id=public_id,
    )


def employees_for_production_filters(user_access):
    return employees_for_access(user_access).order_by(
        "full_name",
        "employee_code",
    )


def active_employees_for_production(user_access):
    return (
        employees_for_access(user_access)
        .filter(
            is_active=True,
            location__is_active=True,
            location__branch__is_active=True,
            category_assignments__is_active=True,
            category_assignments__category__is_active=True,
        )
        .distinct()
        .order_by("full_name", "employee_code")
    )


def eligible_employee_for_production(user_access, public_id):
    if not public_id:
        return None
    return active_employees_for_production(user_access).filter(
        public_id=public_id
    ).first()


def active_assignments_for_employee(employee):
    return (
        WmsEmployeeCategoryAssignment.objects.for_business(employee.business)
        .filter(
            employee=employee,
            is_active=True,
            category__is_active=True,
        )
        .select_related("category")
        .order_by("category__display_order", "category__name")
    )


def eligible_orders_for_production(user_access, location):
    if location is None or not user_access.can_access_location(location):
        return WmsWorkshopOrder.objects.none()
    return (
        WmsWorkshopOrder.objects.for_business(user_access.business)
        .filter(
            location=location,
            location__is_active=True,
            location__branch__is_active=True,
            status=WmsWorkshopOrder.Status.IN_PROCESS,
            eligible_piece_count__gt=0,
        )
        .select_related("location__branch")
        .order_by("order_reference")
    )


def production_locations_for_access(user_access):
    return historical_locations_for_access(user_access)


def active_production_locations_for_access(user_access):
    return locations_for_access(user_access)


def production_categories_for_business(business):
    return categories_for_business(business).order_by("display_order", "name")
