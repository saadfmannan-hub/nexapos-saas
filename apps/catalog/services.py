"""Catalog defaults and helpers."""
import re
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Brand, Category, Product, ProductVariant, TaxRate, Unit


def validate_meter_product_shape(
    product, *, target_unit, target_type, target_tailoring=None
):
    """Block unsafe Meter workflow changes once stock or transactions exist."""
    if not product.pk:
        return
    old_meter = bool(product.unit_id and product.unit.is_meter)
    new_meter = bool(target_unit and target_unit.is_meter)
    if not old_meter and not new_meter:
        return
    type_changed = target_type != product.product_type
    meter_semantic_changed = old_meter != new_meter
    old_meter_workflow = old_meter and product.is_tailoring_item
    new_meter_workflow = new_meter and (
        product.is_tailoring_item
        if target_tailoring is None
        else bool(target_tailoring)
    )
    meter_workflow_changed = old_meter_workflow != new_meter_workflow

    has_history = bool(product_history_refs(product)) or (
        product.stock_levels.exclude(quantity=0).exists()
    )
    has_variants = product.variants.exists()
    if (
        type_changed or meter_semantic_changed or meter_workflow_changed
    ) and (has_history or has_variants):
        raise ValidationError(
            "The Meter workflow, unit, or product type cannot be changed after "
            "variants, stock movements, purchases, or sales exist."
        )

    if new_meter_workflow and target_type == Product.Type.VARIANT:
        if product.stock_levels.filter(variant__isnull=True).exclude(quantity=0).exists():
            raise ValidationError(
                "Move or adjust existing parent stock to zero before using "
                "Meter color variants."
            )
    elif new_meter_workflow and has_variants:
        raise ValidationError(
            "Remove unused variants before changing this Meter product to a "
            "non-variant type."
        )


def sku_prefix_for(business):
    """Per-business auto-SKU prefix: first 3 alphanumeric chars of the
    business name, uppercased. Falls back to 'SKU' when the name has none."""
    letters = re.sub(r"[^A-Za-z0-9]", "", business.name or "")[:3].upper()
    return letters or "SKU"


def generate_sku(business, prefix=None, *, taken=None):
    """Return the next free ``PREFIX-000001`` style SKU for a business.

    Scans existing product and variant SKUs (and any ``taken`` set of SKUs
    being assigned in the same request) so generated codes never collide
    within the tenant. Optional ``taken`` lets a caller reserve SKUs across
    several variants created together before they hit the database.
    """
    prefix = prefix or sku_prefix_for(business)
    taken = taken if taken is not None else set()
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)$")

    highest = 0
    skus = list(
        Product.objects.for_business(business)
        .exclude(sku="").values_list("sku", flat=True)
    ) + list(
        ProductVariant.objects.for_business(business)
        .exclude(sku="").values_list("sku", flat=True)
    ) + list(taken)
    for sku in skus:
        match = pattern.match(sku or "")
        if match:
            highest = max(highest, int(match.group(1)))

    candidate_n = highest + 1
    existing = set(skus)
    while True:
        candidate = f"{prefix}-{candidate_n:06d}"
        if candidate not in existing:
            return candidate
        candidate_n += 1


class ProductInUse(Exception):
    """Raised when a hard delete is attempted on a product with history."""


def product_history_refs(product):
    """Names of transaction types referencing this product (empty = safe
    to hard-delete). Checks sales, purchases, stock ledger, transfers,
    adjustments and counts."""
    from apps.inventory.models import (
        StockAdjustmentItem,
        StockCountItem,
        StockMovement,
        StockTransferItem,
    )
    from apps.purchases.models import PurchaseItem
    from apps.sales.models import SaleItem

    refs = []
    if SaleItem.objects.filter(product=product).exists():
        refs.append("sales")
    if PurchaseItem.objects.filter(product=product).exists():
        refs.append("purchases")
    if StockMovement.objects.filter(product=product).exists():
        refs.append("stock movements")
    if StockTransferItem.objects.filter(product=product).exists():
        refs.append("transfers")
    if StockAdjustmentItem.objects.filter(product=product).exists():
        refs.append("adjustments")
    if StockCountItem.objects.filter(product=product).exists():
        refs.append("stock counts")
    return refs


@transaction.atomic
def delete_product_if_safe(product):
    """Hard-delete a product that has NEVER been used in any transaction.
    Products with history must be archived instead (they stay on old
    invoices and reports)."""
    refs = product_history_refs(product)
    if refs:
        raise ProductInUse(
            "This product appears in " + ", ".join(refs) +
            " and cannot be deleted. Archive it instead — it stays on "
            "historical invoices and reports."
        )
    product.delete()


def restore_product(product):
    product.is_archived = False
    product.is_active = True
    product.save(update_fields=["is_archived", "is_active", "updated_at"])
    return product


EXPORT_COLUMNS = [
    "Product Name", "SKU", "Barcode", "Category", "Brand", "Product Type", "Unit",
    "Purchase Price", "Sale Price", "Cost Price", "Tax/VAT Rate",
    "Tax Inclusive", "Track Inventory", "Current Stock", "Minimum Stock",
    "Warehouse", "Branch", "Variant Parent", "Variant Name", "Variant SKU",
    "Variant Barcode", "Status", "Created Date", "Updated Date",
]
IMPORT_COLUMNS = [
    "product name", "sku", "barcode", "category", "brand", "product type", "unit",
    "purchase price", "sale price", "cost price", "tax/vat rate",
    "tax inclusive", "track inventory", "opening stock", "minimum stock",
    "branch", "warehouse", "variant parent", "variant option name",
    "variant option value", "variant name", "variant sku", "variant barcode",
    "active", "archived",
]
PRODUCT_TYPE_ALIASES = {
    "": Product.Type.STANDARD,
    "standard": Product.Type.STANDARD,
    "product": Product.Type.STANDARD,
    "variant": Product.Type.VARIANT,
    "variants": Product.Type.VARIANT,
    "service": Product.Type.SERVICE,
    "non_stock": Product.Type.NON_STOCK,
    "non-stock": Product.Type.NON_STOCK,
    "non stock": Product.Type.NON_STOCK,
}


def _as_bool(value, default=False):
    raw = str(value or "").strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "y", "active", "enabled")


def _as_decimal(
    value, *, row_no, field, required=False, minimum=None, decimal_places=3,
):
    raw = str(value or "").strip()
    if raw == "":
        if required:
            raise ValueError(f"{field} is required.")
        return Decimal("0")
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be numeric.") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be numeric.")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field} cannot be below {minimum}.")
    if decimal_places is not None and parsed.as_tuple().exponent < -decimal_places:
        raise ValueError(
            f"{field} supports up to {decimal_places} decimal places."
        )
    return parsed


def _resolve_tax_rate(business, raw):
    value = str(raw or "").strip()
    if not value:
        return None
    existing = TaxRate.objects.for_business(business).filter(name__iexact=value).first()
    if existing:
        return existing
    try:
        rate = Decimal(value.rstrip("%"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Tax/VAT rate must be a tax name or numeric percentage: {value}") from exc
    tax, _ = TaxRate.objects.get_or_create(
        business=business,
        name=f"VAT {rate}%",
        defaults={"rate": rate, "is_active": True},
    )
    return tax


def _resolve_warehouse(
    business, branch_name, warehouse_name, *, allowed_warehouse_ids=None
):
    from apps.branches.models import Warehouse

    wh_qs = Warehouse.objects.for_business(business).filter(is_active=True)
    if allowed_warehouse_ids is not None:
        wh_qs = wh_qs.filter(pk__in=allowed_warehouse_ids)
    if branch_name:
        wh_qs = wh_qs.filter(branch__name__iexact=branch_name)
        if not wh_qs.exists():
            raise ValueError(f"Branch not found for this business: {branch_name}")
    if warehouse_name:
        warehouse = wh_qs.filter(name__iexact=warehouse_name).first()
        if warehouse is None:
            raise ValueError(f"Warehouse not found for this business: {warehouse_name}")
        return warehouse
    return wh_qs.filter(is_default=True).first() or wh_qs.first()


def _duplicate_code_exists(business, *, sku="", barcode="", exclude_product=None):
    product_qs = Product.objects.for_business(business)
    variant_qs = ProductVariant.objects.for_business(business)
    if exclude_product is not None:
        product_qs = product_qs.exclude(pk=exclude_product.pk)
        variant_qs = variant_qs.exclude(product=exclude_product)
    if sku and (product_qs.filter(sku=sku).exists() or variant_qs.filter(sku=sku).exists()):
        raise ValueError(f"Duplicate SKU within this business: {sku}")
    if barcode and (
        product_qs.filter(barcode=barcode).exists()
        or variant_qs.filter(barcode=barcode).exists()
    ):
        raise ValueError(f"Duplicate barcode within this business: {barcode}")


def product_export_dataset(business, filters):
    """Build {columns, rows} for product export.

    Scales to large catalogs: stock is fetched in a single aggregated
    query and joined in memory rather than per-row.
    """
    from django.db.models import Sum

    from apps.inventory.models import StockLevel

    qs = Product.objects.for_business(business).select_related(
        "category", "brand", "unit")

    if filters.get("category_id"):
        qs = qs.filter(category_id=filters["category_id"])
    if filters.get("brand_id"):
        qs = qs.filter(brand_id=filters["brand_id"])
    status = filters.get("status", "")
    if status == "archived":
        qs = qs.filter(is_archived=True)
    elif status == "active":
        qs = qs.filter(is_active=True, is_archived=False)
    else:
        qs = qs.filter(is_archived=False)

    # Stock map, optionally scoped to a warehouse/branch
    level_qs = StockLevel.objects.for_business(business)
    warehouse = None
    branch_name = ""
    if filters.get("warehouse_id"):
        level_qs = level_qs.filter(warehouse_id=filters["warehouse_id"])
    if filters.get("branch_id"):
        level_qs = level_qs.filter(warehouse__branch_id=filters["branch_id"])
    stock_map = {
        row["product_id"]: row["q"]
        for row in level_qs.values("product_id").annotate(q=Sum("quantity"))
    }
    if filters.get("warehouse_id"):
        from apps.branches.models import Warehouse

        warehouse = Warehouse.objects.for_business(business).filter(
            id=filters["warehouse_id"]).select_related("branch").first()
        if warehouse:
            branch_name = warehouse.branch.name if warehouse.branch else ""

    rows = []
    for p in qs.prefetch_related("variants").order_by("name"):
        stock = stock_map.get(p.id, 0)
        if status == "low" and not (p.reorder_level > 0 and stock <= p.reorder_level):
            continue
        if status == "out" and stock > 0:
            continue
        rows.append([
            p.name, p.sku, p.barcode,
            p.category.name if p.category else "",
            p.brand.name if p.brand else "",
            p.product_type,
            p.unit.name if p.unit else "",
            p.purchase_price, p.sale_price, p.average_cost,
            p.tax_rate.rate if p.tax_rate else "",
            "" if p.price_includes_tax is None else ("Yes" if p.price_includes_tax else "No"),
            "Yes" if p.track_inventory else "No",
            stock, p.reorder_level,
            warehouse.name if warehouse else "All",
            branch_name or "All",
            "", "", "", "",
            "Archived" if p.is_archived else ("Active" if p.is_active else "Inactive"),
            p.created_at.strftime("%Y-%m-%d"),
            p.updated_at.strftime("%Y-%m-%d"),
        ])
        for variant in p.variants.all():
            rows.append([
                p.name, p.sku, p.barcode,
                p.category.name if p.category else "",
                p.brand.name if p.brand else "",
                p.product_type,
                p.unit.name if p.unit else "",
                variant.purchase_price, variant.sale_price, variant.average_cost,
                p.tax_rate.rate if p.tax_rate else "",
                "" if p.price_includes_tax is None else ("Yes" if p.price_includes_tax else "No"),
                "Yes" if p.track_inventory else "No",
                "", p.reorder_level,
                warehouse.name if warehouse else "All",
                branch_name or "All",
                p.sku or p.name, variant.name, variant.sku, variant.barcode,
                "Active" if variant.is_active else "Inactive",
                variant.created_at.strftime("%Y-%m-%d"),
                variant.updated_at.strftime("%Y-%m-%d"),
            ])
    return {"columns": EXPORT_COLUMNS, "rows": rows, "totals": None}


def import_products(
    *, business, rows, match_by, user, allowed_warehouse_ids=None
):
    """Import products and variants with row-level error reporting.

    Matching remains backward compatible with the old form choices:
    ``sku``, ``barcode`` or ``name``. Returns (summary, errors).
    """
    from apps.core.imports import normalize_row
    from apps.inventory import services as inventory
    from apps.subscriptions import services as subscriptions

    if match_by not in ("sku", "barcode", "name"):
        raise ValidationError("Unknown product match field.")

    summary = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
    errors = []
    seen_skus, seen_barcodes = set(), set()

    for idx, raw in enumerate(rows, start=2):
        r = normalize_row(raw)
        try:
            name = r.get("product name") or r.get("name") or ""
            sku = r.get("sku", "")
            barcode = r.get("barcode", "")
            variant_name = r.get("variant name", "")
            option_name = r.get("variant option name", "")
            option_value = r.get("variant option value", "")
            variant_sku = r.get("variant sku", "")
            variant_barcode = r.get("variant barcode", "")
            is_variant_row = bool(
                r.get("variant parent") or variant_name or option_value
                or variant_sku or variant_barcode
            )

            if not name and not r.get("variant parent"):
                raise ValueError("Product name is required.")
            identifiers = [
                (variant_sku, seen_skus, "Variant SKU"),
                (variant_barcode, seen_barcodes, "Variant barcode"),
            ]
            if not is_variant_row:
                identifiers.extend([
                    (sku, seen_skus, "SKU"),
                    (barcode, seen_barcodes, "Barcode"),
                ])
            for code, seen, label in identifiers:
                if code and code in seen:
                    raise ValueError(f"{label} is repeated in this file: {code}")
                if code:
                    seen.add(code)

            product_type = PRODUCT_TYPE_ALIASES.get(
                str(r.get("product type", "")).strip().lower()
            )
            if product_type is None:
                raise ValueError(f"Unknown product type: {r.get('product type')}")
            if is_variant_row:
                product_type = Product.Type.VARIANT

            purchase_price = _as_decimal(
                r.get("purchase price") or r.get("purchase_price"),
                row_no=idx, field="purchase price", minimum=Decimal("0"),
            )
            sale_price = _as_decimal(
                r.get("sale price") or r.get("sale_price"),
                row_no=idx, field="sale price", minimum=Decimal("0"),
            )
            cost_price = _as_decimal(
                r.get("cost price"), row_no=idx, field="cost price",
                minimum=Decimal("0"),
            )
            reorder_level = _as_decimal(
                r.get("minimum stock") or r.get("minimum stock level")
                or r.get("reorder_level"),
                row_no=idx, field="minimum stock", minimum=Decimal("0"),
            )
            opening_stock = _as_decimal(
                r.get("opening stock") or r.get("opening_stock"),
                row_no=idx, field="opening stock", minimum=Decimal("0"),
            )

            parent_lookup = r.get("variant parent", "")
            existing = None
            if is_variant_row:
                existing = (
                    Product.objects.for_business(business)
                    .filter(sku=parent_lookup)
                    .first()
                    if parent_lookup
                    else None
                )
                if existing is None and parent_lookup:
                    existing = Product.objects.for_business(business).filter(
                        name__iexact=parent_lookup
                    ).first()
                if existing is None and name:
                    existing = Product.objects.for_business(business).filter(
                        name__iexact=name
                    ).first()
            elif match_by == "sku" and sku:
                existing = Product.objects.for_business(business).filter(sku=sku).first()
            elif match_by == "barcode" and barcode:
                existing = Product.objects.for_business(business).filter(
                    barcode=barcode
                ).first()
            elif match_by == "name" and name:
                existing = Product.objects.for_business(business).filter(
                    name__iexact=name
                ).first()

            if existing and not is_variant_row:
                summary["skipped"] += 1
                continue

            _current, limit, allowed = subscriptions.limit_state(business, "products")
            if not existing and not allowed:
                raise ValueError(f"Plan product limit ({limit}) reached.")

            warehouse = _resolve_warehouse(
                business,
                r.get("branch", ""),
                r.get("warehouse", ""),
                allowed_warehouse_ids=allowed_warehouse_ids,
            )
            category = None
            if r.get("category"):
                category, _ = Category.objects.get_or_create(
                    business=business, name=r["category"][:120], parent=None
                )
            brand = None
            if r.get("brand"):
                brand, _ = Brand.objects.get_or_create(
                    business=business, name=r["brand"][:120]
                )
            unit = None
            if r.get("unit"):
                unit = Unit.objects.for_business(business).filter(
                    name__iexact=r["unit"]
                ).first()
                if unit is None:
                    raise ValueError(f"Unit not found: {r['unit']}")
            if (
                is_variant_row
                and existing is not None
                and unit is not None
                and unit.pk != existing.unit_id
            ):
                raise ValueError("Variant unit must match its parent product unit.")
            effective_unit = (
                existing.unit
                if is_variant_row and existing is not None
                else unit or (existing.unit if existing is not None else None)
            )
            expected_parent_unit_id = (
                existing.unit_id
                if is_variant_row and existing is not None
                else None
            )
            expected_parent_is_meter = bool(
                is_variant_row
                and existing is not None
                and effective_unit
                and effective_unit.is_meter
            )
            expected_parent_meter_workflow = bool(
                is_variant_row
                and existing is not None
                and existing.is_meter_tailoring
            )
            is_meter = bool(
                existing.is_meter_tailoring
                if is_variant_row and existing is not None
                else effective_unit and effective_unit.is_meter
            )
            tax_rate = None
            if not is_meter:
                tax_rate = _resolve_tax_rate(
                    business,
                    r.get("tax/vat rate") or r.get("tax rate") or r.get("vat"),
                )
            if is_meter:
                if product_type not in (Product.Type.STANDARD, Product.Type.VARIANT):
                    raise ValueError("Meter tailoring products must track inventory.")
                # Customer pricing and parent thresholds are deliberately not
                # active for fabric inventory.  The garment charge is entered
                # per locked POS line; purchase price remains the meter cost.
                sale_price = Decimal("0")
                reorder_level = Decimal("0")
                tax_rate = None
                if not is_variant_row and opening_stock > 0:
                    raise ValueError(
                        "Meter opening stock must be entered for a variant/color "
                        "or received through a purchase."
                    )
                if is_variant_row and opening_stock > 0 and warehouse is None:
                    raise ValueError(
                        "Select a warehouse for Meter variant opening stock."
                    )

            with transaction.atomic():
                if is_variant_row:
                    product = existing
                    if product is not None:
                        product = (
                            Product.objects.select_for_update()
                            .select_related("unit")
                            .get(pk=product.pk, business=business)
                        )
                        if (
                            product.unit_id != expected_parent_unit_id
                            or bool(product.unit and product.unit.is_meter)
                            != expected_parent_is_meter
                            or product.is_meter_tailoring
                            != expected_parent_meter_workflow
                        ):
                            raise ValueError(
                                "The parent product unit changed during import. "
                                "Retry this row."
                            )
                    if product is None:
                        _duplicate_code_exists(business, sku=sku, barcode=barcode)
                        product = Product.objects.create(
                            business=business, name=name[:200],
                            sku=sku[:60], barcode=barcode[:80],
                            category=category, brand=brand, unit=unit,
                            product_type=Product.Type.VARIANT,
                            purchase_price=purchase_price,
                            sale_price=sale_price,
                            average_cost=cost_price,
                            tax_rate=tax_rate,
                            price_includes_tax=(
                                None
                                if is_meter
                                else (
                                    _as_bool(r.get("tax inclusive"))
                                    if r.get("tax inclusive", "") != ""
                                    else None
                                )
                            ),
                            reorder_level=reorder_level,
                            track_inventory=(
                                True if is_meter
                                else _as_bool(r.get("track inventory"), True)
                            ),
                            allow_discount=not is_meter,
                            is_tailoring_item=is_meter,
                            is_active=_as_bool(r.get("active"), True),
                            is_archived=_as_bool(r.get("archived"), False),
                        )
                        summary["created"] += 1
                    else:
                        update_fields = ["product_type", "updated_at"]
                        if product.unit_id and product.unit.is_meter:
                            validate_meter_product_shape(
                                product,
                                target_unit=product.unit,
                                target_type=Product.Type.VARIANT,
                                target_tailoring=product.is_tailoring_item,
                            )
                            if product.is_meter_tailoring:
                                product.is_tailoring_item = True
                                product.track_inventory = True
                                update_fields.extend([
                                    "is_tailoring_item", "track_inventory",
                                ])
                        product.product_type = Product.Type.VARIANT
                        product.save(update_fields=update_fields)

                    _duplicate_code_exists(
                        business, sku=variant_sku, barcode=variant_barcode,
                    )
                    attributes = {}
                    if option_name and option_value:
                        attributes[option_name] = option_value
                    display_name = (
                        variant_name or option_value or "Variant"
                    )[:160]
                    variant = ProductVariant.objects.create(
                        business=business, product=product, name=display_name,
                        attributes=attributes, sku=variant_sku[:60],
                        barcode=variant_barcode[:80],
                        purchase_price=purchase_price,
                        sale_price=sale_price,
                        average_cost=cost_price,
                        is_active=_as_bool(r.get("active"), True),
                    )
                    if opening_stock > 0 and warehouse and product.is_stocked:
                        inventory.set_opening_stock(
                            business=business, warehouse=warehouse, product=product,
                            variant=variant, quantity=opening_stock,
                            unit_cost=purchase_price, user=user,
                        )
                    summary["created"] += 1
                    continue

                _duplicate_code_exists(business, sku=sku, barcode=barcode)
                product = Product.objects.create(
                    business=business, name=name[:200], sku=sku[:60],
                    barcode=barcode[:80], category=category, brand=brand, unit=unit,
                    product_type=product_type, purchase_price=purchase_price,
                    sale_price=sale_price, average_cost=cost_price,
                    tax_rate=tax_rate,
                    price_includes_tax=(
                        None
                        if is_meter
                        else (
                            _as_bool(r.get("tax inclusive"))
                            if r.get("tax inclusive", "") != "" else None
                        )
                    ),
                    reorder_level=reorder_level,
                    track_inventory=(
                        True if is_meter else _as_bool(
                            r.get("track inventory"),
                            product_type in (Product.Type.STANDARD, Product.Type.VARIANT),
                        )
                    ),
                    allow_discount=not is_meter,
                    is_tailoring_item=is_meter,
                    is_active=_as_bool(r.get("active"), True),
                    is_archived=_as_bool(r.get("archived"), False),
                )
                if opening_stock > 0 and warehouse and product.is_stocked:
                    inventory.set_opening_stock(
                        business=business, warehouse=warehouse, product=product,
                        quantity=opening_stock, unit_cost=purchase_price, user=user,
                    )
                summary["created"] += 1
        except Exception as exc:
            errors.append((idx, str(exc)))
            summary["failed"] += 1
    return summary, errors


DEFAULT_UNITS = [
    ("Piece", "pc", False),
    ("Box", "box", False),
    ("Pack", "pack", False),
    ("Set", "set", False),
    ("Kilogram", "kg", True),
    ("Gram", "g", True),
    ("Liter", "L", True),
    ("Milliliter", "ml", True),
    ("Meter", "m", True),
    ("Hour", "hr", True),
    ("Service", "svc", False),
]


def create_default_catalog(business):
    for name, abbr, dec in DEFAULT_UNITS:
        unit, _created = Unit.objects.get_or_create(
            business=business, name=name,
            defaults={
                "abbreviation": abbr,
                "allow_decimal": dec,
                "is_meter": name == "Meter",
            },
        )
        if name == "Meter" and not unit.is_meter:
            unit.is_meter = True
            unit.save(update_fields=["is_meter", "updated_at"])
