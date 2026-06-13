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
