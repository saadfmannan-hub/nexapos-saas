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
from apps.subscriptions.access import AccessAction, require_actor_access

from .models import StockLevel, StockMovement

ZERO = Decimal("0")

INBOUND_AVERAGES_COST = {"opening", "purchase"}
NEGATIVE_ALLOWED_TYPES = {"count"}  # count corrections may set any value
METER_PARENT_REPAIR_TYPES = {
    "adjust_in", "adjust_out", "count", "damage", "wastage", "internal",
}


def _require_tailoring_product_write(
    *,
    access_context,
    business,
    user,
    product,
    permission_code,
    request=None,
):
    if product.is_tailoring_item and not access_context.has_module("tailoring"):
        require_actor_access(
            user,
            business,
            "tailoring",
            permission_code=permission_code,
            action=AccessAction.WRITE,
            membership=access_context.membership,
            request=request,
        )


class InsufficientStock(ValidationError):
    pass


def require_inventory_write(
    *,
    business,
    user,
    permission_code,
    membership=None,
    request=None,
    warehouses=(),
    tenant_objects=(),
):
    """Authorize an Inventory mutation and its warehouse scope centrally."""
    warehouses = tuple(warehouse for warehouse in warehouses if warehouse is not None)
    initial_context = require_actor_access(
        user,
        business,
        "inventory",
        permission_code=permission_code,
        action=AccessAction.WRITE,
        membership=membership,
        request=request,
    )
    allowed_ids = initial_context.membership.allowed_warehouse_ids
    warehouse_scope_allowed = all(
        warehouse.business_id == business.id
        and (allowed_ids is None or warehouse.id in allowed_ids)
        for warehouse in warehouses
    )
    tenant_scope_allowed = all(
        obj.business_id == business.id
        for obj in tenant_objects
        if obj is not None
    )
    scope_allowed = warehouse_scope_allowed and tenant_scope_allowed
    if not scope_allowed:
        require_actor_access(
            user,
            business,
            "inventory",
            permission_code=permission_code,
            action=AccessAction.WRITE,
            membership=initial_context.membership,
            request=request,
            scope_allowed=False,
        )
    return initial_context


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


def configured_shared_fabric_warehouse(business):
    """Return the active tenant-owned Workshop warehouse, when configured."""
    from apps.branches.models import Branch, Warehouse
    from apps.tenants.models import BusinessSettings

    warehouse_id = (
        BusinessSettings.objects.filter(business=business)
        .values_list("shared_fabric_warehouse_id", flat=True)
        .first()
    )
    if warehouse_id is None:
        return None
    return (
        Warehouse.objects.for_business(business)
        .select_related("branch")
        .filter(
            pk=warehouse_id,
            is_active=True,
            branch__business=business,
            branch__is_active=True,
            branch__usage_type=Branch.UsageType.WORKSHOP_STOCK,
        )
        .first()
    )


def stock_warehouse_for_sale_product(*, business, sale_warehouse, product):
    """Route Meter tailoring stock to Workshop; preserve legacy fallback."""
    if not product.is_meter_tailoring:
        return sale_warehouse
    shared_warehouse = configured_shared_fabric_warehouse(business)
    if shared_warehouse is not None:
        return shared_warehouse

    from apps.tenants.models import BusinessSettings

    if BusinessSettings.objects.filter(
        business=business,
        shared_fabric_warehouse__isnull=False,
    ).exists():
        raise ValidationError(
            "The configured Shared Fabric Location is unavailable."
        )
    return sale_warehouse


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

    # Coordinate Meter product-shape edits with every stock write.  The
    # locked/refreshed product prevents a concurrent edit from changing a
    # Meter parent between caller validation and the ledger write.
    product = (
        product.__class__.objects.select_for_update(of=("self",))
        .select_related("unit")
        .get(pk=product.pk, business=business)
    )
    if variant is not None:
        try:
            variant = variant.__class__.objects.select_for_update().get(
                pk=variant.pk,
                business=business,
                product=product,
            )
        except variant.__class__.DoesNotExist as exc:
            raise ValidationError("Variant does not belong to product.") from exc
    meter_variant_parent = bool(
        product.is_meter_tailoring
        and product.has_variants
        and variant is None
    )

    level, _ = StockLevel.objects.get_or_create(
        business=business, warehouse=warehouse, product=product, variant=variant,
        defaults={"quantity": ZERO},
    )
    # Lock the row (no-op on SQLite, real lock on PostgreSQL)
    level = StockLevel.objects.select_for_update().get(pk=level.pk)
    new_quantity = level.quantity + quantity

    if meter_variant_parent and not (
        level.quantity != 0
        and new_quantity == 0
        and movement_type in METER_PARENT_REPAIR_TYPES
    ):
        raise ValidationError(
            f"Select a variant/color for {product.name}. A legacy parent balance "
            "may only be corrected exactly to zero."
        )

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


@transaction.atomic
def set_opening_stock(
    *,
    business,
    warehouse,
    product,
    variant=None,
    quantity,
    unit_cost,
    user,
    membership=None,
    request=None,
):
    access_context = require_inventory_write(
        business=business,
        user=user,
        permission_code="inventory.adjust",
        membership=membership,
        request=request,
        warehouses=(warehouse,),
        tenant_objects=(product, variant),
    )
    from apps.catalog.models import Product, ProductVariant

    product = (
        Product.objects.select_for_update(of=("self",))
        .select_related("unit")
        .get(pk=product.pk, business=business)
    )
    if variant is not None:
        variant = ProductVariant.objects.select_for_update().get(
            pk=variant.pk,
            business=business,
            product=product,
        )
    _require_tailoring_product_write(
        access_context=access_context,
        business=business,
        user=user,
        product=product,
        permission_code="inventory.adjust",
        request=request,
    )
    return record_movement(
        business=business, warehouse=warehouse, product=product, variant=variant,
        movement_type="opening", quantity=quantity, unit_cost=unit_cost,
        reference_type="Opening", user=user,
    )


EXPORT_COLUMNS = [
    "Branch Code", "Branch Name", "Warehouse Code", "Warehouse Name",
    "SKU", "Barcode", "Product Name", "Variant SKU", "Variant Barcode",
    "Category",
    "Current Stock", "Reserved Stock", "Available Stock", "Minimum Stock Level",
    "Stock Value", "Unit Cost", "Last Purchase Price", "Last Selling Price",
    "Last Stock Movement Date", "Status",
]
IMPORT_COLUMNS = [
    "branch code", "branch name", "warehouse code", "warehouse name",
    "sku", "barcode", "product name", "variant sku", "variant barcode",
    "quantity", "minimum stock level",
    "adjustment type", "reason / notes", "unit cost",
]
IMPORT_MODES = {"add", "replace", "opening", "minimum"}


def inventory_export_dataset(
    business,
    filters,
    *,
    allowed_warehouse_ids=None,
    include_tailoring=True,
):
    """Build {columns, rows} for inventory export (one row per stock level).

    Reserved stock is always 0 (no reservation system in v1), so
    available == current — stated honestly rather than faked.
    """
    qs = (
        StockLevel.objects.for_business(business)
        .select_related("product", "variant", "warehouse", "warehouse__branch",
                        "product__category", "product__unit")
        .filter(product__is_archived=False)
    )
    if allowed_warehouse_ids is not None:
        qs = qs.filter(warehouse_id__in=allowed_warehouse_ids)
    if not include_tailoring:
        qs = qs.filter(product__is_tailoring_item=False)
    if filters.get("warehouse_id"):
        qs = qs.filter(warehouse_id=filters["warehouse_id"])
    if filters.get("branch_id"):
        qs = qs.filter(warehouse__branch_id=filters["branch_id"])

    # Last movement date per (product, warehouse) in one query
    last_moves = {}
    movement_qs = StockMovement.objects.for_business(business)
    if allowed_warehouse_ids is not None:
        movement_qs = movement_qs.filter(
            warehouse_id__in=allowed_warehouse_ids
        )
    for row in (
        movement_qs
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
            level.warehouse.branch.code if level.warehouse.branch else "",
            level.warehouse.branch.name if level.warehouse.branch else "",
            level.warehouse.code,
            level.warehouse.name,
            level.product.sku or "",
            level.product.barcode or "",
            str(level.variant or level.product),
            level.variant.sku if level.variant else "",
            level.variant.barcode if level.variant else "",
            level.product.category.name if level.product.category else "",
            level.quantity, ZERO, level.quantity,
            "" if level.product.is_meter_tailoring else level.product.reorder_level,
            money(level.quantity * cost), cost,
            level.product.purchase_price, level.product.sale_price,
            last.strftime("%Y-%m-%d") if last else "",
            "Active" if level.product.is_active else "Inactive",
        ])
    return {"columns": EXPORT_COLUMNS, "rows": rows, "totals": None}


@transaction.atomic
def import_inventory(
    *,
    business,
    rows,
    mode,
    user,
    selected_branch=None,
    selected_warehouse=None,
    allowed_warehouse_ids=None,
    membership=None,
    request=None,
):
    """Bulk stock import. mode in {add, replace, opening, minimum}.

    Returns (summary, errors). Each stock change flows through
    record_movement so the ledger stays the single source of truth.
    """
    access_context = require_inventory_write(
        business=business,
        user=user,
        permission_code="inventory.import",
        membership=membership,
        request=request,
    )
    membership_warehouse_ids = access_context.membership.allowed_warehouse_ids
    if membership_warehouse_ids is not None:
        if allowed_warehouse_ids is None:
            allowed_warehouse_ids = membership_warehouse_ids
        else:
            allowed_warehouse_ids = frozenset(allowed_warehouse_ids).intersection(
                membership_warehouse_ids
            )

    from apps.branches.models import Branch, Warehouse

    selected_branch = Branch.objects.select_for_update(no_key=True).filter(
        pk=getattr(selected_branch, "pk", None),
        business=business,
        is_active=True,
    ).first()
    selected_warehouse = Warehouse.objects.select_for_update(no_key=True).filter(
        pk=getattr(selected_warehouse, "pk", None),
        business=business,
        branch=selected_branch,
        is_active=True,
    ).first()
    if (
        selected_branch is None
        or selected_warehouse is None
        or not access_context.membership.can_access_branch(selected_branch)
        or (
            membership_warehouse_ids is not None
            and selected_warehouse.pk not in membership_warehouse_ids
        )
    ):
        require_actor_access(
            user,
            business,
            "inventory",
            permission_code="inventory.import",
            action=AccessAction.WRITE,
            membership=access_context.membership,
            request=request,
            scope_allowed=False,
        )

    from apps.catalog.models import Product, ProductVariant
    from apps.core.imports import normalize_row

    if mode not in IMPORT_MODES:
        raise ValidationError("Unknown import mode.")

    summary = {"imported": 0, "updated": 0, "skipped": 0, "failed": 0}
    errors = []
    seen = set()

    for idx, raw in enumerate(rows, start=2):
        r = normalize_row(raw)
        variant_sku = r.get("variant sku", "")
        variant_barcode = r.get("variant barcode", "")
        sku = r.get("sku", "")
        barcode = r.get("barcode", "")
        branch_code = r.get("branch code", "")
        branch_name = r.get("branch name", "")
        warehouse_code = r.get("warehouse code", "")
        warehouse_name = r.get("warehouse name", "")

        if (
            branch_code.casefold() != selected_branch.code.casefold()
            or branch_name.casefold() != selected_branch.name.casefold()
            or warehouse_code.casefold() != selected_warehouse.code.casefold()
            or warehouse_name.casefold() != selected_warehouse.name.casefold()
        ):
            errors.append((
                idx,
                "Branch and warehouse metadata must match the selected "
                f"context {selected_branch.code} / {selected_warehouse.code}.",
            ))
            summary["failed"] += 1
            continue

        if not any((sku, barcode, variant_sku, variant_barcode)):
            errors.append((idx, "Provide a SKU or barcode."))
            summary["failed"] += 1
            continue

        # Resolve every supplied identifier independently. Parent identifiers
        # and color identifiers may coexist, but all must point to the same
        # product and (when specified) the same exact variant.
        targets = []
        resolution_error = ""
        for label, value, field, variant_only in (
            ("Variant SKU", variant_sku, "sku", True),
            ("Variant barcode", variant_barcode, "barcode", True),
            ("SKU", sku, "sku", False),
            ("Barcode", barcode, "barcode", False),
        ):
            if not value:
                continue
            found_variant = ProductVariant.objects.for_business(business).filter(
                **{field: value}
            ).first()
            found_product = None
            if not variant_only:
                found_product = Product.objects.for_business(business).filter(
                    **{field: value}
                ).first()
            if found_variant is not None and found_product is not None:
                resolution_error = f"{label} is ambiguous: {value}"
                break
            if found_variant is not None:
                targets.append((found_variant.product_id, found_variant.id))
            elif found_product is not None:
                targets.append((found_product.id, None))
            else:
                resolution_error = f"{label} was not found: {value}"
                break
        product_ids = {product_id for product_id, _variant_id in targets}
        variant_ids = {
            variant_id for _product_id, variant_id in targets
            if variant_id is not None
        }
        if len(product_ids) > 1 or len(variant_ids) > 1:
            resolution_error = "Supplied product/color identifiers do not match."
        if resolution_error or not product_ids:
            errors.append((idx, resolution_error or "Product not found."))
            summary["failed"] += 1
            continue
        product = Product.objects.for_business(business).get(pk=product_ids.pop())
        variant = (
            ProductVariant.objects.for_business(business).get(pk=variant_ids.pop())
            if variant_ids
            else None
        )
        # Import is one transaction. Lock and refresh the product before
        # interpreting its Unit/type, and before a replace-mode stock read.
        # Every ledger writer takes the same lock, so an absolute replacement
        # cannot compute its delta from a concurrently changing stock value.
        product = (
            Product.objects.select_for_update()
            .select_related("unit")
            .get(pk=product.pk, business=business)
        )
        _require_tailoring_product_write(
            access_context=access_context,
            business=business,
            user=user,
            product=product,
            permission_code="inventory.import",
            request=request,
        )
        if variant is not None and (
            variant.business_id != business.id
            or variant.product_id != product.id
        ):
            errors.append((idx, "Invalid product variant for this business."))
            summary["failed"] += 1
            continue
        if product.is_meter_tailoring and product.has_variants and variant is None:
            errors.append((idx, f"Select a variant/color for {product.name}."))
            summary["failed"] += 1
            continue
        is_meter_product = product.is_meter_tailoring
        if is_meter_product and r.get("minimum stock level", "") != "":
            errors.append((
                idx,
                "Parent/per-variant reorder levels are not supported for Meter "
                "fabric in the current inventory model.",
            ))
            summary["failed"] += 1
            continue
        if is_meter_product and variant is None and mode == "opening":
            errors.append((
                idx,
                "Meter parent opening stock must be received through Purchases.",
            ))
            summary["failed"] += 1
            continue

        warehouse = selected_warehouse

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
            if not level.is_finite() or level.as_tuple().exponent < -3:
                errors.append((idx, "Minimum stock level supports up to 3 decimals."))
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
        if not quantity.is_finite() or quantity.as_tuple().exponent < -3:
            errors.append((idx, "Quantity supports up to 3 decimal places."))
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
            if (
                not unit_cost_override.is_finite()
                or unit_cost_override.as_tuple().exponent < -3
            ):
                errors.append((idx, "Unit cost supports up to 3 decimal places."))
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


def stock_value(
    business,
    warehouse=None,
    *,
    allowed_warehouse_ids=None,
    include_tailoring=True,
):
    """Total stock value at average cost."""
    qs = StockLevel.objects.for_business(business).filter(quantity__gt=0)
    if allowed_warehouse_ids is not None:
        qs = qs.filter(warehouse_id__in=allowed_warehouse_ids)
    if not include_tailoring:
        qs = qs.filter(product__is_tailoring_item=False)
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
