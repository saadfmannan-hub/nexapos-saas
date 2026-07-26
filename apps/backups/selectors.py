"""Tenant-scoped, entitlement-aware backup metadata read helpers."""

from itertools import combinations

from django.db.models import Q, Subquery
from django.shortcuts import get_object_or_404

from .enums import BackupStatus, IntegrityStatus, ProductOwner
from .models import BackupActivity, BackupRecord, BackupSchedule, RestoreOperation

_PRODUCT_ORDER = (ProductOwner.POS, ProductOwner.WMS)


def _currently_visible_product_sets(business):
    from .services import resolve_product_entitlements

    enabled = set(resolve_product_entitlements(business))
    ordered = [product for product in _PRODUCT_ORDER if product in enabled]
    return tuple(
        list(product_set)
        for size in range(1, len(ordered) + 1)
        for product_set in combinations(ordered, size)
    )


def _filter_current_entitlements(queryset, business):
    product_sets = _currently_visible_product_sets(business)
    if not product_sets:
        return queryset.none()
    visible = Q()
    for product_set in product_sets:
        visible |= Q(included_products=product_set)
    return queryset.filter(visible)


def backups_for_business(business):
    """Return business rows allowed by the tenant's current product entitlement."""

    queryset = (
        BackupRecord.objects.for_business(business)
        .select_related("created_by", "parent_restore_operation")
        .order_by("-created_at")
    )
    return _filter_current_entitlements(queryset, business)


def get_backup_for_business(business, public_id):
    return get_object_or_404(
        backups_for_business(business),
        public_id=public_id,
    )


def latest_successful_backup(business):
    return (
        backups_for_business(business)
        .filter(
            status=BackupStatus.SUCCEEDED,
            integrity_status=IntegrityStatus.VERIFIED,
        )
        .first()
    )


def restores_for_business(business):
    queryset = (
        RestoreOperation.objects.for_business(business)
        .select_related("requested_by", "source_backup", "safety_backup")
        .order_by("-created_at")
    )
    visible_backup_ids = backups_for_business(business).values("pk")
    return queryset.filter(source_backup_id__in=Subquery(visible_backup_ids))


def get_restore_for_business(business, public_id):
    return get_object_or_404(
        restores_for_business(business),
        public_id=public_id,
    )


def activities_for_business(business):
    """Return evidence linked only to currently visible product records.

    Unlinked system events are intentionally excluded from the tenant-facing
    selector because their free-form metadata could describe a disabled
    product.
    """

    queryset = (
        BackupActivity.objects.for_business(business)
        .select_related("actor", "backup", "restore")
        .order_by("-created_at")
    )
    visible_backup_ids = backups_for_business(business).values("pk")
    visible_restore_ids = restores_for_business(business).values("pk")
    return queryset.filter(
        Q(backup_id__in=Subquery(visible_backup_ids))
        | Q(restore_id__in=Subquery(visible_restore_ids))
    )


def get_schedule_for_business(business):
    """Return the single v1 schedule without raising when it is not configured."""

    if not resolve_has_enabled_product(business):
        return None
    return (
        BackupSchedule.objects.for_business(business)
        .select_related(
            "created_by",
            "last_successful_backup",
            "last_failed_backup",
        )
        .first()
    )


def resolve_has_enabled_product(business):
    from .services import resolve_product_entitlements

    return bool(resolve_product_entitlements(business))
