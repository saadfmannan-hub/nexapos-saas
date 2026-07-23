import io
import json
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.forms.models import construct_instance
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from apps.audit import services as audit
from apps.core.mixins import get_tenant_object
from apps.inventory import services as inventory
from apps.subscriptions import services as subscriptions
from apps.subscriptions.access import AccessAction, get_access_context, require_access
from apps.subscriptions.decorators import module_permission_required

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
    """Return the canonical membership warehouse scope."""
    return request.membership.allowed_warehouse_ids


def _tailoring_enabled(request):
    return get_access_context(request).has_module("tailoring")


def _require_tailoring_product_access(
    request, product, *, permission_code, action=AccessAction.READ
):
    if product.is_tailoring_item:
        require_access(
            request,
            "tailoring",
            permission_code=permission_code,
            action=action,
        )


def _require_business_wide_catalog(request, permission_code, action=AccessAction.READ):
    if request.membership.allowed_branch_ids is not None:
        require_access(
            request,
            "pos_core",
            permission_code=permission_code,
            action=action,
            scope_allowed=False,
        )


def _catalog_branch_context(request, *, post_field="branch"):
    """Resolve the operational Product branch without accepting forged scope."""
    from apps.branches.models import Branch

    source = request.POST if request.method == "POST" else request.GET
    raw_branch = source.get(post_field, "")
    branches = Branch.objects.for_business(request.business).filter(is_active=True)
    allowed = request.membership.allowed_branch_ids
    if allowed is not None:
        branches = branches.filter(pk__in=allowed)
    selected = None
    if raw_branch:
        if not str(raw_branch).isdigit():
            raise Http404
        selected = branches.filter(pk=int(raw_branch)).first()
        if selected is None:
            raise Http404
    elif allowed is not None:
        assigned = list(branches.order_by("id")[:2])
        if len(assigned) != 1:
            raise Http404
        selected = assigned[0]
    elif request.method == "POST":
        # Backward-compatible server-side resolution for forms submitted from
        # an already-selected single-branch context. Normal owner GET flow
        # still requires an explicit Branch selection on the Products page.
        available = list(branches.order_by("id")[:2])
        if len(available) == 1:
            selected = available[0]
    return selected, branches.order_by("name")


def _catalog_warehouse_context(
    request, branch, *, required=False, post_field="warehouse"
):
    from apps.branches.models import Warehouse

    source = request.POST if request.method == "POST" else request.GET
    raw_warehouse = source.get(post_field, "")
    if request.method == "POST" and not raw_warehouse:
        raw_warehouse = source.get("warehouse", "")
    if branch is None:
        if raw_warehouse:
            raise Http404
        return None, Warehouse.objects.none()
    warehouses = Warehouse.objects.for_business(request.business).filter(
        branch=branch,
        is_active=True,
    )
    allowed = request.membership.allowed_warehouse_ids
    if allowed is not None:
        warehouses = warehouses.filter(pk__in=allowed)
    selected = None
    if raw_warehouse:
        if not str(raw_warehouse).isdigit():
            raise Http404
        selected = warehouses.filter(pk=int(raw_warehouse)).first()
        if selected is None:
            raise Http404
    if selected is None:
        available = list(warehouses.order_by("id")[:2])
        if len(available) == 1:
            selected = available[0]
    if selected is None and required:
        raise Http404
    return selected, warehouses.order_by("name")


def _catalog_product_queryset(request, branch=None):
    from . import services as catalog_services

    queryset = Product.objects.for_business(request.business)
    if branch is not None:
        queryset = catalog_services.products_visible_in_branch(
            queryset,
            business=request.business,
            branch=branch,
        )
    return queryset


def _catalog_product_object_queryset(request, branch=None, *, post_field="branch"):
    """Scope operational object URLs while preserving owner-only legacy access.

    Current branch workflow links always carry an explicit Branch. Older owner
    links and tests may not; those retain tenant-wide Product lookup, but never
    for branch-restricted memberships.
    """
    source = request.POST if request.method == "POST" else request.GET
    if (
        request.membership.allowed_branch_ids is None
        and not source.get(post_field)
    ):
        return _catalog_product_queryset(request)
    return _catalog_product_queryset(request, branch)


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------
@module_permission_required("pos_core", "products.view")
def product_list(request):
    from . import services as catalog_services

    selected_branch, branches = _catalog_branch_context(request)
    selected_warehouse, warehouses = _catalog_warehouse_context(
        request, selected_branch
    )
    qs = (
        Product.objects.for_business(request.business)
        .select_related("category", "brand", "unit", "tax_rate")
    )
    if selected_branch is not None:
        qs = catalog_services.products_visible_in_branch(
            qs,
            business=request.business,
            branch=selected_branch,
        )
    else:
        qs = qs.none()
    if not _tailoring_enabled(request):
        qs = qs.filter(is_tailoring_item=False)
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

    scoped_product_count = qs.count()
    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))

    # Retail stock follows the selected sales warehouse/branch. Shared Meter
    # fabric follows the configured Workshop warehouse, matching POS.
    retail_stock_qs = (
        inventory.StockLevel.objects.for_business(request.business)
        .filter(product__in=[p.pk for p in page_obj])
    )
    if selected_warehouse is not None:
        retail_stock_qs = retail_stock_qs.filter(warehouse=selected_warehouse)
    elif selected_branch is not None:
        retail_stock_qs = retail_stock_qs.filter(
            warehouse__branch=selected_branch,
            warehouse__is_active=True,
        )
    retail_stock = {
        row["product_id"]: row["total"]
        for row in retail_stock_qs.values("product_id").annotate(total=Sum("quantity"))
    }
    shared_warehouse = inventory.configured_shared_fabric_warehouse(request.business)
    shared_stock = {}
    if shared_warehouse is not None:
        shared_stock = {
            row["product_id"]: row["total"]
            for row in (
                inventory.StockLevel.objects.for_business(request.business)
                .filter(
                    warehouse=shared_warehouse,
                    product__in=[p.pk for p in page_obj],
                )
                .values("product_id")
                .annotate(total=Sum("quantity"))
            )
        }
    for p in page_obj:
        source_stock = shared_stock if p.is_meter_tailoring else retail_stock
        p.total_stock = source_stock.get(p.pk, 0)

    categories = Category.objects.for_business(request.business).filter(is_active=True)
    _p_cur, p_lim, _ = subscriptions.limit_state(request.business, "products")
    return render(request, "catalog/product_list.html", {
        "page_obj": page_obj, "q": q, "categories": categories,
        "active_nav": "products",
        "product_count": scoped_product_count,
        "product_limit": p_lim,
        "querystring": _qs_without_page(request),
        "branches": branches,
        "selected_branch": selected_branch,
        "warehouses": warehouses,
        "selected_warehouse": selected_warehouse,
        "has_active_warehouses": warehouses.exists(),
        "branch_locked": request.membership.allowed_branch_ids is not None,
        "context_ready": (
            selected_branch is not None and selected_warehouse is not None
        ),
    })


def _qs_without_page(request):
    params = request.GET.copy()
    params.pop("page", None)
    encoded = params.urlencode()
    return f"{encoded}&" if encoded else ""


@module_permission_required("pos_core", "products.export")
def product_export(request):
    """Export re-importable Products for one selected branch warehouse."""
    from apps.reports import exports

    from . import services as catalog_services

    def _int(name):
        v = request.GET.get(name, "")
        return int(v) if v.isdigit() else None

    selected_branch, _branches = _catalog_branch_context(request)
    if selected_branch is None:
        raise Http404
    selected_warehouse, _warehouses = _catalog_warehouse_context(
        request, selected_branch, required=True
    )
    allowed_branch_ids = {selected_branch.pk}
    allowed_warehouse_ids = {selected_warehouse.pk}

    filters = {
        "category_id": _int("category"),
        "brand_id": _int("brand"),
        "branch_id": selected_branch.pk,
        "warehouse_id": selected_warehouse.pk,
        "status": request.GET.get("status", ""),
        "include_tailoring": _tailoring_enabled(request),
    }
    data = catalog_services.product_export_dataset(
        request.business,
        filters,
        allowed_branch_ids=allowed_branch_ids,
        allowed_warehouse_ids=allowed_warehouse_ids,
    )
    if request.GET.get("format") == "xlsx":
        return exports.export_xlsx("products", data)
    return exports.export_csv("products", data)


@module_permission_required("pos_core", "products.manage")
def product_form(request, public_id=None):
    from . import services as catalog_services

    selected_branch, branches = _catalog_branch_context(request)
    if selected_branch is None and public_id:
        available = list(branches.order_by("id")[:2])
        if len(available) == 1:
            selected_branch = available[0]
    if selected_branch is None:
        messages.info(request, "Select a branch before adding or editing a Product.")
        return redirect("catalog:product_list")
    selected_warehouse, warehouses = _catalog_warehouse_context(
        request,
        selected_branch,
        post_field="opening_warehouse",
    )
    instance = None
    if public_id:
        instance = get_tenant_object(
            _catalog_product_object_queryset(request, selected_branch),
            request.business,
            public_id=public_id,
        )
        _require_tailoring_product_access(
            request,
            instance,
            permission_code="products.manage",
            action=AccessAction.WRITE,
        )
    tailoring_enabled = _tailoring_enabled(request)
    allowed_warehouse_ids = _allowed_warehouse_ids(request)
    if selected_branch is not None:
        branch_warehouse_ids = set(
            selected_branch.warehouses.filter(is_active=True).values_list(
                "id", flat=True
            )
        )
        if allowed_warehouse_ids is None:
            allowed_warehouse_ids = branch_warehouse_ids
        else:
            allowed_warehouse_ids = (
                set(allowed_warehouse_ids) & branch_warehouse_ids
            )
    form_data = request.POST or None
    if (
        request.method == "POST"
        and selected_warehouse is not None
        and not request.POST.get("opening_warehouse")
    ):
        form_data = request.POST.copy()
        form_data["opening_warehouse"] = str(selected_warehouse.pk)
    form = ProductForm(
        request.business,
        form_data,
        request.FILES or None,
        instance=instance,
        allowed_warehouse_ids=allowed_warehouse_ids,
        tailoring_enabled=tailoring_enabled,
        selected_warehouse=selected_warehouse,
        require_branch_warehouse=True,
        allow_product_reuse=instance is None,
        lock_global_fields=(
            instance is not None
            and request.membership.allowed_branch_ids is not None
        ),
    )
    form_context = {
        "form": form,
        "product": instance,
        "active_nav": "products",
        "tailoring_enabled": tailoring_enabled,
        "selected_branch": selected_branch,
        "selected_warehouse": selected_warehouse,
        "warehouses": warehouses,
        "branch_locked": request.membership.allowed_branch_ids is not None,
    }
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
            return render(request, "catalog/product_form.html", form_context)

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
                    return render(request, "catalog/product_form.html", form_context)
            # A concurrent ledger write may have appeared after form.clean().
            # The locked recheck above is authoritative before any edit write.
            reused_product = False
            if instance is not None:
                product = construct_instance(
                    form,
                    locked_product,
                    form._meta.fields,
                    form._meta.exclude,
                )
                form.instance = product
            else:
                try:
                    product = catalog_services.find_reusable_product(
                        request.business,
                        name=form.cleaned_data["name"],
                        sku=form.cleaned_data.get("sku", ""),
                        barcode=form.cleaned_data.get("barcode", ""),
                        category=form.cleaned_data.get("category"),
                        brand=form.cleaned_data.get("brand"),
                        unit=form.cleaned_data.get("unit"),
                        product_type=form.cleaned_data["product_type"],
                    )
                except ValidationError as exc:
                    form.add_error(None, exc)
                    return render(request, "catalog/product_form.html", form_context)
                reused_product = product is not None
                if product is None:
                    _current, limit, allowed = subscriptions.limit_state(
                        request.business, "products"
                    )
                    if not allowed:
                        form.add_error(
                            None,
                            f"Plan product limit ({limit}) reached.",
                        )
                        return render(
                            request, "catalog/product_form.html", form_context
                        )
                    product = form.save(commit=False)
            onboarding_savepoint = transaction.savepoint()
            if not reused_product:
                if auto_sku and not product.sku:
                    product.sku = catalog_services.generate_sku(request.business)
                product = catalog_services.save_product(
                    product=product, business=request.business, user=request.user,
                    membership=request.membership, request=request,
                )

            opening = form.cleaned_data.get("opening_stock")
            warehouse = (
                form.cleaned_data.get("opening_warehouse")
                or selected_warehouse
            )
            if (
                not public_id
                and warehouse
                and product.is_stocked
                and not product.has_variants
                and not (product.unit_id and product.unit.is_meter)
            ):
                try:
                    catalog_services.ensure_branch_opening_stock(
                        business=request.business,
                        warehouse=warehouse,
                        product=product,
                        quantity=opening or Decimal("0"),
                        unit_cost=product.purchase_price,
                        user=request.user,
                        membership=request.membership,
                        request=request,
                    )
                except ValidationError as exc:
                    transaction.savepoint_rollback(onboarding_savepoint)
                    form.add_error(None, exc)
                    return render(
                        request, "catalog/product_form.html", form_context
                    )

            created_variants = 0
            if is_variant and variant_rows:
                try:
                    created_variants = _create_variants(
                        request, product, variant_rows, warehouse
                    )
                except ValidationError as exc:
                    transaction.savepoint_rollback(onboarding_savepoint)
                    form.add_error(None, exc)
                    return render(
                        request, "catalog/product_form.html", form_context
                    )

            transaction.savepoint_commit(onboarding_savepoint)

        audit.log("product.saved", request=request, module="catalog", obj=product,
                  description=f"Product '{product.name}' saved"
                              + (f" with {created_variants} variant(s)."
                                 if created_variants else "."))
        messages.success(request, "Product saved.")
        if product.has_variants:
            target = reverse("catalog:product_detail", args=[product.public_id])
            if selected_branch is not None:
                target += (
                    f"?branch={selected_branch.pk}&warehouse={warehouse.pk}"
                )
            return redirect(target)
        target = reverse("catalog:product_list")
        if selected_branch is not None:
            target += f"?branch={selected_branch.pk}&warehouse={warehouse.pk}"
        return redirect(target)
    return render(request, "catalog/product_form.html", form_context)


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

    Each row is normalised to a dict and identifiers are checked within the
    submitted batch. Existing identifiers are resolved safely against the
    selected parent later, inside the atomic save transaction.
    """
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
            seen_sku.add(sku)
        if barcode:
            if barcode in seen_barcode:
                errors.append(f"Variant {idx}: barcode '{barcode}' is repeated.")
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
    """Create/reuse variants and selected-warehouse stock atomically."""
    from . import services as catalog_services

    business = request.business
    reserved = set()
    created = 0
    for row in rows:
        sku = row["sku"]
        variant = catalog_services.find_reusable_variant(
            business,
            product=product,
            name=row["name"],
            attributes=row["attributes"],
            sku=sku,
            barcode=row["barcode"],
        )
        if variant is None:
            if not sku:
                sku = catalog_services.generate_sku(
                    business, taken=reserved
                )
            reserved.add(sku)
            variant = ProductVariant(
                business=business, product=product, name=row["name"],
                attributes=row["attributes"], sku=sku, barcode=row["barcode"],
                purchase_price=row["purchase_price"],
                sale_price=(
                    Decimal("0")
                    if product.is_meter_tailoring
                    else row["sale_price"]
                ),
            )
            variant = catalog_services.save_variant(
                variant=variant, product=product, user=request.user,
                membership=request.membership, request=request,
            )
            created += 1
        if warehouse and product.is_stocked:
            catalog_services.ensure_branch_opening_stock(
                business=business,
                warehouse=warehouse,
                product=product,
                variant=variant,
                quantity=row["opening_stock"],
                unit_cost=row["purchase_price"],
                user=request.user,
                membership=request.membership,
                request=request,
            )
    return created


@module_permission_required("pos_core", "products.view")
def product_detail(request, public_id):
    selected_branch, branches = _catalog_branch_context(request)
    if selected_branch is None:
        available = list(branches.order_by("id")[:2])
        if len(available) == 1:
            selected_branch = available[0]
    if selected_branch is None:
        raise Http404
    selected_warehouse, _warehouses = _catalog_warehouse_context(
        request, selected_branch, required=True
    )
    product = get_tenant_object(
        _catalog_product_object_queryset(request, selected_branch).select_related(
            "category", "brand", "unit", "tax_rate"
        ),
        request.business, public_id=public_id,
    )
    _require_tailoring_product_access(
        request, product, permission_code="products.view"
    )
    variants = list(product.variants.all())
    levels = (
        inventory.StockLevel.objects.for_business(request.business)
        .filter(product=product).select_related("warehouse", "variant")
    )
    movements = (
        inventory.StockMovement.objects.for_business(request.business)
        .filter(product=product).select_related("warehouse", "variant", "user")
    )
    levels = list(levels.filter(warehouse=selected_warehouse))
    stock_by_variant = {
        level.variant_id: level.quantity
        for level in levels
        if level.variant_id is not None
    }
    for variant in variants:
        variant.current_stock = stock_by_variant.get(variant.id, Decimal("0"))
    movements = movements.filter(warehouse=selected_warehouse)
    movements = movements[:30]
    show_cost = request.membership.has_perm("cost.view")
    return render(request, "catalog/product_detail.html", {
        "product": product, "variants": variants, "levels": levels,
        "movements": movements, "active_nav": "products", "show_cost": show_cost,
        "selected_branch": selected_branch,
        "selected_warehouse": selected_warehouse,
    })


@module_permission_required("pos_core", "products.archive")
def product_archive(request, public_id):
    selected_branch, _branches = _catalog_branch_context(request)
    product = get_tenant_object(
        _catalog_product_object_queryset(request, selected_branch),
        request.business,
        public_id=public_id,
    )
    _require_tailoring_product_access(
        request,
        product,
        permission_code="products.archive",
        action=AccessAction.WRITE,
    )
    if request.method == "POST":
        # Products referenced by invoices are archived, never deleted.
        from . import services as catalog_services

        product = catalog_services.archive_product(
            product=product, user=request.user,
            membership=request.membership, request=request,
        )
        audit.log("product.archived", request=request, module="catalog", obj=product,
                  description=f"Product '{product.name}' archived.")
        messages.success(request, f"'{product.name}' archived.")
    target = reverse("catalog:product_list")
    if selected_branch is not None:
        target += f"?branch={selected_branch.pk}"
    return redirect(target)


@module_permission_required("pos_core", "products.archive")
def product_restore(request, public_id):
    from . import services as catalog_services

    selected_branch, _branches = _catalog_branch_context(request)
    product = get_tenant_object(
        _catalog_product_object_queryset(request, selected_branch),
        request.business,
        public_id=public_id,
    )
    _require_tailoring_product_access(
        request,
        product,
        permission_code="products.archive",
        action=AccessAction.WRITE,
    )
    if request.method == "POST":
        product = catalog_services.restore_product(
            product, user=request.user, membership=request.membership,
            request=request,
        )
        audit.log("product.restored", request=request, module="catalog", obj=product,
                  description=f"Product '{product.name}' restored from archive.")
        messages.success(request, f"'{product.name}' restored and active again.")
    target = reverse("catalog:product_detail", args=[public_id])
    if selected_branch is not None:
        target += f"?branch={selected_branch.pk}"
    return redirect(target)


@module_permission_required("pos_core", "products.delete")
def product_delete(request, public_id):
    from . import services as catalog_services

    selected_branch, _branches = _catalog_branch_context(request)
    selected_warehouse, _warehouses = _catalog_warehouse_context(
        request, selected_branch
    )
    product = get_tenant_object(
        _catalog_product_object_queryset(request, selected_branch),
        request.business,
        public_id=public_id,
    )
    _require_tailoring_product_access(
        request,
        product,
        permission_code="products.delete",
        action=AccessAction.WRITE,
    )
    if request.method == "POST":
        name, ref = product.name, str(product.public_id)
        try:
            catalog_services.delete_product_if_safe(
                product, user=request.user, membership=request.membership,
                request=request,
            )
        except catalog_services.ProductInUse as exc:
            messages.error(request, str(exc))
            if selected_branch is not None and selected_warehouse is not None:
                target = reverse("catalog:product_detail", args=[public_id])
                target += (
                    f"?branch={selected_branch.pk}"
                    f"&warehouse={selected_warehouse.pk}"
                )
            else:
                target = reverse("catalog:product_list")
                if selected_branch is not None:
                    target += f"?branch={selected_branch.pk}"
            return redirect(target)
        audit.log("product.deleted", request=request, module="catalog",
                  description=f"Product '{name}' ({ref}) hard-deleted "
                              "(no transaction history).")
        messages.success(request, f"'{name}' permanently deleted.")
        target = reverse("catalog:product_list")
        if selected_branch is not None:
            target += f"?branch={selected_branch.pk}"
        return redirect(target)
    target = reverse("catalog:product_detail", args=[public_id])
    if selected_branch is not None:
        target += f"?branch={selected_branch.pk}"
        if selected_warehouse is not None:
            target += f"&warehouse={selected_warehouse.pk}"
    return redirect(target)


@module_permission_required("pos_core", "products.manage")
def variant_form(request, product_id, public_id=None):
    selected_branch, _branches = _catalog_branch_context(request)
    product = get_tenant_object(
        _catalog_product_object_queryset(request, selected_branch),
        request.business,
        public_id=product_id,
    )
    _require_tailoring_product_access(
        request,
        product,
        permission_code="products.manage",
        action=AccessAction.WRITE,
    )
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
                variant.attributes = form.build_attributes()
                if not variant.name:
                    variant.name = " / ".join(variant.attributes.values()) or "Variant"
                from . import services as catalog_services

                variant = catalog_services.save_variant(
                    variant=variant, product=locked_product, user=request.user,
                    membership=request.membership, request=request,
                )
                if locked_product.product_type != Product.Type.VARIANT:
                    locked_product.product_type = Product.Type.VARIANT
                    locked_product.save(update_fields=["product_type"])
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "Variant saved.")
            target = reverse("catalog:product_detail", args=[product.public_id])
            if selected_branch is not None:
                target += f"?branch={selected_branch.pk}"
            return redirect(target)
    return render(request, "catalog/variant_form.html",
                  {"form": form, "product": product, "variant": instance,
                   "active_nav": "products",
                   "selected_branch": selected_branch})


# ---------------------------------------------------------------------------
# Barcode generation / labels
# ---------------------------------------------------------------------------
@module_permission_required("barcode_printing", "products.view")
def product_barcode_svg(request, public_id):
    """Server-generated Code128 barcode as SVG."""
    import barcode
    from barcode.writer import SVGWriter

    selected_branch, _branches = _catalog_branch_context(request)
    product = get_tenant_object(
        _catalog_product_object_queryset(request, selected_branch),
        request.business,
        public_id=public_id,
    )
    _require_tailoring_product_access(
        request, product, permission_code="products.view"
    )
    code = product.barcode or product.sku or f"P{product.pk:08d}"
    buffer = io.BytesIO()
    barcode.get("code128", code, writer=SVGWriter()).write(buffer)
    return HttpResponse(buffer.getvalue(), content_type="image/svg+xml")


@module_permission_required("barcode_printing", "products.view")
def product_labels(request, public_id):
    selected_branch, _branches = _catalog_branch_context(request)
    product = get_tenant_object(
        _catalog_product_object_queryset(request, selected_branch),
        request.business,
        public_id=public_id,
    )
    _require_tailoring_product_access(
        request, product, permission_code="products.view"
    )
    try:
        count = max(1, min(int(request.GET.get("count", 12)), 120))
    except ValueError:
        count = 12
    return render(request, "catalog/labels.html", {
        "product": product, "count_range": range(count),
        "business": request.business,
        "selected_branch": selected_branch,
    })


# ---------------------------------------------------------------------------
# Categories / brands / units / taxes (combined setup screens)
# ---------------------------------------------------------------------------
def _simple_crud(request, model, form_class, list_template, name, perm="products.manage",
                 extra=None):
    @module_permission_required("pos_core", perm)
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


@module_permission_required("pos_core", "products.manage")
def category_list(request):
    return _simple_crud(request, Category, CategoryForm,
                        "catalog/category_list.html", "Category")


@module_permission_required("pos_core", "products.manage")
def brand_list(request):
    return _simple_crud(request, Brand, BrandForm, "catalog/brand_list.html", "Brand")


@module_permission_required("pos_core", "products.manage")
def unit_list(request):
    return _simple_crud(request, Unit, UnitForm, "catalog/unit_list.html", "Unit")


@module_permission_required("pos_core", "settings.manage")
def tax_list(request):
    return _simple_crud(request, TaxRate, TaxRateForm, "catalog/tax_list.html",
                        "Tax rate", perm="settings.manage")


@module_permission_required("pos_core", "products.import")
def product_import(request):
    from apps.core.imports import error_report_response, parse_tabular_file

    from . import services as catalog_services

    if request.GET.get("errors") == "1":
        errors = request.session.get("product_import_errors", [])
        return error_report_response("product_import_errors.csv", errors)

    selected_branch, branches = _catalog_branch_context(request)
    if selected_branch is None:
        messages.info(request, "Select a Branch before importing Products.")
        return redirect("catalog:product_list")
    selected_warehouse, warehouses = _catalog_warehouse_context(
        request,
        selected_branch,
        required=request.method == "POST" and selected_branch is not None,
    )
    form = ProductImportForm(request.POST or None, request.FILES or None)
    results = None
    import_error = None
    if request.method == "POST" and form.is_valid():
        try:
            subscriptions.require_operational(request.business)
        except subscriptions.SubscriptionInactive as exc:
            messages.error(request, str(exc))
            target = reverse("catalog:product_list")
            if selected_branch is not None:
                target += f"?branch={selected_branch.pk}"
            return redirect(target)
        rows, parse_error = parse_tabular_file(form.cleaned_data["file"])
        if parse_error:
            import_error = parse_error
            messages.error(request, parse_error)
        else:
            summary, errors = catalog_services.import_products(
                business=request.business, rows=rows,
                match_by=form.cleaned_data["match_by"], user=request.user,
                allowed_warehouse_ids=_allowed_warehouse_ids(request),
                membership=request.membership, request=request,
                branch_context_mode=(
                    "branch"
                ),
                selected_branch=selected_branch,
                selected_warehouse=selected_warehouse,
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
                   "active_nav": "products",
                   "branches": branches,
                   "warehouses": warehouses,
                   "selected_branch": selected_branch,
                   "selected_warehouse": selected_warehouse,
                   "branch_locked": request.membership.allowed_branch_ids is not None,
                   "business_wide_context": False})


@module_permission_required("pos_core", "products.import")
def import_template(request):
    from apps.reports import exports

    from . import services as catalog_services

    selected_branch, _branches = _catalog_branch_context(request)
    if selected_branch is None:
        raise Http404
    selected_warehouse, _warehouses = _catalog_warehouse_context(
        request,
        selected_branch,
        required=True,
    )

    branch_values = (
        [
            selected_branch.code,
            selected_branch.name,
            selected_warehouse.code,
            selected_warehouse.name,
        ]
        if selected_branch is not None
        else ["", "", "", ""]
    )

    data = {
        "columns": catalog_services.EXPORT_COLUMNS,
        "rows": [
            [
                "Hi Sofy", "FAB-HI-SOFY", "", "Fabrics", "Hi Sofy",
                "variant", "Meter", "2.500", "0", "2.500", "", "",
                "Yes", "80", "0", *branch_values,
                "Color Code", "Color 1", "FAB-HI-SOFY-C1", "6299801000010",
                "Yes", "No",
            ],
            [
                "Hi Sofy", "FAB-HI-SOFY", "", "Fabrics", "Hi Sofy",
                "variant", "Meter", "2.500", "0", "2.500", "", "",
                "Yes", "60", "0", *branch_values,
                "Color Code", "Color 2", "FAB-HI-SOFY-C2", "6299801000027",
                "Yes", "No",
            ],
        ],
        "totals": None,
    }
    if request.GET.get("format") == "xlsx":
        return exports.export_xlsx("product_import_template", data)
    return exports.export_csv("product_import_template", data)
