"""Transfer / adjustment / count workflows built on the stock ledger."""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.core.money import D

from . import services
from .models import (
    StockAdjustment,
    StockAdjustmentItem,
    StockCount,
    StockCountItem,
    StockLevel,
    StockTransfer,
    StockTransferItem,
)


def _next_number(model, business, field, prefix):
    n = model.objects.for_business(business).count() + 1
    while model.objects.for_business(business).filter(**{field: f"{prefix}-{n:06d}"}).exists():
        n += 1
    return f"{prefix}-{n:06d}"


def _lock_inventory_rows(business, rows, *, allow_parent_meter_repair=False):
    """Refresh and lock product shape before creating inventory history."""
    from apps.catalog.models import Product

    rows = list(rows)
    if not rows:
        raise ValidationError("Enter at least one inventory item.")
    product_ids = sorted({row["product"].pk for row in rows})
    locked = {
        product.pk: product
        for product in (
            Product.objects.select_for_update()
            .select_related("unit")
            .filter(pk__in=product_ids, business=business)
            .order_by("pk")
        )
    }
    normalised = []
    for row in rows:
        product = locked.get(row["product"].pk)
        variant = row.get("variant")
        if product is None:
            raise ValidationError("Invalid product for this business.")
        if variant is not None and (
            variant.business_id != business.id
            or variant.product_id != product.id
        ):
            raise ValidationError("Invalid product variant for this business.")
        if (
            product.is_meter_tailoring
            and product.has_variants
            and variant is None
            and not allow_parent_meter_repair
        ):
            raise ValidationError(f"Select a variant/color for {product.name}.")
        normalised.append({**row, "product": product, "variant": variant})
    return normalised


def _validate_warehouse(business, warehouse):
    if warehouse.business_id != business.id or not warehouse.is_active:
        raise ValidationError(
            "Inventory warehouse must be active and belong to this business."
        )


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------
@transaction.atomic
def create_transfer(
    *,
    business,
    from_warehouse,
    to_warehouse,
    rows,
    user,
    notes="",
    membership=None,
    request=None,
):
    services.require_inventory_write(
        business=business,
        user=user,
        permission_code="inventory.transfer",
        membership=membership,
        request=request,
        warehouses=(from_warehouse, to_warehouse),
        tenant_objects=tuple(
            obj
            for row in rows
            for obj in (row.get("product"), row.get("variant"))
            if obj is not None
        ),
    )
    _validate_warehouse(business, from_warehouse)
    _validate_warehouse(business, to_warehouse)
    if from_warehouse.pk == to_warehouse.pk:
        raise ValidationError("Transfer warehouses must be different.")
    rows = _lock_inventory_rows(business, rows)
    if any(D(row.get("quantity")) <= 0 for row in rows):
        raise ValidationError("Transfer quantities must be greater than zero.")
    transfer = StockTransfer.objects.create(
        business=business,
        transfer_number=_next_number(StockTransfer, business, "transfer_number", "TRF"),
        from_warehouse=from_warehouse,
        to_warehouse=to_warehouse,
        status=StockTransfer.Status.DRAFT,
        requested_by=user,
        notes=notes,
    )
    for row in rows:
        target = row["variant"] or row["product"]
        StockTransferItem.objects.create(
            business=business, transfer=transfer,
            product=row["product"], variant=row["variant"],
            quantity=row["quantity"],
            unit_cost=D(getattr(target, "average_cost", 0)) or D(
                getattr(target, "purchase_price", 0)),
        )
    return transfer


@transaction.atomic
def dispatch_transfer(*, transfer, user, membership=None, request=None):
    services.require_inventory_write(
        business=transfer.business,
        user=user,
        permission_code="inventory.transfer",
        membership=membership,
        request=request,
        warehouses=(transfer.from_warehouse, transfer.to_warehouse),
    )
    transfer = StockTransfer.objects.select_for_update().get(
        pk=transfer.pk, business=transfer.business
    )
    if transfer.status not in (StockTransfer.Status.DRAFT, StockTransfer.Status.APPROVED):
        raise ValidationError("Transfer cannot be dispatched in its current status.")
    for item in transfer.items.select_related("product", "variant"):
        services.record_movement(
            business=transfer.business, warehouse=transfer.from_warehouse,
            product=item.product, variant=item.variant,
            movement_type="transfer_out", quantity=-item.quantity,
            unit_cost=item.unit_cost, reference_type="Transfer",
            reference_id=transfer.transfer_number, user=user,
        )
    transfer.status = StockTransfer.Status.DISPATCHED
    transfer.dispatched_by = user
    transfer.dispatched_at = timezone.now()
    transfer.save()
    audit.log("transfer.dispatched", business=transfer.business, user=user,
              request=request, module="inventory", obj=transfer,
              description=f"Transfer {transfer.transfer_number} dispatched.")
    return transfer


@transaction.atomic
def receive_transfer(*, transfer, user, membership=None, request=None):
    services.require_inventory_write(
        business=transfer.business,
        user=user,
        permission_code="inventory.transfer",
        membership=membership,
        request=request,
        warehouses=(transfer.from_warehouse, transfer.to_warehouse),
    )
    transfer = StockTransfer.objects.select_for_update().get(
        pk=transfer.pk, business=transfer.business
    )
    if transfer.status != StockTransfer.Status.DISPATCHED:
        raise ValidationError("Only dispatched transfers can be received.")
    for item in transfer.items.select_related("product", "variant"):
        services.record_movement(
            business=transfer.business, warehouse=transfer.to_warehouse,
            product=item.product, variant=item.variant,
            movement_type="transfer_in", quantity=item.quantity,
            unit_cost=item.unit_cost, reference_type="Transfer",
            reference_id=transfer.transfer_number, user=user,
        )
    transfer.status = StockTransfer.Status.RECEIVED
    transfer.received_by = user
    transfer.received_at = timezone.now()
    transfer.save()
    audit.log("transfer.received", business=transfer.business, user=user,
              request=request, module="inventory", obj=transfer,
              description=f"Transfer {transfer.transfer_number} received.")
    return transfer


@transaction.atomic
def cancel_transfer(*, transfer, user, membership=None, request=None):
    services.require_inventory_write(
        business=transfer.business,
        user=user,
        permission_code="inventory.transfer",
        membership=membership,
        request=request,
        warehouses=(transfer.from_warehouse, transfer.to_warehouse),
    )
    transfer = StockTransfer.objects.select_for_update().get(
        pk=transfer.pk, business=transfer.business
    )
    if transfer.status == StockTransfer.Status.DISPATCHED:
        # Return goods to source
        for item in transfer.items.select_related("product", "variant"):
            services.record_movement(
                business=transfer.business, warehouse=transfer.from_warehouse,
                product=item.product, variant=item.variant,
                movement_type="transfer_in", quantity=item.quantity,
                unit_cost=item.unit_cost, reference_type="TransferCancel",
                reference_id=transfer.transfer_number, user=user,
            )
    elif transfer.status == StockTransfer.Status.RECEIVED:
        raise ValidationError("Received transfers cannot be cancelled.")
    elif transfer.status not in (
        StockTransfer.Status.DRAFT,
        StockTransfer.Status.APPROVED,
    ):
        raise ValidationError("Transfer cannot be cancelled in its current status.")
    transfer.status = StockTransfer.Status.CANCELLED
    transfer.save(update_fields=["status", "updated_at"])
    audit.log("transfer.cancelled", business=transfer.business, user=user,
              request=request, module="inventory", obj=transfer,
              description=f"Transfer {transfer.transfer_number} cancelled.")
    return transfer


# ---------------------------------------------------------------------------
# Adjustments
# ---------------------------------------------------------------------------
ADJUST_MOVEMENT = {
    "damage": "damage", "expiry": "wastage", "loss": "adjust_out",
    "theft": "adjust_out", "wastage": "wastage", "internal": "internal",
    "sample": "internal", "count": "count", "data": "adjust_out", "other": "adjust_out",
}


@transaction.atomic
def create_adjustment(*, business, warehouse, reason, rows, user, notes="",
                      requires_approval=False, membership=None, request=None):
    services.require_inventory_write(
        business=business,
        user=user,
        permission_code="inventory.adjust",
        membership=membership,
        request=request,
        warehouses=(warehouse,),
        tenant_objects=tuple(
            obj
            for row in rows
            for obj in (row.get("product"), row.get("variant"))
            if obj is not None
        ),
    )
    _validate_warehouse(business, warehouse)
    rows = _lock_inventory_rows(
        business, rows, allow_parent_meter_repair=True
    )
    for row in rows:
        product = row["product"]
        if product.is_meter_tailoring and product.has_variants and row["variant"] is None:
            current = services.get_stock(business, warehouse, product, None)
            if current == 0 or D(row["quantity"]) != -current:
                raise ValidationError(
                    f"Legacy parent stock for {product.name} must be corrected "
                    "exactly to zero."
                )
    adjustment = StockAdjustment.objects.create(
        business=business,
        adjustment_number=_next_number(StockAdjustment, business,
                                       "adjustment_number", "ADJ"),
        warehouse=warehouse, reason=reason,
        status=(StockAdjustment.Status.PENDING if requires_approval
                else StockAdjustment.Status.APPROVED),
        notes=notes, created_by=user,
    )
    for row in rows:
        previous = services.get_stock(business, warehouse, row["product"], row["variant"])
        StockAdjustmentItem.objects.create(
            business=business, adjustment=adjustment,
            product=row["product"], variant=row["variant"],
            previous_quantity=previous, change=row["quantity"],
        )
    if adjustment.status == StockAdjustment.Status.APPROVED:
        _apply_adjustment(adjustment, user)
    else:
        from apps.notifications.services import notify_role

        notify_role(business, "inventory.adjust_approve",
                    f"Stock adjustment {adjustment.adjustment_number} needs approval",
                    severity="warning", category="adjustment_pending",
                    link="/inventory/adjustments/")
    audit.log("adjustment.created", business=business, user=user, request=request,
              module="inventory", obj=adjustment,
              description=f"Adjustment {adjustment.adjustment_number} "
                          f"({adjustment.get_reason_display()}) created.")
    return adjustment


def _apply_adjustment(adjustment, user):
    movement_type_out = ADJUST_MOVEMENT.get(adjustment.reason, "adjust_out")
    for item in adjustment.items.select_related("product", "variant"):
        if item.change == 0:
            continue
        mtype = "adjust_in" if item.change > 0 else movement_type_out
        target = item.variant or item.product
        services.record_movement(
            business=adjustment.business, warehouse=adjustment.warehouse,
            product=item.product, variant=item.variant,
            movement_type=mtype, quantity=item.change,
            unit_cost=D(getattr(target, "average_cost", 0)),
            reference_type="Adjustment", reference_id=adjustment.adjustment_number,
            user=user, notes=adjustment.notes[:300],
        )


@transaction.atomic
def approve_adjustment(*, adjustment, user, membership=None, request=None):
    services.require_inventory_write(
        business=adjustment.business,
        user=user,
        permission_code="inventory.adjust_approve",
        membership=membership,
        request=request,
        warehouses=(adjustment.warehouse,),
    )
    adjustment = StockAdjustment.objects.select_for_update().get(
        pk=adjustment.pk, business=adjustment.business
    )
    if adjustment.status != StockAdjustment.Status.PENDING:
        raise ValidationError("Only pending adjustments can be approved.")
    adjustment.status = StockAdjustment.Status.APPROVED
    adjustment.approved_by = user
    adjustment.save()
    _apply_adjustment(adjustment, user)
    audit.log("adjustment.approved", business=adjustment.business, user=user,
              request=request, module="inventory", obj=adjustment,
              description=f"Adjustment {adjustment.adjustment_number} approved.")
    return adjustment


@transaction.atomic
def reject_adjustment(*, adjustment, user, membership=None, request=None):
    services.require_inventory_write(
        business=adjustment.business,
        user=user,
        permission_code="inventory.adjust_approve",
        membership=membership,
        request=request,
        warehouses=(adjustment.warehouse,),
    )
    adjustment = StockAdjustment.objects.select_for_update().get(
        pk=adjustment.pk, business=adjustment.business
    )
    if adjustment.status != StockAdjustment.Status.PENDING:
        raise ValidationError("Only pending adjustments can be rejected.")
    adjustment.status = StockAdjustment.Status.REJECTED
    adjustment.approved_by = user
    adjustment.save()
    audit.log("adjustment.rejected", business=adjustment.business, user=user,
              request=request, module="inventory", obj=adjustment,
              description=f"Adjustment {adjustment.adjustment_number} rejected.")
    return adjustment


# ---------------------------------------------------------------------------
# Physical counts
# ---------------------------------------------------------------------------
@transaction.atomic
def start_count(
    *, business, warehouse, user, notes="", membership=None, request=None
):
    """Snapshot expected stock for every stocked product in the warehouse."""
    services.require_inventory_write(
        business=business,
        user=user,
        permission_code="inventory.count",
        membership=membership,
        request=request,
        warehouses=(warehouse,),
    )
    _validate_warehouse(business, warehouse)
    count = StockCount.objects.create(
        business=business,
        count_number=_next_number(StockCount, business, "count_number", "CNT"),
        warehouse=warehouse, created_by=user, notes=notes,
    )
    levels = StockLevel.objects.for_business(business).filter(
        warehouse=warehouse
    ).select_related("product", "variant")
    for level in levels:
        StockCountItem.objects.create(
            business=business, count=count, product=level.product,
            variant=level.variant, expected_quantity=level.quantity,
        )
    return count


@transaction.atomic
def approve_count(*, count, user, membership=None, request=None):
    services.require_inventory_write(
        business=count.business,
        user=user,
        permission_code="inventory.adjust_approve",
        membership=membership,
        request=request,
        warehouses=(count.warehouse,),
    )
    count = StockCount.objects.select_for_update().get(
        pk=count.pk, business=count.business
    )
    if count.status not in (StockCount.Status.OPEN, StockCount.Status.REVIEW):
        raise ValidationError("This count is not open.")
    corrections = 0
    for item in count.items.select_related("product", "variant"):
        if item.counted_quantity is None:
            continue
        variance = item.counted_quantity - item.expected_quantity
        if variance == 0:
            continue
        target = item.variant or item.product
        services.record_movement(
            business=count.business, warehouse=count.warehouse,
            product=item.product, variant=item.variant,
            movement_type="count", quantity=variance,
            unit_cost=D(getattr(target, "average_cost", 0)),
            reference_type="StockCount", reference_id=count.count_number,
            user=user, enforce_policy=False,
        )
        corrections += 1
    count.status = StockCount.Status.APPROVED
    count.approved_by = user
    count.save()
    audit.log("count.approved", business=count.business, user=user, request=request,
              module="inventory", obj=count,
              description=f"Stock count {count.count_number} approved with "
                          f"{corrections} corrections.")
    return count
