"""Inventory services — the ONLY way stock changes.

record_movement() writes a StockMovement and updates the StockLevel
cache atomically, enforcing the business's negative-stock policy and
maintaining the product's moving-average cost on inbound movements.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Sum

from apps.core.money import D, money, qty as q3

from .models import StockLevel, StockMovement

ZERO = Decimal("0")

INBOUND_AVERAGES_COST = {"opening", "purchase"}
NEGATIVE_ALLOWED_TYPES = {"count"}  # count corrections may set any value


class InsufficientStock(ValidationError):
    pass


def get_stock(business, warehouse, product, variant=None):
    level = StockLevel.objects.for_business(business).filter(
        warehouse=warehouse, product=product, variant=variant
    ).first()
    return level.quantity if level else ZERO


def total_stock(business, product, variant=None):
    agg = StockLevel.objects.for_business(business).filter(
        product=product, variant=variant
    ).aggregate(t=Sum("quantity"))
    return agg["t"] or ZERO


@transaction.atomic
def record_movement(
    *,
    business,
    warehouse,
    product,
    variant=None,
    movement_type,
    quantity,
    unit_cost=ZERO,
    reference_type="",
    reference_id="",
    user=None,
    notes="",
    enforce_policy=True,
):
    """Append a ledger entry and update cached stock. quantity sign:
    positive = stock in, negative = stock out."""
    quantity = q3(D(quantity))
    if quantity == 0:
        raise ValidationError("Stock movement quantity cannot be zero.")
    if warehouse.business_id != business.id or product.business_id != business.id:
        raise ValidationError("Cross-tenant stock movement blocked.")
    if variant is not None and variant.product_id != product.id:
        raise ValidationError("Variant does not belong to product.")

    level, _ = StockLevel.objects.get_or_create(
        business=business, warehouse=warehouse, product=product, variant=variant,
        defaults={"quantity": ZERO},
    )
    # Lock the row (no-op on SQLite, real lock on PostgreSQL)
    level = StockLevel.objects.select_for_update().get(pk=level.pk)
    new_quantity = level.quantity + quantity

    if (
        quantity < 0
        and new_quantity < 0
        and enforce_policy
        and movement_type not in NEGATIVE_ALLOWED_TYPES
    ):
        policy = business.settings.negative_stock_policy
        if policy == "block":
            item = variant or product
            raise InsufficientStock(
                f"Insufficient stock for {item}: available {level.quantity}, "
                f"requested {-quantity}."
            )

    level.quantity = new_quantity
    level.save(update_fields=["quantity"])

    movement = StockMovement.objects.create(
        business=business,
        warehouse=warehouse,
        product=product,
        variant=variant,
        movement_type=movement_type,
        quantity=quantity,
        unit_cost=money(unit_cost),
        balance_after=new_quantity,
        reference_type=reference_type,
        reference_id=str(reference_id)[:60],
        user=user,
        notes=notes[:300],
    )

    # Moving-average cost on inbound purchase/opening
    if movement_type in INBOUND_AVERAGES_COST and quantity > 0 and unit_cost:
        _update_average_cost(business, product, variant, quantity, D(unit_cost))

    # Low-stock alert
    if quantity < 0 and product.reorder_level > 0:
        total = total_stock(business, product, variant)
        if total <= product.reorder_level:
            _low_stock_alert(business, product, variant, total)

    return movement


def _update_average_cost(business, product, variant, in_qty, in_cost):
    target = variant or product
    existing_qty = total_stock(business, product, variant) - in_qty
    if existing_qty < 0:
        existing_qty = ZERO
    old_cost = D(target.average_cost)
    denom = existing_qty + in_qty
    if denom <= 0:
        new_avg = in_cost
    else:
        new_avg = (existing_qty * old_cost + in_qty * in_cost) / denom
    target.average_cost = money(new_avg)
    target.save(update_fields=["average_cost"])


def _low_stock_alert(business, product, variant, current):
    from apps.notifications.services import notify_role

    item = variant or product
    # Avoid alert storms: only one unread low-stock alert per product
    from apps.notifications.models import Notification

    exists = Notification.objects.for_business(business).filter(
        category="low_stock", is_read=False, body__contains=f"#{product.pk}#"
    ).exists()
    if exists or not business.settings.notify_low_stock:
        return
    notify_role(
        business, "inventory.view",
        f"Low stock: {item}",
        body=f"Current stock {current} is at or below the reorder level "
             f"({product.reorder_level}). Ref #{product.pk}#",
        severity="warning", category="low_stock",
        link="/inventory/stock/",
    )


def set_opening_stock(*, business, warehouse, product, variant=None, quantity, unit_cost, user=None):
    return record_movement(
        business=business, warehouse=warehouse, product=product, variant=variant,
        movement_type="opening", quantity=quantity, unit_cost=unit_cost,
        reference_type="Opening", user=user,
    )


def stock_value(business, warehouse=None):
    """Total stock value at average cost."""
    qs = StockLevel.objects.for_business(business).filter(quantity__gt=0)
    if warehouse is not None:
        qs = qs.filter(warehouse=warehouse)
    total = ZERO
    for level in qs.select_related("product", "variant"):
        target = level.variant or level.product
        cost = D(target.average_cost) or D(
            target.purchase_price if hasattr(target, "purchase_price") else 0
        )
        total += level.quantity * cost
    return money(total)
