"""Catalog defaults and helpers."""
from django.db import transaction

from .models import Product, Unit


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
    "SKU", "Barcode", "Product Name", "Category", "Brand", "Unit",
    "Purchase Price", "Selling Price", "Current Stock", "Minimum Stock",
    "Warehouse", "Branch", "Status", "Created Date", "Updated Date",
]


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
    for p in qs.order_by("name"):
        stock = stock_map.get(p.id, 0)
        if status == "low" and not (p.reorder_level > 0 and stock <= p.reorder_level):
            continue
        if status == "out" and stock > 0:
            continue
        rows.append([
            p.sku, p.barcode, p.name,
            p.category.name if p.category else "",
            p.brand.name if p.brand else "",
            p.unit.name if p.unit else "",
            p.purchase_price, p.sale_price, stock, p.reorder_level,
            warehouse.name if warehouse else "All",
            branch_name or "All",
            "Archived" if p.is_archived else ("Active" if p.is_active else "Inactive"),
            p.created_at.strftime("%Y-%m-%d"),
            p.updated_at.strftime("%Y-%m-%d"),
        ])
    return {"columns": EXPORT_COLUMNS, "rows": rows, "totals": None}


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
        Unit.objects.get_or_create(
            business=business, name=name,
            defaults={"abbreviation": abbr, "allow_decimal": dec},
        )
