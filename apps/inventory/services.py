"""Inventory services — the ONLY way stock changes.

record_movement() writes a StockMovement and updates the StockLevel
cache atomically, enforcing the business's negative-stock policy and
maintaining the product's moving-average cost on inbound movements.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Sum

from apps.core.money import D, money
from apps.core.money import qty as q3

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


EXPORT_COLUMNS = [
    "SKU", "Barcode", "Product Name", "Variant SKU", "Variant Barcode",
    "Category", "Branch", "Warehouse",
    "Current Stock", "Reserved Stock", "Available Stock", "Minimum Stock Level",
    "Stock Value", "Unit Cost", "Last Purchase Price", "Last Selling Price",
    "Last Stock Movement Date", "Status",
]
IMPORT_COLUMNS = [
    "sku", "barcode", "product name", "variant sku", "variant barcode",
    "branch", "warehouse", "quantity", "minimum stock level",
    "adjustment type", "reason / notes", "unit cost",
]
IMPORT_MODES = {"add", "replace", "opening", "minimum"}


def inventory_export_dataset(business, filters):
    """Build {columns, rows} for inventory export (one row per stock level).

    Reserved stock is always 0 (no reservation system in v1), so
    available == current — stated honestly rather than faked.
    """
    qs = (
        StockLevel.objects.for_business(business)
        .select_related("product", "variant", "warehouse", "warehouse__branch",
                        "product__category")
        .filter(product__is_archived=False)
    )
    if filters.get("warehouse_id"):
        qs = qs.filter(warehouse_id=filters["warehouse_id"])
    if filters.get("branch_id"):
        qs = qs.filter(warehouse__branch_id=filters["branch_id"])

    # Last movement date per (product, warehouse) in one query
    last_moves = {}
    for row in (
        StockMovement.objects.for_business(business)
        .values("product_id", "warehouse_id")
        .annotate(last=Max("created_at"))
    ):
        last_moves[(row["product_id"], row["warehouse_id"])] = row["last"]

    rows = []
    for level in qs.order_by("product__name"):
        target = level.variant or level.product
        cost = D(target.average_cost) or D(getattr(target, "purchase_price", 0))
        last = last_moves.get((level.product_id, level.warehouse_id))
        rows.append([
            level.product.sku or "",
            level.product.barcode or "",
            str(level.variant or level.product),
            level.variant.sku if level.variant else "",
            level.variant.barcode if level.variant else "",
            level.product.category.name if level.product.category else "",
            level.warehouse.branch.name if level.warehouse.branch else "",
            level.warehouse.name,
            level.quantity, ZERO, level.quantity,
            level.product.reorder_level,
            money(level.quantity * cost), cost,
            level.product.purchase_price, level.product.sale_price,
            last.strftime("%Y-%m-%d") if last else "",
            "Active" if level.product.is_active else "Inactive",
        ])
    return {"columns": EXPORT_COLUMNS, "rows": rows, "totals": None}


@transaction.atomic
def import_inventory(*, business, rows, mode, user):
    """Bulk stock import. mode in {add, replace, opening, minimum}.

    Returns (summary, errors). Each stock change flows through
    record_movement so the ledger stays the single source of truth.
    """
    from apps.branches.models import Warehouse
    from apps.catalog.models import Product, ProductVariant
    from apps.core.imports import normalize_row

    if mode not in IMPORT_MODES:
        raise ValidationError("Unknown import mode.")

    summary = {"imported": 0, "updated": 0, "skipped": 0, "failed": 0}
    errors = []
    seen = set()

    for idx, raw in enumerate(rows, start=2):
        r = normalize_row(raw)
        sku = r.get("variant sku") or r.get("sku", "")
        barcode = r.get("variant barcode") or r.get("barcode", "")
        wh_name = r.get("warehouse", "")
        branch_name = r.get("branch", "")

        if not sku and not barcode:
            errors.append((idx, "Provide a SKU or barcode."))
            summary["failed"] += 1
            continue

        # Resolve product / variant
        variant = None
        product = None
        if sku:
            variant = ProductVariant.objects.for_business(business).filter(sku=sku).first()
            if variant:
                product = variant.product
            else:
                product = Product.objects.for_business(business).filter(sku=sku).first()
        if product is None and barcode:
            variant = ProductVariant.objects.for_business(business).filter(
                barcode=barcode).first()
            if variant:
                product = variant.product
            else:
                product = Product.objects.for_business(business).filter(
                    barcode=barcode).first()
        if product is None:
            errors.append((idx, f"Product not found (sku={sku!r}, barcode={barcode!r})."))
            summary["failed"] += 1
            continue

        # Resolve warehouse (by name, optionally within branch)
        wh_qs = Warehouse.objects.for_business(business).filter(is_active=True)
        if branch_name:
            wh_qs = wh_qs.filter(branch__name__iexact=branch_name)
            if not wh_qs.exists():
                errors.append((idx, f"Branch not found: {branch_name}"))
                summary["failed"] += 1
                continue
        if wh_name:
            warehouse = wh_qs.filter(name__iexact=wh_name).first()
        else:
            warehouse = wh_qs.filter(is_default=True).first() or wh_qs.first()
        if warehouse is None:
            errors.append((idx, f"Warehouse not found: {wh_name or '(default)'}"))
            summary["failed"] += 1
            continue

        # In-file duplicate guard
        key = (product.id, variant.id if variant else None, warehouse.id)
        if key in seen:
            errors.append((idx, "Duplicate product/warehouse row in file."))
            summary["failed"] += 1
            continue
        seen.add(key)

        notes = (
            f"{r.get('adjustment type', '')} "
            f"{r.get('reason / notes') or r.get('notes', '')}"
        ).strip() or \
            f"Bulk import ({mode})"

        # Minimum-stock-only mode: no ledger movement
        if mode == "minimum":
            from decimal import Decimal as _Dec
            from decimal import InvalidOperation as _InvOp

            min_raw = r.get("minimum stock level", "")
            if min_raw == "":
                errors.append((idx, "Minimum stock level is required for this mode."))
                summary["failed"] += 1
                continue
            try:
                level = _Dec(str(min_raw))
            except (_InvOp, ValueError):
                errors.append((idx, f"Invalid minimum stock level: {min_raw}"))
                summary["failed"] += 1
                continue
            if level < 0:
                errors.append((idx, "Minimum stock level cannot be negative."))
                summary["failed"] += 1
                continue
            product.reorder_level = level
            product.save(update_fields=["reorder_level", "updated_at"])
            summary["updated"] += 1
            continue

        # Quantity-based modes — strict numeric validation (D() would
        # silently coerce "abc" to 0, so parse explicitly here)
        from decimal import Decimal as _Dec
        from decimal import InvalidOperation as _InvOp

        qty_raw = r.get("quantity", "")
        if qty_raw == "":
            errors.append((idx, "Quantity is required for this mode."))
            summary["failed"] += 1
            continue
        try:
            quantity = _Dec(str(qty_raw))
        except (_InvOp, ValueError):
            errors.append((idx, f"Invalid quantity: {qty_raw}"))
            summary["failed"] += 1
            continue
        if quantity < 0:
            errors.append((idx, f"Quantity cannot be negative: {qty_raw}"))
            summary["failed"] += 1
            continue
        unit_cost_override = None
        if r.get("unit cost", "") != "":
            try:
                unit_cost_override = _Dec(str(r["unit cost"]))
            except (_InvOp, ValueError):
                errors.append((idx, f"Invalid unit cost: {r['unit cost']}"))
                summary["failed"] += 1
                continue
            if unit_cost_override < 0:
                errors.append((idx, "Unit cost cannot be negative."))
                summary["failed"] += 1
                continue

        if not product.is_stocked:
            errors.append((idx, f"{product.name} does not track inventory."))
            summary["failed"] += 1
            continue

        try:
            current = get_stock(business, warehouse, product, variant)
            unit_cost = D(unit_cost_override) or \
                D(getattr(variant or product, "average_cost", 0)) or \
                D(getattr(variant or product, "purchase_price", 0))
            if mode == "add":
                if quantity > 0:
                    record_movement(
                        business=business, warehouse=warehouse, product=product,
                        variant=variant, movement_type="adjust_in",
                        quantity=quantity, unit_cost=unit_cost,
                        reference_type="Import", user=user, notes=notes)
            elif mode == "opening":
                if quantity > 0:
                    record_movement(
                        business=business, warehouse=warehouse, product=product,
                        variant=variant, movement_type="opening",
                        quantity=quantity, unit_cost=unit_cost,
                        reference_type="Import", user=user, notes=notes)
            elif mode == "replace":
                delta = quantity - current
                if delta != 0:
                    record_movement(
                        business=business, warehouse=warehouse, product=product,
                        variant=variant, movement_type="count",
                        quantity=delta, unit_cost=unit_cost,
                        reference_type="Import", user=user, notes=notes,
                        enforce_policy=False)
            # Optional minimum update alongside quantity
            min_raw = r.get("minimum stock level", "")
            if min_raw != "":
                try:
                    product.reorder_level = D(min_raw)
                    product.save(update_fields=["reorder_level", "updated_at"])
                except Exception:
                    pass
            summary["imported"] += 1
        except (ValidationError, Exception) as exc:
            msg = "; ".join(getattr(exc, "messages", [str(exc)]))
            errors.append((idx, msg))
            summary["failed"] += 1
            continue

    return summary, errors


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
