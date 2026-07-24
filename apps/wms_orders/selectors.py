"""Tenant- and WMS-location-scoped workshop-order read helpers."""

from django.db.models import Prefetch
from django.shortcuts import get_object_or_404

from apps.wms_core.selectors import (
    historical_locations_for_access,
    locations_for_access,
)

from .models import WmsWorkshopOrder, WmsWorkshopOrderStatusHistory


def workshop_orders_for_access(user_access):
    location_ids = historical_locations_for_access(user_access).values("pk")
    history = WmsWorkshopOrderStatusHistory.objects.for_business(
        user_access.business
    ).select_related("changed_by")
    return (
        WmsWorkshopOrder.objects.for_business(user_access.business)
        .filter(location_id__in=location_ids)
        .select_related(
            "location__branch",
            "created_by",
            "updated_by",
        )
        .prefetch_related(
            Prefetch("status_history", queryset=history),
        )
    )


def filtered_workshop_orders(
    user_access,
    *,
    query="",
    received_date=None,
    location_id="",
    status="",
):
    orders = workshop_orders_for_access(user_access)
    if query:
        orders = orders.filter(order_reference__icontains=query)
    if received_date is not None:
        orders = orders.filter(received_date=received_date)
    if location_id:
        orders = orders.filter(location__public_id=location_id)
    if status:
        orders = orders.filter(status=status)
    return orders.order_by("-received_date", "order_reference")


def get_workshop_order_for_access(user_access, public_id):
    return get_object_or_404(
        workshop_orders_for_access(user_access),
        public_id=public_id,
    )


def order_locations_for_access(user_access):
    return historical_locations_for_access(user_access)


def active_order_locations_for_access(user_access):
    return locations_for_access(user_access)
