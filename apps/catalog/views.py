import io
import json
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.forms.models import construct_instance
from django.http import HttpResponse
from django.shortcuts import redirect, render

from apps.audit import services as audit
from apps.core.decorators import require_permission
from apps.core.mixins import get_tenant_object
from apps.inventory import services as inventory
from apps.subscriptions import services as subscriptions

from .forms import (
    BrandForm,
    CategoryForm,
    ProductForm,
    ProductImportForm,
    TaxRateForm,
    UnitForm,
    VariantForm,
)
from .models import Brand, Category, Product, ProductVariant, TaxRate, Unit


def _allowed_warehouse_ids(request):
    """Return None for tenant-wide access, otherwise allowed + central IDs."""
    allowed = request.membership.allowed_branch_ids
    if allowed is None:
        return None
    from apps.branches.models import Warehouse

    return list(
        Warehouse.objects.for_business(request.business)
        .filter(Q(branch_id__in=allowed) | Q(branch__isnull=True))
        .values_list("id", flat=True)
    )


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------
@require_permission("products.view")
def product_list(request):
    qs = (
        Product.objects.for_business(request.business)
        .select_related("category", "brand", "unit", "tax_rate")
    )
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(sku__icontains=q) |
                       Q(barcode__icontains=q) | Q(internal_code__icontains=q))
    category_id = request.GET.get("category", "")
    if category_id.isdigit():
        qs = qs.filter(category_id=category_id)
    status = request.GET.get("status", "")
    if status == "archived":
        qs = qs.filter(is_archived=True)
    elif status == "all":
        pass  # active + inactive + archived
    elif status == "inactive":
        qs = qs.filter(is_active=False, is_archived=False)
    else:  # default: everything not archived
        qs = qs.filter(is_archived=False)
        if status == "active":
            qs = qs.filter(is_active=True)
    sort = request.GET.get("sort", "name")
    if sort in ("name", "-name", "sale_price", "-sale_price", "-created_at"):
        qs = qs.order_by(sort)

    from django.core.paginator import Paginator

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))

    # Attach total stock to page items (single query)
    stock = {
        row["product_id"]: row["total"]
        for row in inventory.StockLevel.objects.for_business(request.business)
        .filter(product__in=[p.pk for p in page_obj])
        .values("product_id").annotate(total=Sum("quantity"))
    }
    for p in page_obj:
        p.total_stock = stock.get(p.pk, 0)

    categories = Category.objects.for_business(request.business).filter(is_active=True)
    p_cur, p_lim, _ = subscriptions.limit_state(request.business, "products")
    return render(request, "catalog/product_list.html", {
        "page_obj": page_obj, "q": q, "categories": categories,
        "active_nav": "products", "product_count": p_cur, "product_limit": p_lim,
        "querystring": _qs_without_page(request),
    })


def _qs_without_page(request):
    params = request.GET.copy()
    params.pop("page", None)
    encoded = params.urlencode()
    return f"{encoded}&" if encoded else ""


@require_permission("products.export")
def product_export(request):
    """Export products (CSV/XLSX) honoring catalog + stock filters."""
    from apps.reports import exports

    from . import services as catalog_services

    def _int(name):
        v = request.GET.get(name, "")
        return int(v) if v.isdigit() else None

    filters = {
        "category_id": _int("category"),
        "brand_id": _int("brand"),
        "branch_id": _int("branch"),
        "warehouse_id": _int("warehouse"),
        "status": request.GET.get("status", ""),
    }
    data = catalog_services.product_export_dataset(request.business, filters)
    audit.log("product.exported", request=request, module="catalog",
              description=f"Exported {len(data['rows'])} products "
                          f"({request.GET.get('format', 'csv')}).")
    if request.GET.get("format") == "xlsx":
        return exports.export_xlsx("products", data)
    return exports.export_csv("products", data)


@require_permission("products.manage")
def product_form(request, public_id=None):
    from . import services as catalog_services

    instance = None
    if public_id:
        instance = get_tenant_object(Product, request.business, public_id=public_id)
    else:
        from apps.subscriptions.helpers import guard_limit

        blocked = guard_limit(request, "products")
        if blocked:
            return blocked

    form = ProductForm(
        request.business,
        request.POST or None,
        request.FILES or None,
        instance=instance,
        allowed_warehouse_ids=_allowed_warehouse_ids(request),
    )
    if request.method == "POST" and form.is_valid():
        auto_sku = form.cleaned_data.get("auto_generate_sku")
        is_variant = form.cleaned_data.get("product_type") == Product.Type.VARIANT

        # Parse + validate any submitted variant rows BEFORE writing anything,
        # so a bad row re-renders the form without a partial save.
        variant_rows, variant_errors = ([], [])
        if is_variant:
            variant_rows, variant_errors = _parse_variant_rows(
                request, request.POST.get("variants_json", ""), auto_sku)
            if (
                any(row["opening_stock"] > 0 for row in variant_rows)
                and not form.cleaned_data.get("opening_warehouse")
            ):
                form.add_error(
                    "opening_warehouse",
                    "Select a warehouse for the variant opening stock.",
                )
        if variant_errors or form.errors:
            for err in variant_errors:
                messages.error(request, err)
            return render(request, "catalog/product_form.html",
                          {"form": form, "product": instance,
                           "active_nav": "products"})

        with transaction.atomic():
            if instance is not None:
                locked_product = (
                    Product.objects.select_for_update()
                    .select_related("unit")
                    .get(pk=instance.pk, business=request.business)
                )
                try:
                    catalog_services.validate_meter_product_shape(
                        locked_product,
                        target_unit=form.cleaned_data.get("unit"),
                        target_type=form.cleaned_data.get("product_type"),
                        target_tailoring=form.cleaned_data.get(
                            "is_tailoring_item"
                        ),
                    )
                except ValidationError as exc:
                    form.add_error("product_type", exc)
                    return render(
                        request,
                        "catalog/product_form.html",
                        {
                            "form": form,
                            "product": instance,
                            "active_nav": "products",
                        },
                    )
            # A concurrent ledger write may have appeared after form.clean().
            # The locked recheck above is authoritative before any edit write.
            if instance is not None:
                product = construct_instance(
                    form,
                    locked_product,
                    form._meta.fields,
                    form._meta.exclude,
                )
                form.instance = product
            else:
                product = form.save(commit=False)
            product.business = request.business
            if auto_sku and not product.sku:
                product.sku = catalog_services.generate_sku(request.business)
            product.save()

            opening = form.cleaned_data.get("opening_stock")
            warehouse = form.cleaned_data.get("opening_warehouse")
            if (
                not public_id
                and opening
                and warehouse
                and product.is_stocked
                and not (product.unit_id and product.unit.is_meter)
            ):
                inventory.set_opening_stock(
                    business=request.business, warehouse=warehouse, product=product,
                    quantity=opening, unit_cost=product.purchase_price,
                    user=request.user,
                )

            created_variants = 0
            if is_variant and variant_rows:
                created_variants = _create_variants(
                    request, product, variant_rows, warehouse)

        audit.log("product.saved", request=request, module="catalog", obj=product,
                  description=f"Product '{product.name}' saved"
                              + (f" with {created_variants} variant(s)."
                                 if created_variants else "."))
        messages.success(request, "Product saved.")
        if product.has_variants:
            return redirect("catalog:product_detail", public_id=product.public_id)
        return redirect("catalog:product_list")
    return render(request, "catalog/product_form.html",
                  {"form": form, "product": instance, "active_nav": "products"})


VARIANT_DECIMAL_MAX = Decimal("99999999999.999")
VARIANT_DECIMAL_QUANTUM = Decimal("0.001")


def _variant_decimal(item, field, label, index, errors):
    raw = item.get(field)
    if raw is None or str(raw).strip() == "":
        return Decimal("0")
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        errors.append(f"Variant {index}: enter a valid {label}.")
        return Decimal("0")
    if not value.is_finite():
        errors.append(f"Variant {index}: enter a valid {label}.")
        return Decimal("0")
    if value < 0:
        errors.append(f"Variant {index}: {label.capitalize()} cannot be negative.")
        return Decimal("0")
    if value > VARIANT_DECIMAL_MAX:
        errors.append(f"Variant {index}: {label.capitalize()} is too large.")
        return Decimal("0")
    try:
        quantized = value.quantize(VARIANT_DECIMAL_QUANTUM)
    except InvalidOperation:
        errors.append(f"Variant {index}: enter a valid {label}.")
        return Decimal("0")
    if value != quantized:
        errors.append(
            f"Variant {index}: {label.capitalize()} supports up to 3 decimal places."
        )
        return Decimal("0")
    return quantized


def _parse_variant_rows(request, raw_json, auto_sku):
    """Validate the variant builder payload. Returns (rows, errors).

    Each row is normalised to a dict; SKU/barcode uniqueness is checked
    against existing products + variants and within the submitted batch.
    Auto-SKU rows are validated for uniqueness only when a SKU is supplied
    (blank ones are generated at save time).
    """
    business = request.business
    if not raw_json.strip():
        return [], []
    try:
        payload = json.loads(raw_json)
    except (ValueError, TypeError):
        return [], ["Could not read the variants data. Please try again."]
    if not isinstance(payload, list):
        return [], ["Invalid variants data."]

    rows, errors = [], []
    seen_sku, seen_barcode = set(), set()
    for idx, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            errors.append(f"Variant {idx}: invalid variant data.")
            continue
        attributes = item.get("attributes") or {}
        if not isinstance(attributes, dict):
            errors.append(f"Variant {idx}: invalid attributes data.")
            attributes = {}
        attributes = {str(k).strip(): str(v).strip()
                      for k, v in attributes.items() if str(k).strip() and str(v).strip()}
        name = str(item.get("name") or "").strip() or " / ".join(attributes.values()) or "Variant"
        sku = str(item.get("sku") or "").strip()
        barcode = str(item.get("barcode") or "").strip()

        if sku:
            if sku in seen_sku:
                errors.append(f"Variant {idx}: SKU '{sku}' is repeated.")
            elif (Product.objects.for_business(business).filter(sku=sku).exists()
                  or ProductVariant.objects.for_business(business).filter(sku=sku).exists()):
                errors.append(f"Variant {idx}: SKU '{sku}' is already in use.")
            seen_sku.add(sku)
        if barcode:
            if barcode in seen_barcode:
                errors.append(f"Variant {idx}: barcode '{barcode}' is repeated.")
            elif (Product.objects.for_business(business).filter(barcode=barcode).exists()
                  or ProductVariant.objects.for_business(business).filter(barcode=barcode).exists()):
                errors.append(f"Variant {idx}: barcode '{barcode}' is already in use.")
            seen_barcode.add(barcode)

        rows.append({
            "name": name[:160], "attributes": attributes,
            "sku": sku[:60], "barcode": barcode[:80],
            "purchase_price": _variant_decimal(
                item, "purchase_price", "purchase price", idx, errors,
            ),
            "sale_price": _variant_decimal(
                item, "sale_price", "sale price", idx, errors,
            ),
            "opening_stock": _variant_decimal(
                item, "opening_stock", "opening stock", idx, errors,
            ),
        })
    return rows, errors


def _create_variants(request, product, rows, warehouse):
    """Create ProductVariant rows (+ opening stock). Assumes rows are
    already validated. Runs inside the caller's atomic block."""
    from . import services as catalog_services

    business = request.business
    reserved = set()
    created = 0
    for row in rows:
        sku = row["sku"]
        if not sku:  # auto-generate (auto-SKU on, or simply left blank)
            sku = catalog_services.generate_sku(business, taken=reserved)
        reserved.add(sku)
        variant = ProductVariant.objects.create(
            business=business, product=product, name=row["name"],
            attributes=row["attributes"], sku=sku, barcode=row["barcode"],
            purchase_price=row["purchase_price"],
            sale_price=(
                Decimal("0")
                if product.is_meter_tailoring
                else row["sale_price"]
            ),
        )
        if row["opening_stock"] and warehouse and product.is_stocked:
            inventory.set_opening_stock(
                business=business, warehouse=warehouse, product=product,
                variant=variant, quantity=row["opening_stock"],
                unit_cost=row["purchase_price"], user=request.user,
            )
        created += 1
    return created


@require_permission("products.view")
def product_detail(request, public_id):
    product = get_tenant_object(
        Product.objects.select_related("category", "brand", "unit", "tax_rate"),
        request.business, public_id=public_id,
    )
    variants = product.variants.all()
    levels = (
        inventory.StockLevel.objects.for_business(request.business)
        .filter(product=product).select_related("warehouse", "variant")
    )
    movements = (
        inventory.StockMovement.objects.for_business(request.business)
        .filter(product=product).select_related("warehouse", "user")[:30]
    )
    show_cost = request.membership.has_perm("cost.view")
    return render(request, "catalog/product_detail.html", {
        "product": product, "variants": variants, "levels": levels,
        "movements": movements, "active_nav": "products", "show_cost": show_cost,
    })


@require_permission("products.archive")
def product_archive(request, public_id):
    product = get_tenant_object(Product, request.business, public_id=public_id)
    if request.method == "POST":
        # Products referenced by invoices are archived, never deleted.
        product.is_archived = True
        product.is_active = False
        product.save(update_fields=["is_archived", "is_active"])
        audit.log("product.archived", request=request, module="catalog", obj=product,
                  description=f"Product '{product.name}' archived.")
        messages.success(request, f"'{product.name}' archived.")
    return redirect("catalog:product_list")


@require_permission("products.archive")
def product_restore(request, public_id):
    from . import services as catalog_services

    product = get_tenant_object(Product, request.business, public_id=public_id)
    if request.method == "POST":
        catalog_services.restore_product(product)
        audit.log("product.restored", request=request, module="catalog", obj=product,
                  description=f"Product '{product.name}' restored from archive.")
        messages.success(request, f"'{product.name}' restored and active again.")
    return redirect("catalog:product_detail", public_id=public_id)


@require_permission("products.delete")
def product_delete(request, public_id):
    from . import services as catalog_services

    product = get_tenant_object(Product, request.business, public_id=public_id)
    if request.method == "POST":
        name, ref = product.name, str(product.public_id)
        try:
            catalog_services.delete_product_if_safe(product)
        except catalog_services.ProductInUse as exc:
            messages.error(request, str(exc))
            return redirect("catalog:product_detail", public_id=public_id)
        audit.log("product.deleted", request=request, module="catalog",
                  description=f"Product '{name}' ({ref}) hard-deleted "
                              "(no transaction history).")
        messages.success(request, f"'{name}' permanently deleted.")
        return redirect("catalog:product_list")
    return redirect("catalog:product_detail", public_id=public_id)


@require_permission("products.manage")
def variant_form(request, product_id, public_id=None):
    product = get_tenant_object(Product, request.business, public_id=product_id)
    instance = None
    if public_id:
        instance = get_tenant_object(ProductVariant, request.business, public_id=public_id)
        if instance.product_id != product.id:
            from django.http import Http404
            raise Http404
    form = VariantForm(
        request.business,
        request.POST or None,
        request.FILES or None,
        instance=instance,
        product=product,
    )
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                locked_product = (
                    Product.objects.select_for_update()
                    .select_related("unit")
                    .get(pk=product.pk, business=request.business)
                )
                if instance is None or locked_product.product_type != Product.Type.VARIANT:
                    from . import services as catalog_services

                    catalog_services.validate_meter_product_shape(
                        locked_product,
                        target_unit=locked_product.unit,
                        target_type=Product.Type.VARIANT,
                        target_tailoring=locked_product.is_tailoring_item,
                    )
                if instance is not None:
                    locked_variant = ProductVariant.objects.select_for_update().get(
                        pk=instance.pk,
                        business=request.business,
                        product=locked_product,
                    )
                    variant = construct_instance(
                        form,
                        locked_variant,
                        form._meta.fields,
                        form._meta.exclude,
                    )
                    form.instance = variant
                else:
                    variant = form.save(commit=False)
                variant.business = request.business
                variant.product = locked_product
                variant.attributes = form.build_attributes()
                if not variant.name:
                    variant.name = " / ".join(variant.attributes.values()) or "Variant"
                variant.save()
                if locked_product.product_type != Product.Type.VARIANT:
                    locked_product.product_type = Product.Type.VARIANT
                    locked_product.save(update_fields=["product_type"])
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "Variant saved.")
            return redirect("catalog:product_detail", public_id=product.public_id)
    return render(request, "catalog/variant_form.html",
                  {"form": form, "product": product, "variant": instance,
                   "active_nav": "products"})


# ---------------------------------------------------------------------------
# Barcode generation / labels
# ---------------------------------------------------------------------------
@require_permission("products.view")
def product_barcode_svg(request, public_id):
    """Server-generated Code128 barcode as SVG."""
    import barcode
    from barcode.writer import SVGWriter

    product = get_tenant_object(Product, request.business, public_id=public_id)
    code = product.barcode or product.sku or f"P{product.pk:08d}"
    buffer = io.BytesIO()
    barcode.get("code128", code, writer=SVGWriter()).write(buffer)
    return HttpResponse(buffer.getvalue(), content_type="image/svg+xml")


@require_permission("products.view")
def product_labels(request, public_id):
    product = get_tenant_object(Product, request.business, public_id=public_id)
    try:
        count = max(1, min(int(request.GET.get("count", 12)), 120))
    except ValueError:
        count = 12
    return render(request, "catalog/labels.html", {
        "product": product, "count_range": range(count),
        "business": request.business,
    })


# ---------------------------------------------------------------------------
# Categories / brands / units / taxes (combined setup screens)
# ---------------------------------------------------------------------------
def _simple_crud(request, model, form_class, list_template, name, perm="products.manage",
                 extra=None):
    @require_permission(perm)
    def handler(request):
        instance = None
        edit_id = request.GET.get("edit")
        if edit_id:
            instance = get_tenant_object(model, request.business, public_id=edit_id)
        form = form_class(request.business, request.POST or None, instance=instance)
        if request.method == "POST" and form.is_valid():
            obj = form.save(commit=False)
            obj.business = request.business
            obj.save()
            if isinstance(obj, TaxRate) and obj.is_default:
                TaxRate.objects.for_business(request.business).exclude(pk=obj.pk).update(
                    is_default=False
                )
            messages.success(request, f"{name} saved.")
            return redirect(request.path)
        items = model.objects.for_business(request.business)
        ctx = {"form": form, "items": items, "editing": instance,
               "active_nav": "catalog_setup"}
        if extra:
            ctx.update(extra(request))
        return render(request, list_template, ctx)

    return handler(request)


def category_list(request):
    return _simple_crud(request, Category, CategoryForm,
                        "catalog/category_list.html", "Category")


def brand_list(request):
    return _simple_crud(request, Brand, BrandForm, "catalog/brand_list.html", "Brand")


def unit_list(request):
    return _simple_crud(request, Unit, UnitForm, "catalog/unit_list.html", "Unit")


def tax_list(request):
    return _simple_crud(request, TaxRate, TaxRateForm, "catalog/tax_list.html",
                        "Tax rate", perm="settings.manage")


@require_permission("products.import")
def product_import(request):
    from apps.core.imports import error_report_response, parse_tabular_file

    from . import services as catalog_services

    if request.GET.get("errors") == "1":
        errors = request.session.get("product_import_errors", [])
        return error_report_response("product_import_errors.csv", errors)

    form = ProductImportForm(request.POST or None, request.FILES or None)
    results = None
    import_error = None
    if request.method == "POST" and form.is_valid():
        try:
            subscriptions.require_operational(request.business)
        except subscriptions.SubscriptionInactive as exc:
            messages.error(request, str(exc))
            return redirect("catalog:product_list")
        rows, parse_error = parse_tabular_file(form.cleaned_data["file"])
        if parse_error:
            import_error = parse_error
            messages.error(request, parse_error)
        else:
            summary, errors = catalog_services.import_products(
                business=request.business, rows=rows,
                match_by=form.cleaned_data["match_by"], user=request.user,
                allowed_warehouse_ids=_allowed_warehouse_ids(request),
            )
            request.session["product_import_errors"] = errors
            results = {"summary": summary, "errors": errors, "total": len(rows)}
            audit.log("products.imported", request=request, module="catalog",
                      description=(f"Product import: {summary['created']} created, "
                                   f"{summary['failed']} failed, "
                                   f"{summary['skipped']} skipped."))
    return render(request, "catalog/product_import.html",
                  {"form": form, "results": results,
                   "import_error": import_error,
                   "columns": catalog_services.IMPORT_COLUMNS,
                   "active_nav": "products"})


@require_permission("products.import")
def import_template(request):
    from apps.reports import exports

    from . import services as catalog_services

    data = {
        "columns": [c.title() for c in catalog_services.IMPORT_COLUMNS],
        "rows": [[
            "Example T-Shirt", "TSH-001", "6291041500213", "Clothing",
            "Generic", "standard", "Piece", "2.500", "4.900", "2.500",
            "5", "No", "Yes", "10", "5", "Head Office", "Main Warehouse",
            "", "", "", "", "", "", "Active", "No",
        ]],
        "totals": None,
    }
    if request.GET.get("format") == "xlsx":
        return exports.export_xlsx("product_import_template", data)
    return exports.export_csv("product_import_template", data)
