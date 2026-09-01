"""Atomic WMS workshop-order batch services with audit and history."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.functions import Lower

from apps.audit import services as audit
from apps.wms_core.models import WmsLocation
from apps.wms_core.selectors import historical_locations_for_access

from .models import (
    WmsWorkshopOrder,
    WmsWorkshopOrderStatusHistory,
    normalize_order_reference,
)


def _actor(user=None, request=None):
    return user or getattr(request, "user", None)


def _normalized_references(references):
    values = [
        normalize_order_reference(reference)
        for reference in references
        if normalize_order_reference(reference)
    ]
    if not values:
        raise ValidationError("Enter at least one order reference.")
    duplicates = sorted(
        {value for value in values if values.count(value) > 1}
    )
    if duplicates:
        raise ValidationError(
            f"Duplicate references in this batch: {', '.join(duplicates)}."
        )
    return values


def _normalized_order_rows(order_rows):
    normalized = []
    references = []
    for row in order_rows:
        if isinstance(row, dict):
            reference = row.get("order_reference")
            piece_count = row.get("eligible_piece_count")
        else:
            try:
                reference, piece_count = row
            except (TypeError, ValueError) as exc:
                raise ValidationError(
                    "Each order must contain a reference and PCS."
                ) from exc
        reference = normalize_order_reference(reference)
        if not reference:
            raise ValidationError("Every order requires a reference.")
        if (
            isinstance(piece_count, bool)
            or not isinstance(piece_count, int)
            or piece_count <= 0
        ):
            raise ValidationError(
                f"Eligible PCS for {reference} must be a whole number greater than zero."
            )
        references.append(reference)
        normalized.append((reference, piece_count))
    duplicates = sorted(
        {value for value in references if references.count(value) > 1}
    )
    if duplicates:
        raise ValidationError(
            f"Duplicate references in this batch: {', '.join(duplicates)}."
        )
    if not normalized:
        raise ValidationError("Enter at least one workshop order.")
    return normalized


def _order_state(order, *, previous_status=""):
    return {
        "business_public_id": str(order.business.public_id),
        "location_public_id": str(order.location.public_id),
        "order_reference": order.order_reference,
        "eligible_piece_count": order.eligible_piece_count,
        "previous_status": previous_status,
        "new_status": order.status,
        "received_date": order.received_date.isoformat(),
        "finished_date": (
            order.finished_date.isoformat()
            if order.finished_date is not None
            else None
        ),
    }


@transaction.atomic
def create_order_batch(
    *,
    business,
    user_access,
    location,
    received_date,
    order_rows,
    notes="",
    user=None,
    request=None,
):
    actor = _actor(user, request)
    order_rows = _normalized_order_rows(order_rows)
    references = [reference for reference, _piece_count in order_rows]
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
            "The selected WMS location is outside your allowed scope."
        )
    if not location.is_active or not location.branch.is_active:
        raise ValidationError(
            "Inactive WMS locations cannot receive new orders."
        )

    existing = list(
        WmsWorkshopOrder.objects.for_business(business)
        .select_for_update()
        .filter(order_reference__in=references)
        .values_list("order_reference", flat=True)
    )
    if existing:
        raise ValidationError(
            "Order references already exist: "
            f"{', '.join(sorted(existing))}."
        )

    orders = []
    try:
        with transaction.atomic():
            for reference, piece_count in order_rows:
                order = WmsWorkshopOrder(
                    business=business,
                    location=location,
                    order_reference=reference,
                    eligible_piece_count=piece_count,
                    status=WmsWorkshopOrder.Status.IN_PROCESS,
                    received_date=received_date,
                    notes=notes,
                    created_by=actor,
                    updated_by=actor,
                )
                order.save()
                WmsWorkshopOrderStatusHistory.objects.create(
                    business=business,
                    order=order,
                    previous_status="",
                    new_status=WmsWorkshopOrder.Status.IN_PROCESS,
                    changed_by=actor,
                )
                audit.log(
                    "wms.order_created",
                    business=business,
                    user=actor,
                    request=request,
                    module="wms",
                    obj=order,
                    description=(
                        f"Workshop order '{reference}' received In Process."
                    ),
                    new_values=_order_state(order),
                )
                orders.append(order)
    except IntegrityError as exc:
        conflict_exists = (
            WmsWorkshopOrder.objects.for_business(business)
            .annotate(normalized_reference=Lower("order_reference"))
            .filter(
                normalized_reference__in=[
                    reference.lower() for reference in references
                ]
            )
            .exists()
        )
        if conflict_exists:
            raise ValidationError(
                "One or more order references already exist."
            ) from exc
        raise

    audit.log(
        "wms.order_batch_created",
        business=business,
        user=actor,
        request=request,
        module="wms",
        obj=orders[0],
        description=f"{len(orders)} workshop orders received.",
        new_values={
            "business_public_id": str(business.public_id),
            "location_public_id": str(location.public_id),
            "order_references": references,
            "orders": [
                {
                    "order_reference": reference,
                    "eligible_piece_count": piece_count,
                }
                for reference, piece_count in order_rows
            ],
            "new_status": WmsWorkshopOrder.Status.IN_PROCESS,
            "received_date": received_date.isoformat(),
            "batch_count": len(orders),
        },
    )
    return orders


@transaction.atomic
def update_order_piece_count(
    *,
    business,
    user_access,
    order,
    eligible_piece_count,
    user=None,
    request=None,
):
    actor = _actor(user, request)
    if (
        isinstance(eligible_piece_count, bool)
        or not isinstance(eligible_piece_count, int)
        or eligible_piece_count <= 0
    ):
        raise ValidationError("Eligible PCS must be a whole number greater than zero.")
    try:
        order = (
            WmsWorkshopOrder.objects.for_business(business)
            .select_for_update()
            .select_related("location__branch")
            .get(pk=order.pk)
        )
    except WmsWorkshopOrder.DoesNotExist as exc:
        raise ValidationError("The workshop order belongs to another business.") from exc
    if not user_access.can_access_location(order.location):
        raise ValidationError("The workshop order is outside your allowed scope.")
    if order.status != WmsWorkshopOrder.Status.IN_PROCESS:
        raise ValidationError("Eligible PCS can be changed only while an order is In Process.")
    old_values = _order_state(order)
    order.eligible_piece_count = eligible_piece_count
    order.updated_by = actor
    order.save(update_fields=["eligible_piece_count", "updated_by", "updated_at"])
    audit.log(
        "wms.order_piece_count_updated",
        business=business,
        user=actor,
        request=request,
        module="wms",
        obj=order,
        description=f"Eligible PCS updated for workshop order '{order.order_reference}'.",
        old_values=old_values,
        new_values=_order_state(order),
    )
    return order


@transaction.atomic
def finish_order_batch(
    *,
    business,
    user_access,
    finished_date,
    references,
    user=None,
    request=None,
):
    actor = _actor(user, request)
    references = _normalized_references(references)
    allowed_locations = historical_locations_for_access(
        user_access
    ).values("pk")
    orders = list(
        WmsWorkshopOrder.objects.for_business(business)
        .select_for_update()
        .select_related("location__branch")
        .filter(
            order_reference__in=references,
            location_id__in=allowed_locations,
        )
    )
    order_map = {order.order_reference: order for order in orders}
    unavailable = sorted(set(references).difference(order_map))
    if unavailable:
        raise ValidationError(
            "Unknown or unavailable order references: "
            f"{', '.join(unavailable)}."
        )
    already_finished = sorted(
        order.order_reference
        for order in orders
        if order.status == WmsWorkshopOrder.Status.FINISHED_READY
    )
    if already_finished:
        raise ValidationError(
            "Orders already Finished / Ready: "
            f"{', '.join(already_finished)}."
        )
    invalid_dates = sorted(
        order.order_reference
        for order in orders
        if finished_date < order.received_date
    )
    if invalid_dates:
        raise ValidationError(
            "Finished date is before the received date for: "
            f"{', '.join(invalid_dates)}."
        )

    ordered_orders = [order_map[reference] for reference in references]
    for order in ordered_orders:
        previous_status = order.status
        order.status = WmsWorkshopOrder.Status.FINISHED_READY
        order.finished_date = finished_date
        order.updated_by = actor
        order.save()
        WmsWorkshopOrderStatusHistory.objects.create(
            business=business,
            order=order,
            previous_status=previous_status,
            new_status=WmsWorkshopOrder.Status.FINISHED_READY,
            changed_by=actor,
        )
        audit.log(
            "wms.order_finished",
            business=business,
            user=actor,
            request=request,
            module="wms",
            obj=order,
            description=(
                f"Workshop order '{order.order_reference}' marked "
                "Finished / Ready."
            ),
            old_values={
                **_order_state(order, previous_status=previous_status),
                "new_status": previous_status,
                "finished_date": None,
            },
            new_values=_order_state(
                order,
                previous_status=previous_status,
            ),
        )

    audit.log(
        "wms.order_batch_finished",
        business=business,
        user=actor,
        request=request,
        module="wms",
        obj=ordered_orders[0],
        description=f"{len(ordered_orders)} workshop orders finished.",
        new_values={
            "business_public_id": str(business.public_id),
            "order_references": references,
            "previous_status": WmsWorkshopOrder.Status.IN_PROCESS,
            "new_status": WmsWorkshopOrder.Status.FINISHED_READY,
            "finished_date": finished_date.isoformat(),
            "batch_count": len(ordered_orders),
        },
    )
    return ordered_orders
