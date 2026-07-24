"""Tenant-scoped WMS read helpers."""

from django.shortcuts import get_object_or_404

from .models import WmsLocation, WmsUserAccess


def active_locations(business):
    return (
        WmsLocation.objects.for_business(business)
        .filter(is_active=True, branch__is_active=True)
        .select_related("branch")
        .order_by("branch__name")
    )


def locations_for_access(user_access):
    """Return active locations; an empty assignment means all active locations."""

    locations = active_locations(user_access.business)
    allowed_ids = user_access.allowed_location_ids
    if allowed_ids is not None:
        locations = locations.filter(pk__in=allowed_ids)
    return locations


def get_location_for_access(user_access, public_id):
    return get_object_or_404(
        locations_for_access(user_access),
        public_id=public_id,
    )


def get_business_location(business, public_id):
    return get_object_or_404(
        WmsLocation.objects.for_business(business).select_related("branch"),
        public_id=public_id,
    )


def get_business_user_access(business, public_id):
    return get_object_or_404(
        WmsUserAccess.objects.for_business(business).select_related(
            "membership__user",
            "role",
        ),
        public_id=public_id,
    )
