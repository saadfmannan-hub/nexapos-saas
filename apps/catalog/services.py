"""Catalog defaults and helpers."""
import re
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.subscriptions.access import AccessAction, require_actor_access
from apps.subscriptions.exceptions import ModuleAccessDenied

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


PRODUCT_FORM_FIELDS = (
    "name", "product_type", "category", "brand", "unit", "internal_code",
    "sku", "barcode", "purchase_price", "sale_price", "wholesale_price",
    "minimum_sale_price", "tax_rate", "price_includes_tax", "reorder_level",
    "track_inventory", "allow_discount", "is_tailoring_item",
    "estimated_adult_fabric", "estimated_child_fabric", "image", "description",
    "preferred_supplier", "is_active",
)
VARIANT_FORM_FIELDS = (
    "name", "sku", "barcode", "purchase_price", "sale_price", "image",
    "is_active", "attributes",
)


def _require_product_write(*, business, user, permission_code,
                           membership=None, request=None, scope_allowed=True):
    return require_actor_access(
        user,
        business,
        "pos_core",
        permission_code=permission_code,
        action=AccessAction.WRITE,
        membership=membership,
        request=request,
        scope_allowed=scope_allowed,
    )


def _require_tailoring_product_write(
    *, business, user, permission_code, membership=None, request=None
):
    """Authorize a mutation whose canonical target is Tailoring-specific."""

    return require_actor_access(
        user,
        business,
        "tailoring",
        permission_code=permission_code,
        action=AccessAction.WRITE,
        membership=membership,
        request=request,
    )


@transaction.atomic
def save_product(*, product, business, user, membership=None, request=None):
    """Persist a basic product only after central POS Core authorization."""
    _require_product_write(
        business=business,
        user=user,
        permission_code="products.manage",
        membership=membership,
        request=request,
    )
    if product.business_id not in (None, business.id):
        _require_product_write(
            business=business,
            user=user,
            permission_code="products.manage",
            membership=membership,
            request=request,
            scope_allowed=False,
        )
    for field_name in (
        "category", "brand", "unit", "tax_rate", "preferred_supplier"
    ):
        relation_id = getattr(product, f"{field_name}_id")
        if relation_id is None:
            continue
        relation_model = Product._meta.get_field(field_name).remote_field.model
        canonical_relation = relation_model.objects.filter(
            pk=relation_id, business=business
        ).first()
        if canonical_relation is None:
            _require_product_write(
                business=business,
                user=user,
                permission_code="products.manage",
                membership=membership,
                request=request,
                scope_allowed=False,
            )
        setattr(product, field_name, canonical_relation)
    if product.pk:
        canonical_product = (
            Product.objects.select_for_update()
            .select_related("unit")
            .filter(pk=product.pk, business=business)
            .first()
        )
        if canonical_product is None:
            _require_product_write(
                business=business,
                user=user,
                permission_code="products.manage",
                membership=membership,
                request=request,
                scope_allowed=False,
            )
        target_is_meter = bool(product.unit_id and product.unit.is_meter)
        preserves_legacy_meter_retail = bool(
            canonical_product.unit_id
            and canonical_product.unit.is_meter
            and not canonical_product.is_tailoring_item
            and target_is_meter
            and not product.is_tailoring_item
        )
        if (
            canonical_product.is_tailoring_item
            or product.is_tailoring_item
            or (target_is_meter and not preserves_legacy_meter_retail)
        ):
            _require_tailoring_product_write(
                business=business,
                user=user,
                permission_code="products.manage",
                membership=membership,
                request=request,
            )
        for field_name in PRODUCT_FORM_FIELDS:
            setattr(canonical_product, field_name, getattr(product, field_name))
        product = canonical_product
    elif product.is_tailoring_item or (
        product.unit_id and product.unit.is_meter
    ):
        _require_tailoring_product_write(
            business=business,
            user=user,
            permission_code="products.manage",
            membership=membership,
            request=request,
        )
    product.business = business
    product.save()
    return product


@transaction.atomic
def save_variant(*, variant, product, user, membership=None, request=None):
    """Persist a product variant within its authorized product tenant."""
    business = product.business
    _require_product_write(
        business=business,
        user=user,
        permission_code="products.manage",
        membership=membership,
        request=request,
    )
    canonical_product = (
        Product.objects.select_for_update()
        .filter(pk=product.pk, business=business)
        .first()
    )
    if canonical_product is None:
        _require_product_write(
            business=business,
            user=user,
            permission_code="products.manage",
            membership=membership,
            request=request,
            scope_allowed=False,
        )
    if canonical_product.is_tailoring_item:
        _require_tailoring_product_write(
            business=business,
            user=user,
            permission_code="products.manage",
            membership=membership,
            request=request,
        )
    if variant.business_id not in (None, business.id) or variant.product_id not in (
        None, canonical_product.id
    ):
        _require_product_write(
            business=business,
            user=user,
            permission_code="products.manage",
            membership=membership,
            request=request,
            scope_allowed=False,
        )
    if variant.pk:
        canonical_variant = (
            ProductVariant.objects.select_for_update()
            .filter(
                pk=variant.pk,
                business=business,
                product=canonical_product,
            )
            .first()
        )
        if canonical_variant is None:
            _require_product_write(
                business=business,
                user=user,
                permission_code="products.manage",
                membership=membership,
                request=request,
                scope_allowed=False,
            )
        for field_name in VARIANT_FORM_FIELDS:
            setattr(canonical_variant, field_name, getattr(variant, field_name))
        variant = canonical_variant
    variant.business = business
    variant.product = canonical_product
    variant.save()
    return variant


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
def delete_product_if_safe(product, *, user, membership=None, request=None):
    """Hard-delete a product that has NEVER been used in any transaction.
    Products with history must be archived instead (they stay on old
    invoices and reports)."""
    business = product.business
    _require_product_write(
        business=business,
        user=user,
        permission_code="products.delete",
        membership=membership,
        request=request,
    )
    canonical_product = (
        Product.objects.select_for_update()
        .filter(pk=product.pk, business=business)
        .first()
    )
    if canonical_product is None:
        _require_product_write(
            business=business,
            user=user,
            permission_code="products.delete",
            membership=membership,
            request=request,
            scope_allowed=False,
        )
    if canonical_product.is_tailoring_item:
        _require_tailoring_product_write(
            business=business,
            user=user,
            permission_code="products.delete",
            membership=membership,
            request=request,
        )
    refs = product_history_refs(canonical_product)
    if refs:
        raise ProductInUse(
            "This product appears in " + ", ".join(refs) +
            " and cannot be deleted. Archive it instead — it stays on "
            "historical invoices and reports."
        )
    canonical_product.delete()


@transaction.atomic
def archive_product(*, product, user, membership=None, request=None):
    business = product.business
    _require_product_write(
        business=business,
        user=user,
        permission_code="products.archive",
        membership=membership,
        request=request,
    )
    canonical_product = (
        Product.objects.select_for_update()
        .filter(pk=product.pk, business=business)
        .first()
    )
    if canonical_product is None:
        _require_product_write(
            business=business,
            user=user,
            permission_code="products.archive",
            membership=membership,
            request=request,
            scope_allowed=False,
        )
    if canonical_product.is_tailoring_item:
        _require_tailoring_product_write(
            business=business,
            user=user,
            permission_code="products.archive",
            membership=membership,
            request=request,
        )
    canonical_product.is_archived = True
    canonical_product.is_active = False
    canonical_product.save(
        update_fields=["is_archived", "is_active", "updated_at"]
    )
    return canonical_product


@transaction.atomic
def restore_product(product, *, user, membership=None, request=None):
    business = product.business
    _require_product_write(
        business=business,
        user=user,
        permission_code="products.archive",
        membership=membership,
        request=request,
    )
    canonical_product = (
        Product.objects.select_for_update()
        .filter(pk=product.pk, business=business)
        .first()
    )
    if canonical_product is None:
        _require_product_write(
            business=business,
            user=user,
            permission_code="products.archive",
            membership=membership,
            request=request,
            scope_allowed=False,
        )
    if canonical_product.is_tailoring_item:
        _require_tailoring_product_write(
            business=business,
            user=user,
            permission_code="products.archive",
            membership=membership,
            request=request,
        )
    canonical_product.is_archived = False
    canonical_product.is_active = True
    canonical_product.save(
        update_fields=["is_archived", "is_active", "updated_at"]
    )
    return canonical_product


IMPORT_COLUMNS = [
    "product name", "sku", "barcode", "category", "brand", "product type", "unit",
    "purchase price", "sale price", "cost price", "tax/vat rate",
    "tax inclusive", "track inventory", "opening stock", "minimum stock",
    "branch code", "branch name", "warehouse code", "warehouse name",
    "variant option name", "variant option value", "variant sku", "variant barcode",
    "active", "archived",
]
EXPORT_COLUMNS = [
    "Product Name", "SKU", "Barcode", "Category", "Brand", "Product Type",
    "Unit", "Purchase Price", "Sale Price", "Cost Price", "Tax/VAT Rate",
    "Tax Inclusive", "Track Inventory", "Opening Stock", "Minimum Stock",
    "Branch Code", "Branch Name", "Warehouse Code", "Warehouse Name",
    "Variant Option Name", "Variant Option Value", "Variant SKU",
    "Variant Barcode", "Active", "Archived",
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


def products_visible_in_branch(queryset, *, business, branch):
    """Scope stocked items by warehouse; non-stock services stay global."""
    from django.db.models import Q

    if branch is None or branch.business_id != business.id:
        return queryset.none()
    from apps.inventory import services as inventory_services

    shared_warehouse = inventory_services.configured_shared_fabric_warehouse(business)
    shared_fabric = Q()
    if shared_warehouse is not None:
        shared_fabric = Q(
            is_tailoring_item=True,
            unit__is_meter=True,
            stock_levels__business=business,
            stock_levels__warehouse=shared_warehouse,
        )
    return queryset.filter(
        Q(track_inventory=False)
        | Q(product_type__in=(Product.Type.SERVICE, Product.Type.NON_STOCK))
        | Q(
            stock_levels__business=business,
            stock_levels__warehouse__business=business,
            stock_levels__warehouse__branch=branch,
            stock_levels__warehouse__is_active=True,
        )
        | shared_fabric
    ).distinct()


def product_is_visible_in_branch(*, business, product, branch, variant=None):
    """Return whether a product (and optional variant) is available in a branch."""
    from apps.inventory.models import StockLevel

    if (
        branch is None
        or branch.business_id != business.id
        or product.business_id != business.id
    ):
        return False
    if not product.is_stocked:
        return True
    from apps.inventory import services as inventory_services

    shared_warehouse = inventory_services.configured_shared_fabric_warehouse(business)
    if product.is_meter_tailoring and shared_warehouse is not None:
        levels = StockLevel.objects.for_business(business).filter(
            product=product,
            warehouse=shared_warehouse,
        )
    else:
        levels = StockLevel.objects.for_business(business).filter(
            product=product,
            warehouse__business=business,
            warehouse__branch=branch,
            warehouse__is_active=True,
        )
    if variant is not None:
        if variant.business_id != business.id or variant.product_id != product.id:
            return False
        levels = levels.filter(variant=variant)
    elif product.has_variants:
        levels = levels.filter(variant__isnull=False)
    return levels.exists()


def _as_bool(value, default=False):
    raw = str(value or "").strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "y", "active", "enabled")


def _as_optional_variant_value(value):
    """Normalize import placeholders used for absent variant-only values."""
    raw = "" if value is None else str(value).strip()
    if raw.casefold() in ("", "null", "-"):
        return ""
    return raw


def _as_optional_barcode(value):
    """Normalize Product Import placeholders used for an absent barcode."""
    raw = "" if value is None else str(value).strip()
    if raw.casefold() in ("", "null", "-"):
        return ""
    return raw


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
    barcode = _as_optional_barcode(barcode)
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


def find_reusable_product(
    business,
    *,
    name,
    sku="",
    barcode="",
    category=None,
    brand=None,
    unit=None,
    product_type,
):
    """Resolve one safely matching business-wide Product for branch onboarding."""
    products = Product.objects.for_business(business).select_for_update()
    variants = ProductVariant.objects.for_business(business)
    candidates = []
    for field, value in (("sku", sku), ("barcode", barcode)):
        value = str(value or "").strip()
        if not value:
            continue
        if variants.filter(**{field: value}).exists():
            raise ValidationError(
                f"{field.upper()} belongs to an existing variant: {value}"
            )
        match = products.filter(**{field: value}).first()
        if match is not None:
            candidates.append(match)
    if candidates and any(item.pk != candidates[0].pk for item in candidates):
        raise ValidationError("SKU and barcode identify different Products.")

    signature = products.filter(
        name__iexact=str(name or "").strip(),
        category=category,
        brand=brand,
        unit=unit,
    )
    if candidates:
        product = candidates[0]
        if not signature.filter(pk=product.pk).exists():
            raise ValidationError(
                "The Product identifiers already belong to a different Product."
            )
    else:
        matches = list(signature[:2])
        if len(matches) > 1:
            raise ValidationError(
                "Product identity is ambiguous. Supply a unique Product SKU."
            )
        product = matches[0] if matches else None

    if product is None:
        return None
    if product.product_type != product_type:
        raise ValidationError(
            "The existing Product has a different Product Type."
        )
    if sku and product.sku != sku:
        raise ValidationError("The matching Product has a different SKU.")
    if barcode and product.barcode != barcode:
        raise ValidationError("The matching Product has a different barcode.")
    return product


def find_reusable_variant(
    business,
    *,
    product,
    name,
    attributes,
    sku="",
    barcode="",
):
    """Resolve a safe existing variant or return None for a missing variant."""
    variants = ProductVariant.objects.for_business(business).select_for_update()
    candidates = []
    for field, value in (("sku", sku), ("barcode", barcode)):
        value = str(value or "").strip()
        if not value:
            continue
        if Product.objects.for_business(business).filter(
            **{field: value}
        ).exists():
            raise ValidationError(
                f"Variant {field.upper()} belongs to an existing Product: {value}"
            )
        match = variants.filter(**{field: value}).first()
        if match is not None:
            if match.product_id != product.id:
                raise ValidationError(
                    f"Variant {field.upper()} belongs to another Product: {value}"
                )
            candidates.append(match)
    if candidates and any(item.pk != candidates[0].pk for item in candidates):
        raise ValidationError(
            "Variant SKU and barcode identify different variants."
        )
    if candidates:
        variant = candidates[0]
        if (
            variant.name.casefold() != str(name or "").strip().casefold()
            or variant.attributes != attributes
        ):
            raise ValidationError(
                "The Variant identifiers already belong to a different variant."
            )
        return variant

    matches = list(
        variants.filter(
            product=product,
            name__iexact=str(name or "").strip(),
            attributes=attributes,
        )[:2]
    )
    if len(matches) > 1:
        raise ValidationError(
            "Variant identity is ambiguous. Supply a unique Variant SKU."
        )
    variant = matches[0] if matches else None
    if variant is not None:
        if sku and variant.sku != sku:
            raise ValidationError("The matching Variant has a different SKU.")
        if barcode and variant.barcode != barcode:
            raise ValidationError("The matching Variant has a different barcode.")
    return variant


def ensure_branch_opening_stock(
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
    """Create one warehouse assignment and opening movement, idempotently."""
    from apps.inventory import services as inventory
    from apps.inventory.models import StockLevel

    inventory.require_inventory_write(
        business=business,
        user=user,
        permission_code="inventory.adjust",
        membership=membership,
        request=request,
        warehouses=(warehouse,),
        tenant_objects=(product, variant),
    )
    existing = (
        StockLevel.objects.select_for_update()
        .filter(
            business=business,
            warehouse=warehouse,
            product=product,
            variant=variant,
        )
        .first()
    )
    if existing is not None:
        return existing, False
    if quantity > 0:
        inventory.set_opening_stock(
            business=business,
            warehouse=warehouse,
            product=product,
            variant=variant,
            quantity=quantity,
            unit_cost=unit_cost,
            user=user,
            membership=membership,
            request=request,
        )
        return (
            StockLevel.objects.get(
                business=business,
                warehouse=warehouse,
                product=product,
                variant=variant,
            ),
            True,
        )
    return (
        StockLevel.objects.create(
            business=business,
            warehouse=warehouse,
            product=product,
            variant=variant,
            quantity=Decimal("0"),
        ),
        True,
    )


def product_export_dataset(
    business, filters, *, allowed_branch_ids, allowed_warehouse_ids
):
    """Build {columns, rows} for product export.

    Scales to large catalogs: stock is fetched in a single aggregated
    query and joined in memory rather than per-row.
    """
    from django.db.models import Q, Sum

    from apps.branches.models import Branch, Warehouse
    from apps.inventory.models import StockLevel

    allowed_branch_ids = (
        None if allowed_branch_ids is None else set(allowed_branch_ids)
    )
    allowed_warehouse_ids = (
        None if allowed_warehouse_ids is None else set(allowed_warehouse_ids)
    )
    branch_id = filters.get("branch_id")
    warehouse_id = filters.get("warehouse_id")

    if (
        branch_id is not None
        and allowed_branch_ids is not None
        and branch_id not in allowed_branch_ids
    ) or (
        warehouse_id is not None
        and allowed_warehouse_ids is not None
        and warehouse_id not in allowed_warehouse_ids
    ):
        return {"columns": EXPORT_COLUMNS, "rows": [], "totals": None}

    branch = None
    if branch_id is not None:
        branch_qs = Branch.objects.for_business(business)
        if allowed_branch_ids is not None:
            branch_qs = branch_qs.filter(pk__in=allowed_branch_ids)
        branch = branch_qs.filter(pk=branch_id).first()
        if branch is None:
            return {"columns": EXPORT_COLUMNS, "rows": [], "totals": None}

    warehouse = None
    if warehouse_id is not None:
        warehouse_qs = Warehouse.objects.for_business(business).select_related("branch")
        if allowed_warehouse_ids is not None:
            warehouse_qs = warehouse_qs.filter(pk__in=allowed_warehouse_ids)
        if allowed_branch_ids is not None:
            warehouse_qs = warehouse_qs.filter(
                Q(branch_id__in=allowed_branch_ids) | Q(branch__isnull=True)
            )
        warehouse = warehouse_qs.filter(pk=warehouse_id).first()
        if warehouse is None:
            return {"columns": EXPORT_COLUMNS, "rows": [], "totals": None}

    qs = Product.objects.for_business(business).select_related(
        "category", "brand", "unit")
    if branch is not None:
        qs = products_visible_in_branch(qs, business=business, branch=branch)
    if not filters.get("include_tailoring", True):
        qs = qs.filter(is_tailoring_item=False)

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

    # Stock is always intersected with the canonical membership scope before
    # applying optional request filters.
    level_qs = StockLevel.objects.for_business(business)
    if allowed_warehouse_ids is not None:
        level_qs = level_qs.filter(warehouse_id__in=allowed_warehouse_ids)
    if allowed_branch_ids is not None:
        level_qs = level_qs.filter(
            Q(warehouse__branch_id__in=allowed_branch_ids)
            | Q(warehouse__branch__isnull=True)
        )
    if warehouse is not None:
        level_qs = level_qs.filter(warehouse=warehouse)
    if branch is not None:
        level_qs = level_qs.filter(warehouse__branch=branch)
    stock_map = {
        (row["product_id"], row["variant_id"]): row["q"]
        for row in level_qs.values(
            "product_id", "variant_id"
        ).annotate(q=Sum("quantity"))
    }
    branch_code = branch.code if branch is not None else ""
    branch_name = branch.name if branch is not None else ""
    warehouse_code = warehouse.code if warehouse is not None else ""
    warehouse_name = warehouse.name if warehouse is not None else ""

    def export_row(product, *, variant=None, stock=Decimal("0")):
        option_name = ""
        option_value = ""
        if variant is not None and variant.attributes:
            option_name = " / ".join(variant.attributes.keys())
            option_value = " / ".join(str(v) for v in variant.attributes.values())
        return [
            product.name,
            product.sku,
            product.barcode,
            product.category.name if product.category else "",
            product.brand.name if product.brand else "",
            product.product_type,
            product.unit.name if product.unit else "",
            variant.purchase_price if variant is not None else product.purchase_price,
            variant.sale_price if variant is not None else product.sale_price,
            variant.average_cost if variant is not None else product.average_cost,
            product.tax_rate.rate if product.tax_rate else "",
            (
                ""
                if product.price_includes_tax is None
                else ("Yes" if product.price_includes_tax else "No")
            ),
            "Yes" if product.track_inventory else "No",
            stock,
            product.reorder_level,
            branch_code,
            branch_name,
            warehouse_code,
            warehouse_name,
            option_name,
            option_value,
            variant.sku if variant is not None else "",
            variant.barcode if variant is not None else "",
            "Yes" if (variant or product).is_active else "No",
            "Yes" if product.is_archived else "No",
        ]

    rows = []
    for product in qs.prefetch_related("variants").order_by("name"):
        product_stock = sum(
            quantity
            for (product_id, _variant_id), quantity in stock_map.items()
            if product_id == product.id
        )
        if status == "low" and not (
            product.reorder_level > 0
            and product_stock <= product.reorder_level
        ):
            continue
        if status == "out" and product_stock > 0:
            continue
        if product.has_variants:
            for variant in product.variants.all():
                key = (product.id, variant.id)
                if product.is_stocked and key not in stock_map:
                    continue
                rows.append(
                    export_row(
                        product,
                        variant=variant,
                        stock=stock_map.get(key, Decimal("0")),
                    )
                )
        else:
            key = (product.id, None)
            if product.is_stocked and key not in stock_map:
                continue
            rows.append(
                export_row(
                    product,
                    stock=stock_map.get(key, Decimal("0")),
                )
            )
    return {"columns": EXPORT_COLUMNS, "rows": rows, "totals": None}


@transaction.atomic
def import_products(
    *, business, rows, match_by, user, allowed_warehouse_ids=None,
    membership=None, request=None, branch_context_mode=None,
    selected_branch=None, selected_warehouse=None,
):
    """Import products and variants with row-level error reporting.

    Matching remains backward compatible with the old form choices:
    ``sku``, ``barcode`` or ``name``. Returns (summary, errors).
    """
    from apps.core.imports import normalize_row
    from apps.subscriptions import services as subscriptions

    context = _require_product_write(
        business=business,
        user=user,
        permission_code="products.import",
        membership=membership,
        request=request,
    )

    if branch_context_mode not in (None, "master", "branch"):
        raise ValidationError("Unknown Product import context.")
    canonical_branch = None
    canonical_warehouse = None
    if branch_context_mode == "branch":
        from apps.branches.models import Branch, Warehouse

        branch_qs = Branch.objects.for_business(business).filter(is_active=True)
        allowed_branch_ids = context.membership.allowed_branch_ids
        if allowed_branch_ids is not None:
            branch_qs = branch_qs.filter(pk__in=allowed_branch_ids)
        canonical_branch = branch_qs.filter(
            pk=getattr(selected_branch, "pk", selected_branch)
        ).first()
        if canonical_branch is None:
            raise ValidationError("Select a valid branch for Product import.")
        warehouse_qs = Warehouse.objects.for_business(business).filter(
            branch=canonical_branch,
            is_active=True,
        )
        allowed_ids = context.membership.allowed_warehouse_ids
        if allowed_ids is not None:
            warehouse_qs = warehouse_qs.filter(pk__in=allowed_ids)
        canonical_warehouse = warehouse_qs.filter(
            pk=getattr(selected_warehouse, "pk", selected_warehouse)
        ).first()
        if canonical_warehouse is None:
            raise ValidationError(
                "Select a valid warehouse in the selected branch for Product import."
            )

    canonical_warehouse_ids = context.membership.allowed_warehouse_ids
    explicit_warehouse_ids = (
        None if allowed_warehouse_ids is None else set(allowed_warehouse_ids)
    )
    if canonical_warehouse_ids is None:
        effective_warehouse_ids = explicit_warehouse_ids
    elif explicit_warehouse_ids is None:
        effective_warehouse_ids = canonical_warehouse_ids
    else:
        effective_warehouse_ids = (
            set(canonical_warehouse_ids) & explicit_warehouse_ids
        )

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
            barcode = _as_optional_barcode(r.get("barcode"))
            variant_parent = _as_optional_variant_value(
                r.get("variant parent")
            )
            option_name = _as_optional_variant_value(
                r.get("variant option name")
            )
            option_value = _as_optional_variant_value(
                r.get("variant option value")
            )
            variant_name = _as_optional_variant_value(r.get("variant name"))
            variant_sku = _as_optional_variant_value(r.get("variant sku"))
            variant_barcode = _as_optional_barcode(
                r.get("variant barcode")
            )
            is_variant_row = bool(
                variant_parent or variant_name or option_name or option_value
                or variant_sku or variant_barcode
            )

            if not name and not variant_parent:
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

            parent_lookup = variant_parent
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
                if existing is None and sku:
                    existing = Product.objects.for_business(business).filter(
                        sku=sku
                    ).first()
                if existing is None and barcode:
                    existing = Product.objects.for_business(business).filter(
                        barcode=barcode
                    ).first()
                if existing is None and name:
                    identity = Product.objects.for_business(business).filter(
                        name__iexact=name
                    )
                    if r.get("category"):
                        identity = identity.filter(
                            category__name__iexact=r["category"]
                        )
                    else:
                        identity = identity.filter(category__isnull=True)
                    if r.get("brand"):
                        identity = identity.filter(
                            brand__name__iexact=r["brand"]
                        )
                    else:
                        identity = identity.filter(brand__isnull=True)
                    if r.get("unit"):
                        identity = identity.filter(
                            unit__name__iexact=r["unit"]
                        )
                    else:
                        identity = identity.filter(unit__isnull=True)
                    identity_matches = list(identity[:2])
                    if len(identity_matches) > 1:
                        raise ValueError(
                            "Product identity is ambiguous. Supply a unique SKU."
                        )
                    existing = identity_matches[0] if identity_matches else None
            elif match_by == "sku" and sku:
                existing = Product.objects.for_business(business).filter(sku=sku).first()
            elif match_by == "barcode" and barcode:
                existing = Product.objects.for_business(business).filter(
                    barcode=barcode
                ).first()
            elif match_by == "name" and name:
                identity = Product.objects.for_business(business).filter(
                    name__iexact=name
                )
                if r.get("brand"):
                    identity = identity.filter(brand__name__iexact=r["brand"])
                if r.get("category"):
                    identity = identity.filter(
                        category__name__iexact=r["category"]
                    )
                if r.get("unit"):
                    identity = identity.filter(unit__name__iexact=r["unit"])
                matches = list(identity[:2])
                if len(matches) > 1:
                    raise ValueError(
                        "Product identity is ambiguous. Supply a unique SKU."
                    )
                existing = matches[0] if matches else None

            if existing is not None:
                if name and existing.name.casefold() != name.casefold():
                    raise ValueError(
                        "The Product identifiers belong to a different Product."
                    )
                if sku and existing.sku != sku:
                    raise ValueError("The matching Product has a different SKU.")
                if barcode and existing.barcode != barcode:
                    raise ValueError(
                        "The matching Product has a different barcode."
                    )
                if existing.product_type != product_type:
                    raise ValueError(
                        "The existing Product has a different Product Type."
                    )

            _current, limit, allowed = subscriptions.limit_state(business, "products")
            if not existing and not allowed:
                raise ValueError(f"Plan product limit ({limit}) reached.")

            if branch_context_mode == "branch":
                expected_metadata = (
                    ("Branch Code", r.get("branch code"), canonical_branch.code),
                    ("Branch Name", r.get("branch name"), canonical_branch.name),
                    ("Warehouse Code", r.get("warehouse code"), canonical_warehouse.code),
                    ("Warehouse Name", r.get("warehouse name"), canonical_warehouse.name),
                )
                for label, supplied, expected in expected_metadata:
                    if str(supplied or "").strip().casefold() != str(expected).casefold():
                        raise ValueError(
                            f"{label} must exactly match the selected context: {expected}"
                        )
                warehouse = canonical_warehouse
            elif branch_context_mode == "master":
                context_values = (
                    r.get("branch code"), r.get("branch name"),
                    r.get("warehouse code"), r.get("warehouse name"),
                    r.get("branch"), r.get("warehouse"),
                )
                if opening_stock > 0 or any(str(value or "").strip() for value in context_values):
                    raise ValueError(
                        "Business-wide Product Master import cannot mutate branch stock. "
                        "Select a branch and warehouse first."
                    )
                warehouse = None
            else:
                warehouse = _resolve_warehouse(
                    business,
                    r.get("branch name") or r.get("branch", ""),
                    r.get("warehouse name") or r.get("warehouse", ""),
                    allowed_warehouse_ids=effective_warehouse_ids,
                )

            row_tracks_stock = (
                product_type in (Product.Type.STANDARD, Product.Type.VARIANT)
                and _as_bool(r.get("track inventory"), True)
            )
            if opening_stock > 0 and not row_tracks_stock:
                raise ValueError(
                    "Opening Stock must be 0 for a non-stock Product."
                )

            if existing and not is_variant_row:
                if branch_context_mode != "branch":
                    summary["skipped"] += 1
                    continue
                if existing.has_variants:
                    raise ValueError(
                        "Import each variant separately to make this product available "
                        "in the selected branch."
                    )
                if existing.is_stocked:
                    ensure_branch_opening_stock(
                        business=business,
                        warehouse=warehouse,
                        product=existing,
                        quantity=opening_stock,
                        unit_cost=purchase_price,
                        user=user,
                        membership=context.membership,
                        request=request,
                    )
                elif opening_stock > 0:
                    raise ValueError(
                        "Opening Stock must be 0 for a non-stock Product."
                    )
                summary["updated"] += 1
                continue
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
            if is_meter or (existing is not None and existing.is_tailoring_item):
                _require_tailoring_product_write(
                    business=business,
                    user=user,
                    permission_code="products.import",
                    membership=context.membership,
                    request=request,
                )
            # Do not create even ancillary catalog rows until the commercial
            # Tailoring boundary above has authorized a Meter/Tailoring row.
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
            if existing is not None:
                if existing.category_id != getattr(category, "id", None):
                    raise ValueError(
                        "The matching Product has a different Category."
                    )
                if existing.brand_id != getattr(brand, "id", None):
                    raise ValueError(
                        "The matching Product has a different Brand."
                    )
                if existing.unit_id != getattr(effective_unit, "id", None):
                    raise ValueError(
                        "The matching Product has a different Unit."
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

                    attributes = {}
                    if option_name and option_value:
                        attributes[option_name] = option_value
                    display_name = (
                        variant_name or option_value or "Variant"
                    )[:160]
                    variant = find_reusable_variant(
                        business,
                        product=product,
                        name=display_name,
                        attributes=attributes,
                        sku=variant_sku,
                        barcode=variant_barcode,
                    )
                    if variant is None:
                        if not variant_sku:
                            variant_sku = generate_sku(business)
                        variant = ProductVariant.objects.create(
                            business=business, product=product, name=display_name,
                            attributes=attributes, sku=variant_sku[:60],
                            barcode=variant_barcode[:80],
                            purchase_price=purchase_price,
                            sale_price=sale_price,
                            average_cost=cost_price,
                            is_active=_as_bool(r.get("active"), True),
                        )
                        summary["created"] += 1
                    else:
                        summary["updated"] += 1
                    if branch_context_mode == "branch":
                        ensure_branch_opening_stock(
                            business=business,
                            warehouse=warehouse,
                            product=product,
                            variant=variant,
                            quantity=opening_stock,
                            unit_cost=purchase_price,
                            user=user,
                            membership=context.membership,
                            request=request,
                        )
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
                if branch_context_mode == "branch" and product.is_stocked:
                    ensure_branch_opening_stock(
                        business=business,
                        warehouse=warehouse,
                        product=product,
                        quantity=opening_stock,
                        unit_cost=purchase_price,
                        user=user,
                        membership=context.membership,
                        request=request,
                    )
                summary["created"] += 1
        except ModuleAccessDenied:
            raise
        except Exception as exc:
            errors.append((idx, str(exc)))
            summary["failed"] += 1
    if errors:
        transaction.set_rollback(True)
        summary.update({
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "failed": len(rows),
        })
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
