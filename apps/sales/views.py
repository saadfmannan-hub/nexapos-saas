import json

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.date_ranges import date_range_querystring, resolve_date_range
from apps.core.mixins import get_tenant_object
from apps.core.money import D, money
from apps.customers import services as customer_services
from apps.customers.models import Customer
from apps.inventory.models import StockLevel
from apps.registers import services as register_services
from apps.subscriptions import services as subscriptions
from apps.subscriptions.access import (
    AccessAction,
    evaluate_access,
    get_access_context,
    require_access,
)
from apps.subscriptions.decorators import module_permission_required
from apps.subscriptions.exceptions import ModuleAccessDenied

from . import calculations, services
from .forms import ActualFabricForm
from .models import (
    MAX_FABRIC_TOTAL,
    HeldSale,
    PaymentMethod,
    Sale,
    SaleItem,
    SaleReturn,
)
from .services import SaleError


def _user_branches(request):
    """Branches the member may operate in."""
    from apps.branches.models import Branch

    qs = Branch.objects.for_business(request.business).filter(is_active=True)
    allowed = request.membership.allowed_branch_ids
    if allowed is not None:
        qs = qs.filter(id__in=allowed)
    return qs


def _sales_for_request(request, queryset=None):
    """Scope sale reads and mutations to the member's assigned branches."""
    qs = queryset if queryset is not None else Sale.objects.all()
    allowed = request.membership.allowed_branch_ids
    if allowed is None:
        return qs
    return qs.filter(
        branch_id__in=allowed,
        warehouse__business=request.business,
    ).filter(
        Q(warehouse__branch_id__in=allowed) | Q(warehouse__branch__isnull=True)
    )


def _held_sales_for_request(request):
    qs = HeldSale.objects.for_business(request.business).filter(cashier=request.user)
    allowed = request.membership.allowed_branch_ids
    return qs if allowed is None else qs.filter(branch_id__in=allowed)


def _tailoring_enabled(request):
    return get_access_context(request).has_module("tailoring")


def _visible_held_sales(request, held_sales):
    """Hide Tailoring carts after downgrade without affecting retail carts."""

    held_sales = list(held_sales)
    if _tailoring_enabled(request):
        return held_sales

    from apps.catalog.models import Product

    product_ids = {
        line.get("product_id")
        for held in held_sales
        for line in (
            (held.cart or {}).get("items", [])
            if isinstance(held.cart, dict)
            else []
        )
        if isinstance(line, dict) and line.get("product_id") is not None
    }
    tailoring_ids = {
        str(product_id)
        for product_id in Product.objects.for_business(request.business)
        .filter(pk__in=product_ids, is_tailoring_item=True)
        .values_list("pk", flat=True)
    }
    return [
        held
        for held in held_sales
        if not any(
            str(line.get("product_id")) in tailoring_ids
            for line in (
                (held.cart or {}).get("items", [])
                if isinstance(held.cart, dict)
                else []
            )
            if isinstance(line, dict) and line.get("product_id") is not None
        )
    ]


def _branch_warehouse(branch):
    """Default warehouse used when selling from a branch."""
    from apps.branches.models import Warehouse

    return (
        Warehouse.objects.for_business(branch.business)
        .filter(is_active=True)
        .filter(Q(branch=branch) | Q(branch__isnull=True))
        .order_by("-is_default")
        .first()
    )


TAILORING_CHECKOUT_FIELDS = (
    "design_type",
    "daraz_details",
    "vip_3d_design",
    "computer_design",
    "customer_notes",
    "workshop_notes",
)


def _checkout_tailoring_details(raw):
    raw_details = raw.get("tailoring_details") or raw.get("tailoring") or {}
    if not isinstance(raw_details, dict):
        return raw_details
    return {
        key: str(raw_details.get(key, "") or "").strip()
        for key in TAILORING_CHECKOUT_FIELDS
        if str(raw_details.get(key, "") or "").strip()
    }


def _checkout_priority(payload, raw_items):
    raw_priority = str(payload.get("priority") or "").strip().lower()
    if raw_priority:
        return raw_priority
    legacy_priorities = {
        str((item.get("tailoring_details") or {}).get("priority") or "").lower()
        for item in raw_items
        if isinstance(item, dict) and isinstance(item.get("tailoring_details"), dict)
    }
    if "urgent" in legacy_priorities:
        return Sale.Priority.URGENT
    if legacy_priorities.intersection({"high", "vip"}):
        return Sale.Priority.HIGH
    return Sale.Priority.NORMAL


def _sale_error_response(exc, *, status=400):
    body = {"ok": False, "error": str(exc)}
    if getattr(exc, "errors", None):
        body["errors"] = exc.errors
    return JsonResponse(body, status=status)


def _checkout_success_response(sale):
    return JsonResponse({"ok": True, "sale": {
        "public_id": str(sale.public_id),
        "invoice_number": sale.invoice_number,
        "total": str(sale.total),
        "change_due": str(sale.change_due),
        "receipt_url": f"/sales/{sale.public_id}/receipt/",
        "invoice_url": f"/sales/{sale.public_id}/invoice/",
    }})


# ---------------------------------------------------------------------------
# POS screen
# ---------------------------------------------------------------------------
@module_permission_required("pos_core", "sales.create", action="write")
def pos_view(request):
    from apps.accounts.services import post_login_redirect
    from apps.catalog.models import Category

    branches = list(_user_branches(request))
    if not branches:
        messages.error(request, "You are not assigned to any active branch.")
        return post_login_redirect(request, excluded_routes={"sales:pos"})

    shift = register_services.get_open_shift(
        request.business, request.user, membership=request.membership
    )
    branch = shift.branch if shift else branches[0]
    warehouse = _branch_warehouse(branch)
    if warehouse is None:
        messages.error(request, "No active warehouse is configured for this branch.")
        return post_login_redirect(request, excluded_routes={"sales:pos"})

    categories = Category.objects.for_business(request.business).filter(is_active=True)
    credit_module_write = evaluate_access(
        request, "customer_credit", action=AccessAction.WRITE
    ).allowed
    can_credit = credit_module_write and request.membership.has_perm("sales.credit")
    payment_methods = PaymentMethod.objects.for_business(request.business).filter(
        is_active=True
    )
    if not credit_module_write:
        payment_methods = payment_methods.exclude(
            kind__in=[
                PaymentMethod.Kind.CUSTOMER_CREDIT,
                PaymentMethod.Kind.STORE_CREDIT,
            ]
        )
    elif not can_credit:
        payment_methods = payment_methods.exclude(
            kind=PaymentMethod.Kind.CUSTOMER_CREDIT
        )
    held_sales = _visible_held_sales(
        request,
        _held_sales_for_request(request).order_by("-created_at")[:20],
    )
    walk_in = Customer.objects.for_business(request.business).filter(
        home_branch=branch,
        is_walk_in=True,
        is_active=True,
    ).first()
    if walk_in is None:
        from apps.customers.services import ensure_walk_in_customer

        walk_in = ensure_walk_in_customer(request.business, branch)
    settings_obj = request.business.settings
    can_sell_without_shift = settings_obj.allow_sale_without_shift
    vat_rate = settings_obj.effective_vat_rate

    return render(request, "pos/pos.html", {
        "active_nav": "pos",
        "branch": branch,
        "warehouse": warehouse,
        "shift": shift,
        "categories": categories,
        "payment_methods": payment_methods,
        "held_sales": held_sales,
        "walk_in": walk_in,
        "can_sell_without_shift": can_sell_without_shift,
        "currency": request.business.currency_display,
        "precision": request.business.currency_precision,
        "max_discount": settings_obj.max_discount_percent,
        "can_discount": request.membership.has_perm("sales.discount"),
        "can_override_price": request.membership.has_perm("sales.price_override"),
        "can_credit": can_credit,
        "credit_module_write": credit_module_write,
        "vat_enabled": settings_obj.vat_enabled,
        "vat_rate": vat_rate,
        "show_vat_on_invoice_receipt": settings_obj.show_vat_on_invoice_receipt,
        "today": timezone.localdate(),
        "tailoring_enabled": _tailoring_enabled(request),
    })


@module_permission_required("pos_core", "sales.create")
def pos_products(request):
    """JSON product grid/search for the POS screen."""
    from apps.branches.models import Warehouse
    from apps.catalog.models import Product

    q = request.GET.get("q", "").strip()
    category_id = request.GET.get("category", "")
    qs = (
        Product.objects.for_business(request.business)
        .filter(is_active=True, is_archived=False)
        .select_related("brand", "tax_rate", "unit")
        .prefetch_related("variants")
    )
    if not _tailoring_enabled(request):
        qs = qs.filter(is_tailoring_item=False)
    if q:
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(brand__business=request.business, brand__name__icontains=q)
            | Q(sku__icontains=q)
            | Q(barcode__icontains=q)
            | Q(internal_code__icontains=q)
        )
    if category_id.isdigit():
        qs = qs.filter(Q(category_id=category_id) | Q(category__parent_id=category_id))
    qs = qs.order_by("name")[:60]

    warehouse_id = request.GET.get("warehouse_id")
    stock_map = {}
    if warehouse_id and str(warehouse_id).isdigit():
        warehouse_qs = Warehouse.objects.for_business(request.business).filter(
            pk=warehouse_id,
            is_active=True,
        )
        allowed = request.membership.allowed_branch_ids
        if allowed is not None:
            warehouse_qs = warehouse_qs.filter(branch_id__in=allowed)
        if not warehouse_qs.exists():
            return JsonResponse(
                {"items": [], "error": "Invalid warehouse."}, status=403,
            )
        for row in StockLevel.objects.for_business(request.business).filter(
            warehouse_id=warehouse_id, product__in=[p.pk for p in qs]
        ).values("product_id", "variant_id", "quantity"):
            stock_map[(row["product_id"], row["variant_id"])] = float(row["quantity"])

    items = []
    for p in qs:
        tax_rate = calculations.resolve_tax_rate(request.business, p)
        brand_name = (
            p.brand.name
            if p.brand_id and p.brand.business_id == request.business.id
            else ""
        )
        if p.has_variants:
            for v in p.variants.all():
                if not v.is_active:
                    continue
                items.append({
                    "product_id": p.id, "variant_id": v.id,
                    "name": f"{p.name} — {v.name}",
                    "brand": brand_name,
                    "price": (
                        "0"
                        if p.is_meter_tailoring
                        else str(v.sale_price if v.sale_price > 0 else p.sale_price)
                    ),
                    "sku": v.sku or p.sku,
                    "tax_rate": str(tax_rate),
                    "stocked": p.is_stocked,
                    "allow_discount": p.allow_discount,
                    "is_tailoring_item": p.is_tailoring_item,
                    "is_meter_tailoring": p.is_meter_tailoring,
                    "is_legacy_tailoring": p.is_legacy_tailoring,
                    "unit": p.unit.abbreviation if p.unit else "",
                    "min_price": str(p.minimum_sale_price),
                    "stock": stock_map.get((p.id, v.id), None),
                    "image": v.image.url if v.image else (p.image.url if p.image else None),
                })
        else:
            items.append({
                "product_id": p.id, "variant_id": None,
                "name": p.name,
                "brand": brand_name,
                "price": "0" if p.is_meter_tailoring else str(p.sale_price),
                "sku": p.sku,
                "tax_rate": str(tax_rate),
                "stocked": p.is_stocked,
                "allow_discount": p.allow_discount,
                "is_tailoring_item": p.is_tailoring_item,
                "is_meter_tailoring": p.is_meter_tailoring,
                "is_legacy_tailoring": p.is_legacy_tailoring,
                "unit": p.unit.abbreviation if p.unit else "",
                "min_price": str(p.minimum_sale_price),
                "stock": stock_map.get((p.id, None), None),
                "image": p.image.url if p.image else None,
            })
    return JsonResponse({"items": items})


@module_permission_required("pos_core", "sales.create")
def pos_barcode(request):
    """Exact barcode/SKU lookup — used by scanner input."""
    from apps.catalog.models import Product, ProductVariant

    code = request.GET.get("code", "").strip()
    if not code:
        return JsonResponse({"found": False})
    tailoring_enabled = _tailoring_enabled(request)
    variant = (
        ProductVariant.objects.for_business(request.business)
        .filter(Q(barcode=code) | Q(sku=code), is_active=True)
        .select_related("product__tax_rate", "product__unit", "product")
        .first()
    )
    if variant and variant.product.is_tailoring_item and not tailoring_enabled:
        variant = None
    if variant and variant.product.is_active and not variant.product.is_archived:
        p = variant.product
        tax_rate = calculations.resolve_tax_rate(request.business, p)
        return JsonResponse({"found": True, "item": {
            "product_id": p.id, "variant_id": variant.id,
            "name": f"{p.name} — {variant.name}",
            "price": (
                "0"
                if p.is_meter_tailoring
                else str(variant.sale_price if variant.sale_price > 0 else p.sale_price)
            ),
            "sku": variant.sku or p.sku, "tax_rate": str(tax_rate),
            "stocked": p.is_stocked, "allow_discount": p.allow_discount,
            "is_tailoring_item": p.is_tailoring_item,
            "is_meter_tailoring": p.is_meter_tailoring,
            "is_legacy_tailoring": p.is_legacy_tailoring,
            "unit": p.unit.abbreviation if p.unit else "",
            "min_price": str(p.minimum_sale_price),
        }})
    product = (
        Product.objects.for_business(request.business)
        .filter(Q(barcode=code) | Q(sku=code), is_active=True, is_archived=False)
        .select_related("tax_rate", "unit")
        .first()
    )
    if product and product.is_tailoring_item and not tailoring_enabled:
        product = None
    if product and not product.has_variants:
        tax_rate = calculations.resolve_tax_rate(request.business, product)
        return JsonResponse({"found": True, "item": {
            "product_id": product.id, "variant_id": None,
            "name": product.name,
            "price": "0" if product.is_meter_tailoring else str(product.sale_price),
            "sku": product.sku, "tax_rate": str(tax_rate),
            "stocked": product.is_stocked, "allow_discount": product.allow_discount,
            "is_tailoring_item": product.is_tailoring_item,
            "is_meter_tailoring": product.is_meter_tailoring,
            "is_legacy_tailoring": product.is_legacy_tailoring,
            "unit": product.unit.abbreviation if product.unit else "",
            "min_price": str(product.minimum_sale_price),
        }})
    return JsonResponse({"found": False})


@module_permission_required("pos_core", "sales.create")
def pos_customers(request):
    from apps.branches.models import Branch

    try:
        branch = Branch.objects.for_business(request.business).get(
            pk=request.GET.get("branch_id"),
            is_active=True,
        )
    except (Branch.DoesNotExist, ValueError):
        return JsonResponse({"results": [], "error": "Invalid branch."}, status=404)
    if not request.membership.can_access_branch(branch):
        return JsonResponse({"results": [], "error": "Invalid branch."}, status=404)
    q = request.GET.get("q", "").strip()
    qs = Customer.objects.for_business(request.business).filter(
        home_branch=branch,
        is_active=True,
    )
    if q:
        qs = qs.filter(Q(full_name__icontains=q) | Q(mobile__icontains=q) |
                       Q(code__icontains=q) | Q(email__icontains=q))
    credit_access = (
        evaluate_access(
            request, "customer_credit", action=AccessAction.READ
        ).allowed
    )
    results = [{
        "id": c.id, "name": c.full_name, "mobile": c.mobile,
        "balance": str(c.balance) if credit_access else "0",
        "store_credit": str(c.store_credit) if credit_access else "0",
        "credit_limit": str(c.credit_limit) if credit_access else "0",
        "is_walk_in": c.is_walk_in,
        "more_options": customer_services.more_option_values(request.business, c),
    } for c in qs.order_by("-is_walk_in", "full_name")[:15]]
    return JsonResponse({"results": results})


@require_POST
@module_permission_required("pos_core", "customers.manage")
def pos_quick_customer(request):
    """Create a customer from the POS without leaving the screen."""
    from apps.branches.models import Branch
    from apps.customers.services import next_customer_code

    try:
        branch = Branch.objects.for_business(request.business).get(
            pk=request.POST.get("branch_id"),
            is_active=True,
        )
    except (Branch.DoesNotExist, ValueError):
        return JsonResponse({"ok": False, "error": "Invalid branch."}, status=404)
    if not request.membership.can_access_branch(branch):
        return JsonResponse({"ok": False, "error": "Invalid branch."}, status=404)
    try:
        subscriptions.check_limit(request.business, "customers")
    except (subscriptions.LimitExceeded, subscriptions.SubscriptionInactive) as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    name = request.POST.get("name", "").strip()
    mobile = request.POST.get("mobile", "").strip()
    if not name:
        return JsonResponse({"ok": False, "error": "Name is required."}, status=400)
    if mobile and Customer.objects.for_business(request.business).filter(
        home_branch=branch,
        mobile=mobile,
    ).exists():
        return JsonResponse(
            {"ok": False, "error": "A customer with this mobile already exists."},
            status=400,
        )
    customer = customer_services.save_customer(
        customer=Customer(
            business=request.business,
            home_branch=branch,
            code=next_customer_code(request.business, branch),
            full_name=name[:160],
            mobile=mobile[:30],
        ),
        business=request.business,
        user=request.user,
        membership=request.membership,
        request=request,
    )
    return JsonResponse({"ok": True, "customer": {
        "id": customer.id, "name": customer.full_name, "mobile": customer.mobile,
        "balance": "0", "store_credit": "0", "credit_limit": "0", "is_walk_in": False,
        "more_options": [],
    }})


@require_POST
@module_permission_required("pos_core", "sales.create")
def pos_checkout(request):
    """Finalize the cart. Body: JSON contract from the POS screen."""
    from apps.branches.models import Branch
    from apps.catalog.models import Product, ProductVariant

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid request."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"ok": False, "error": "Invalid request."}, status=400)

    try:
        branch = Branch.objects.for_business(request.business).get(
            pk=payload.get("branch_id"), is_active=True
        )
    except Branch.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Invalid branch."}, status=400)
    if not request.membership.can_access_branch(branch):
        return JsonResponse({"ok": False, "error": "You cannot sell from this branch."},
                            status=403)
    warehouse = _branch_warehouse(branch)
    if warehouse is None:
        return JsonResponse({"ok": False, "error": "Branch has no warehouse."}, status=400)

    try:
        customer = Customer.objects.for_business(request.business).get(
            pk=payload.get("customer_id"),
            home_branch=branch,
            is_active=True,
        )
    except Customer.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Invalid customer."}, status=400)

    checkout_token = str(payload.get("checkout_token") or "").strip()
    if not checkout_token or len(checkout_token) > 64:
        return JsonResponse(
            {"ok": False, "error": "A valid checkout token is required."},
            status=400,
        )
    existing_sale = Sale.objects.for_business(request.business).filter(
        checkout_token=checkout_token,
    ).first()
    if existing_sale is not None:
        if (
            existing_sale.cashier_id != request.user.id
            or existing_sale.branch_id != branch.id
            or existing_sale.customer_id != customer.id
        ):
            return JsonResponse(
                {"ok": False, "error": "Invalid checkout token."},
                status=400,
            )
        if (
            services.sale_has_tailoring_lines(existing_sale)
            or existing_sale.delivery_date is not None
            or existing_sale.priority != Sale.Priority.NORMAL
        ):
            require_access(
                request,
                "tailoring",
                permission_code="sales.create",
                action=AccessAction.WRITE,
            )
        replay_payment_kinds = set(
            existing_sale.payments.values_list("method__kind", flat=True)
        )
        if PaymentMethod.Kind.CUSTOMER_CREDIT in replay_payment_kinds:
            require_access(
                request,
                "customer_credit",
                permission_code="sales.credit",
                action=AccessAction.WRITE,
            )
        elif PaymentMethod.Kind.STORE_CREDIT in replay_payment_kinds:
            require_access(
                request,
                "customer_credit",
                permission_code="sales.create",
                action=AccessAction.WRITE,
            )
        held_id = payload.get("held_id")
        if held_id:
            # Only clean the held cart that originally carried this token. A
            # replay must never delete another one of the cashier's carts.
            HeldSale.objects.for_business(request.business).filter(
                pk=held_id,
                cashier=request.user,
                branch=branch,
                cart__checkout_token=checkout_token,
            ).delete()
        return _checkout_success_response(existing_sale)

    held = None
    held_id = payload.get("held_id")

    shift = register_services.get_open_shift(
        request.business, request.user, membership=request.membership
    )

    raw_items = payload.get("items", [])
    if not isinstance(raw_items, list):
        return JsonResponse({"ok": False, "error": "Invalid cart."}, status=400)
    items = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            return JsonResponse({
                "ok": False,
                "error": "Invalid product in cart.",
                "errors": {f"items.{index}": "Invalid cart line."},
            }, status=400)
        try:
            product = Product.objects.for_business(request.business).select_related(
                "unit"
            ).get(pk=raw.get("product_id"), is_active=True, is_archived=False)
        except Product.DoesNotExist:
            return JsonResponse({"ok": False, "error": "Invalid product in cart."},
                                status=400)
        variant = None
        if raw.get("variant_id"):
            try:
                variant = ProductVariant.objects.for_business(request.business).get(
                    pk=raw["variant_id"], product=product, is_active=True
                )
            except ProductVariant.DoesNotExist:
                return JsonResponse({"ok": False, "error": "Invalid variant in cart."},
                                    status=400)
        line = {
            "product": product, "variant": variant,
            "quantity": D(raw.get("quantity")),
            "unit_price": D(raw.get("unit_price")),
            # POS line discounts were retired in favour of invoice discounts.
            # Keep the persisted field for historical and non-POS compatibility.
            "discount_amount": D("0"),
            "garment_classification": raw.get("garment_classification", ""),
            "collection_type": raw.get("collection_type", ""),
            "tailoring_details": _checkout_tailoring_details(raw),
        }
        # Key presence is significant: legacy service integrations that omit
        # it keep their historical compatibility path, while POS/held carts
        # always submit it and therefore must provide an explicit meter.
        if "fabric_meter_used" in raw:
            line["fabric_meter_used"] = raw.get("fabric_meter_used")
        items.append(line)

    if any(line["product"].is_tailoring_item for line in items):
        require_access(
            request,
            "tailoring",
            permission_code="sales.create",
            action=AccessAction.WRITE,
        )

    raw_payments = payload.get("payments", [])
    if not isinstance(raw_payments, list):
        return JsonResponse({"ok": False, "error": "Invalid payments."}, status=400)
    payments = []
    for raw in raw_payments:
        if not isinstance(raw, dict):
            return JsonResponse({"ok": False, "error": "Invalid payment."}, status=400)
        try:
            method = PaymentMethod.objects.for_business(request.business).get(
                pk=raw.get("method_id"), is_active=True
            )
        except PaymentMethod.DoesNotExist:
            return JsonResponse({"ok": False, "error": "Invalid payment method."},
                                status=400)
        payments.append({"method": method, "amount": D(raw.get("amount")),
                         "reference": str(raw.get("reference", ""))[:120]})

    delivery_date = None
    raw_delivery = str(payload.get("delivery_date") or "").strip()
    if raw_delivery:
        import datetime as _dt

        try:
            delivery_date = _dt.date.fromisoformat(raw_delivery)
        except ValueError:
            return JsonResponse({
                "ok": False,
                "error": "Invalid delivery date.",
                "errors": {"delivery_date": "Enter a valid delivery date."},
            }, status=400)
        if delivery_date < timezone.localdate():
            return JsonResponse({
                "ok": False,
                "error": "Delivery date cannot be in the past.",
                "errors": {"delivery_date": "Delivery date cannot be in the past."},
            }, status=400)

    try:
        with transaction.atomic():
            sale = None
            if held_id:
                held = (
                    HeldSale.objects.for_business(request.business)
                    .select_for_update()
                    .filter(pk=held_id, cashier=request.user)
                    .first()
                )
                if held is None:
                    # A concurrent request may have completed and removed the
                    # held cart while this request waited for its row lock.
                    sale = Sale.objects.for_business(request.business).filter(
                        checkout_token=checkout_token,
                        cashier=request.user,
                        branch=branch,
                        customer=customer,
                    ).first()
                    if sale is None:
                        raise SaleError("This held sale is no longer available.")
                else:
                    if held.branch_id != branch.id:
                        raise SaleError(
                            "Resume this held sale from its original branch."
                        )
                    held_token = str(
                        (held.cart or {}).get("checkout_token") or ""
                    ).strip()
                    if held_token and held_token != checkout_token:
                        raise SaleError("Invalid checkout token for this held sale.")
            if sale is None:
                sale = services.complete_sale(
                    business=request.business,
                    branch=branch,
                    warehouse=warehouse,
                    cashier=request.user,
                    customer=customer,
                    items=items,
                    payments=payments,
                    membership=request.membership,
                    register=shift.register if shift else None,
                    shift=shift,
                    invoice_discount=D(payload.get("invoice_discount")),
                    notes=str(payload.get("notes", ""))[:1000],
                    delivery_date=delivery_date,
                    priority=_checkout_priority(payload, raw_items),
                    checkout_token=checkout_token,
                    request=request,
                )
            if held is not None:
                held.delete()
    except ModuleAccessDenied:
        raise
    except (SaleError, subscriptions.LimitExceeded,
            subscriptions.SubscriptionInactive) as exc:
        return _sale_error_response(exc)
    except IntegrityError:
        # A concurrent retry with the same tenant-unique checkout token can
        # win the race.  The losing atomic transaction has rolled back fully;
        # return the already-completed sale instead of deducting stock again.
        sale = (
            Sale.objects.for_business(request.business)
            .filter(
                checkout_token=checkout_token,
                cashier=request.user,
                branch=branch,
                customer=customer,
            )
            .first()
            if checkout_token else None
        )
        if sale is None:
            return JsonResponse(
                {"ok": False, "error": "The sale could not be completed."},
                status=400,
            )
    except Exception as exc:  # ValidationError from inventory etc.
        msg = "; ".join(getattr(exc, "messages", [str(exc)]))
        return JsonResponse({"ok": False, "error": msg}, status=400)

    return _checkout_success_response(sale)


@require_POST
@module_permission_required("pos_core", "sales.create")
def pos_hold(request):
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid request."}, status=400)
    cart = payload.get("cart") or {}
    if not cart.get("items"):
        return JsonResponse({"ok": False, "error": "Cart is empty."}, status=400)
    checkout_token = str(cart.get("checkout_token") or "").strip()
    if not checkout_token or len(checkout_token) > 64:
        return JsonResponse(
            {"ok": False, "error": "A valid checkout token is required."},
            status=400,
        )
    from apps.branches.models import Branch

    try:
        branch = Branch.objects.for_business(request.business).get(
            pk=payload.get("branch_id"), is_active=True
        )
    except Branch.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Invalid branch."}, status=400)
    if not request.membership.can_access_branch(branch):
        return JsonResponse(
            {"ok": False, "error": "You cannot hold a sale for this branch."},
            status=403,
        )
    held = services.hold_sale(
        business=request.business,
        branch=branch,
        cashier=request.user,
        label=str(payload.get("label", ""))[:80],
        cart=cart,
        membership=request.membership,
        request=request,
    )
    return JsonResponse({"ok": True, "held_id": held.pk})


@module_permission_required("pos_core", "sales.create")
def pos_held_list(request):
    from apps.catalog.models import Product

    held = list(
        _held_sales_for_request(request)
        .select_related("branch")
        .order_by("-created_at")[:20]
    )
    product_ids = {
        line.get("product_id")
        for h in held
        for line in ((h.cart or {}).get("items", []) if isinstance(h.cart, dict) else [])
        if isinstance(line, dict) and line.get("product_id")
    }
    products = {
        str(product.id): product
        for product in Product.objects.for_business(request.business).filter(
            id__in=product_ids
        ).select_related("unit")
    }
    payload = []
    tailoring_enabled = _tailoring_enabled(request)
    for h in held:
        raw_cart = h.cart if isinstance(h.cart, dict) else {}
        if not tailoring_enabled and any(
            products.get(str(line.get("product_id"))) is not None
            and products[str(line.get("product_id"))].is_tailoring_item
            for line in raw_cart.get("items", [])
            if isinstance(line, dict)
        ):
            continue
        cart = dict(raw_cart)
        cart_items = []
        for raw_line in raw_cart.get("items", []):
            if not isinstance(raw_line, dict):
                continue
            line = dict(raw_line)
            product = products.get(str(line.get("product_id")))
            if product is not None:
                line["is_tailoring_item"] = product.is_tailoring_item
                line["is_meter_tailoring"] = product.is_meter_tailoring
                line["is_legacy_tailoring"] = product.is_legacy_tailoring
            cart_items.append(line)
        cart["items"] = cart_items
        payload.append({
            "id": h.pk,
            "label": h.label or f"Held #{h.pk}",
            "created": h.created_at.strftime("%H:%M"),
            "items": len(cart_items),
            "branch_id": h.branch_id,
            "branch": h.branch.name,
            "cart": cart,
        })
    return JsonResponse({"held": payload})


@require_POST
@module_permission_required("pos_core", "sales.create")
def pos_held_delete(request, pk):
    services.delete_held_sale(
        business=request.business,
        held_id=pk,
        cashier=request.user,
        membership=request.membership,
        request=request,
    )
    return JsonResponse({"ok": True})


# ---------------------------------------------------------------------------
# Sales list / detail / invoice / receipt / void
# ---------------------------------------------------------------------------
def _qs_without_page(request, date_from, date_to):
    encoded = date_range_querystring(request.GET, date_from, date_to)
    return f"{encoded}&" if encoded else ""


@module_permission_required("pos_core", "sales.view")
def sale_list(request):
    qs = (
        _sales_for_request(
            request,
            Sale.objects.for_business(request.business),
        )
        .exclude(status=Sale.Status.DRAFT)
        .select_related("customer", "branch", "cashier")
    )
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(invoice_number__icontains=q) |
                       Q(customer__full_name__icontains=q) |
                       Q(customer__mobile__icontains=q))
    status = request.GET.get("status", "")
    if status:
        qs = qs.filter(status=status)
    branch_id = request.GET.get("branch", "")
    if branch_id.isdigit():
        qs = qs.filter(branch_id=branch_id)
    date_from, date_to = resolve_date_range(request.GET, request.business)
    qs = qs.filter(
        sale_date__date__gte=date_from,
        sale_date__date__lte=date_to,
    )

    # Delivery filters
    from django.utils import timezone as _tz

    today = _tz.localdate()
    tailoring_enabled = _tailoring_enabled(request)
    if tailoring_enabled:
        delivery = request.GET.get("delivery", "")
        open_delivery = ~Q(delivery_status__in=["delivered", "cancelled"])
        if delivery == "today":
            qs = qs.filter(open_delivery, delivery_date=today)
        elif delivery == "upcoming":
            qs = qs.filter(open_delivery, delivery_date__gt=today)
        elif delivery == "overdue":
            qs = qs.filter(open_delivery, delivery_date__lt=today)
        elif delivery == "scheduled":
            qs = qs.filter(delivery_date__isnull=False)

    totals = qs.aggregate(
        total=Sum("total"),
        paid=Sum("amount_paid"),
        discount=Sum("discount_amount"),
    )
    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "sales/list.html", {
        "page_obj": page_obj, "q": q, "active_nav": "sales",
        "statuses": Sale.Status.choices, "branches": _user_branches(request),
        "totals": totals, "date_from": date_from, "date_to": date_to,
        "tailoring_enabled": tailoring_enabled,
        "querystring": _qs_without_page(request, date_from, date_to),
    })


@module_permission_required("pos_core", "sales.view")
def sale_detail(request, public_id):
    sale = get_tenant_object(
        _sales_for_request(
            request,
            Sale.objects.select_related("customer", "branch", "cashier", "register"),
        ),
        request.business, public_id=public_id,
    )
    items = _invoice_display_items(list(
        sale.items.select_related("product__unit", "variant")
    ))
    access_context = get_access_context(request)
    show_tailoring = access_context.has_module("tailoring")
    has_tailoring_jobs = show_tailoring and any(
        item.is_tailoring_line for item in items
    )
    credit_access = evaluate_access(
        request, "customer_credit", action=AccessAction.READ
    ).allowed
    payments = sale.payments.select_related("method", "received_by")
    if not credit_access:
        payments = payments.exclude(
            method__kind__in=[
                PaymentMethod.Kind.CUSTOMER_CREDIT,
                PaymentMethod.Kind.STORE_CREDIT,
            ]
        )
    return_records = list(sale.returns.prefetch_related("items"))
    has_returns = bool(return_records)
    if not credit_access:
        return_records = [
            sale_return
            for sale_return in return_records
            if sale_return.refund_method
            not in {
                SaleReturn.RefundMethod.CUSTOMER_ACCOUNT,
                SaleReturn.RefundMethod.STORE_CREDIT,
            }
        ]
    returns = _invoice_display_returns(return_records)
    settings_obj = request.business.settings
    first_taxed_item = next((item for item in items if item.tax_rate), None)
    vat_rate = first_taxed_item.tax_rate if first_taxed_item else settings_obj.effective_vat_rate
    show_profit = request.membership.has_perm("profit.view")
    can_edit_actual_fabric = (
        show_tailoring
        and access_context.can_write
        and request.membership.has_perm("workshop.fabric_actual")
        and request.membership.can_access_branch(sale.branch)
    )
    can_collect_credit = evaluate_access(
        request,
        "customer_credit",
        permission_code="customers.payments",
        action=AccessAction.WRITE,
    ).allowed
    collect_methods = PaymentMethod.objects.none()
    if can_collect_credit:
        collect_methods = PaymentMethod.objects.for_business(request.business).filter(
            is_active=True
        ).exclude(kind__in=["customer_credit", "store_credit"])
    return render(request, "sales/detail.html", {
        "sale": sale, "items": items, "payments": payments, "returns": returns,
        "active_nav": "sales", "show_profit": show_profit,
        "collect_methods": collect_methods,
        "credit_access": credit_access,
        "has_returns": has_returns,
        "can_collect_credit": can_collect_credit,
        "has_tailoring_jobs": has_tailoring_jobs,
        "show_tailoring": show_tailoring,
        "can_edit_actual_fabric": can_edit_actual_fabric,
        "max_fabric_total": MAX_FABRIC_TOTAL,
        "discounted_subtotal": money(sale.subtotal - sale.discount_amount),
        "invoice_label": "TAX INVOICE" if settings_obj.vat_enabled else "INVOICE",
        "show_vat": bool(settings_obj.show_vat_on_invoice_receipt and (vat_rate or sale.tax_amount)),
        "vat_rate": vat_rate,
    })


@require_POST
@module_permission_required(
    "tailoring", "workshop.fabric_actual", action=AccessAction.WRITE
)
def sale_item_update_fabric(request, public_id, item_id):
    sale = get_tenant_object(
        _sales_for_request(
            request,
            Sale.objects.select_related("branch"),
        ),
        request.business,
        public_id=public_id,
    )
    sale_item = get_tenant_object(
        SaleItem.objects.select_related("sale__branch", "product__unit"),
        request.business,
        pk=item_id,
        sale=sale,
    )
    form = ActualFabricForm(request.POST)
    if not form.is_valid():
        error = next(iter(form.errors.values()))[0]
        messages.error(request, f"Actual fabric was not updated: {error}")
        return redirect("sales:detail", public_id=sale.public_id)
    try:
        services.update_actual_fabric(
            sale_item=sale_item,
            actual_fabric_used=form.cleaned_data["actual_fabric_used"],
            user=request.user,
            membership=request.membership,
            request=request,
        )
    except SaleError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Actual fabric used updated.")
    return redirect("sales:detail", public_id=sale.public_id)


def _invoice_display_items(items):
    for item in items:
        item.display_subtotal = money(
            item.quantity * item.unit_price - item.discount_amount
        )
    return items


def _invoice_display_returns(returns):
    for sale_return in returns:
        sale_return.display_returned_quantity = sum(
            (item.quantity for item in sale_return.items.all()), D("0")
        )
    return returns


def _invoice_status_label(sale):
    if sale.returned_amount > 0:
        return "Refunded"
    if sale.balance <= 0:
        return "Paid"
    if sale.net_amount_paid > 0:
        return "Partially Paid"
    return "Unpaid"


def _ordered_tailoring_items(sale):
    return [
        item
        for item in sale.items.select_related("product__unit", "variant").order_by("id")
        if item.is_tailoring_line
    ]


def _sale_item_position(sale_item, tailoring_items=None):
    tailoring_items = (
        list(tailoring_items)
        if tailoring_items is not None
        else _ordered_tailoring_items(sale_item.sale)
    )
    ids = [item.id for item in tailoring_items]
    if sale_item.id not in ids:
        return 0, len(ids)
    return ids.index(sale_item.id) + 1, len(ids)


def _sale_item_sequence(sale_item):
    """Backward-compatible ordinal, now correctly tailoring-only."""
    sequence, _total = _sale_item_position(sale_item)
    return sequence or 1


def _job_card_data(
    sale,
    request,
    items,
    sale_item=None,
    *,
    job_sequence=None,
    job_total=None,
):
    items = list(items)
    if sale_item is not None:
        items = [sale_item]
    priority_options = {
        "normal": ("Normal", "normal"),
        "high": ("High", "high"),
        "urgent": ("Urgent", "urgent"),
        "vip": ("VIP", "vip"),
    }
    tailoring = sale_item.tailoring_details if sale_item is not None else {}
    legacy_priority = str(tailoring.get("priority") or "").strip().lower()
    priority_key = sale.priority
    if priority_key == Sale.Priority.NORMAL and legacy_priority in priority_options:
        priority_key = legacy_priority
    priority_label, priority_class = priority_options.get(
        priority_key, priority_options["normal"]
    )
    copy_type = request.GET.get("copy", "").strip().lower()
    if copy_type == "copy":
        copy_label = "Copy"
    elif copy_type == "reprint" or sale.reprint_count > 0:
        copy_label = "Reprint"
    else:
        copy_label = "Original"
    if sale_item is not None and (job_sequence is None or job_total is None):
        job_sequence, job_total = _sale_item_position(sale_item)
    sequence = job_sequence or 1
    total = job_total or (1 if sale_item is not None else 0)
    return {
        "sale": sale,
        "items": items,
        "job_item": sale_item,
        "tailoring": tailoring,
        "business": sale.business,
        "more_options": customer_services.more_option_values(
            request.business, sale.customer
        ),
        "job_card_number": f"JC-{sale.invoice_number}-{sequence:02d}",
        "job_card_sequence": sequence,
        "job_card_total": total,
        "job_card_sequence_label": f"{sequence}/{total}" if total else "",
        "workshop_copy_number": sale.reprint_count + 1,
        "copy_type": copy_label,
        "priority_label": priority_label,
        "priority_class": priority_class,
        "job_delivery_date": sale.delivery_date,
    }


def _job_card_context(
    sale,
    request,
    items,
    sale_item=None,
    *,
    job_sequence=None,
    job_total=None,
):
    card = _job_card_data(
        sale,
        request,
        items,
        sale_item=sale_item,
        job_sequence=job_sequence,
        job_total=job_total,
    )
    return {**card, "job_cards": [card]}


def _invoice_context(
    sale,
    *,
    items=None,
    payments=None,
    returns=None,
    is_reprint=False,
    pdf_mode=False,
    show_tailoring=True,
    show_credit=True,
):
    item_source = items if items is not None else sale.items.select_related(
        "product__unit", "variant"
    )
    items = _invoice_display_items(list(item_source))
    payment_source = payments if payments is not None else sale.payments.select_related(
        "method", "received_by"
    )
    payments = list(payment_source)
    if not show_credit:
        payments = [
            payment
            for payment in payments
            if payment.method.kind
            not in {
                PaymentMethod.Kind.CUSTOMER_CREDIT,
                PaymentMethod.Kind.STORE_CREDIT,
            }
        ]
    returns = list(
        returns if returns is not None else sale.returns.prefetch_related("items")
    )
    if not show_credit:
        returns = [
            sale_return
            for sale_return in returns
            if sale_return.refund_method
            not in {
                SaleReturn.RefundMethod.CUSTOMER_ACCOUNT,
                SaleReturn.RefundMethod.STORE_CREDIT,
            }
        ]
    returns = _invoice_display_returns(returns)
    settings_obj = sale.business.settings
    first_taxed_item = next((item for item in items if item.tax_rate), None)
    vat_rate = first_taxed_item.tax_rate if first_taxed_item else settings_obj.effective_vat_rate
    show_vat = bool(settings_obj.show_vat_on_invoice_receipt and (vat_rate or sale.tax_amount))
    status_label = _invoice_status_label(sale)
    return {
        "sale": sale, "items": items, "payments": payments,
        "returns": returns,
        "business": sale.business, "settings_obj": settings_obj,
        "is_reprint": is_reprint, "copy_label": "DUPLICATE COPY" if is_reprint else "ORIGINAL COPY",
        "invoice_label": "TAX INVOICE" if settings_obj.vat_enabled else "INVOICE",
        "invoice_status_label": status_label,
        "invoice_status_class": status_label.lower().replace(" ", "-"),
        "pdf_mode": pdf_mode, "vat_rate": vat_rate,
        "show_vat": show_vat, "vat_number": settings_obj.vat_number,
        "discounted_subtotal": money(sale.subtotal - sale.discount_amount),
        "show_tailoring": show_tailoring,
        "show_credit": show_credit,
    }


def _render_invoice(request, sale, template):
    is_reprint = sale.reprint_count > 0
    return render(
        request,
        template,
        _invoice_context(
            sale,
            is_reprint=is_reprint,
            show_tailoring=_tailoring_enabled(request),
            show_credit=evaluate_access(
                request, "customer_credit", action=AccessAction.READ
            ).allowed,
        ),
    )


@module_permission_required("pos_core", "sales.view")
def sale_invoice(request, public_id):
    sale = get_tenant_object(
        _sales_for_request(
            request,
            Sale.objects.select_related("customer", "branch", "business"),
        ),
        request.business, public_id=public_id,
    )
    return _render_invoice(request, sale, "invoices/invoice_a4.html")


@module_permission_required("pos_core", "sales.view")
def sale_receipt(request, public_id):
    sale = get_tenant_object(
        _sales_for_request(
            request,
            Sale.objects.select_related("customer", "branch", "business", "register"),
        ),
        request.business, public_id=public_id,
    )
    width = sale.register.receipt_printer if sale.register else "80mm"
    template = ("invoices/receipt_58mm.html" if width == "58mm"
                else "invoices/receipt_80mm.html")
    return _render_invoice(request, sale, template)


@module_permission_required("pos_core", "sales.view")
def sale_invoice_pdf(request, public_id):
    from apps.reports.pdf import render_pdf

    sale = get_tenant_object(
        _sales_for_request(
            request,
            Sale.objects.select_related("customer", "branch", "business"),
        ),
        request.business, public_id=public_id,
    )
    items = _invoice_display_items(list(
        sale.items.select_related("product__unit", "variant")
    ))
    payments = sale.payments.select_related("method", "received_by")
    pdf = render_pdf(
        "invoices/invoice_a4.html",
        _invoice_context(
            sale,
            items=items,
            payments=payments,
            is_reprint=False,
            pdf_mode=True,
            show_tailoring=_tailoring_enabled(request),
            show_credit=evaluate_access(
                request, "customer_credit", action=AccessAction.READ
            ).allowed,
        ),
    )
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="invoice-{sale.invoice_number}.pdf"'
    )
    return response


@module_permission_required("tailoring", "sales.view")
def sale_workshop_job_card_pdf(request, public_id):
    from apps.reports.pdf import render_pdf

    sale = get_tenant_object(
        _sales_for_request(
            request,
            Sale.objects.select_related("customer", "branch", "business"),
        ),
        request.business,
        public_id=public_id,
    )
    tailoring_items = _ordered_tailoring_items(sale)
    if not tailoring_items:
        raise Http404("This sale has no tailoring job cards.")
    total = len(tailoring_items)
    cards = [
        _job_card_data(
            sale,
            request,
            [item],
            sale_item=item,
            job_sequence=index,
            job_total=total,
        )
        for index, item in enumerate(tailoring_items, start=1)
    ]
    pdf = render_pdf(
        "invoices/workshop_job_card.html",
        {"job_cards": cards},
    )
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="workshop-job-cards-{sale.invoice_number}.pdf"'
    )
    return response


@module_permission_required("tailoring", "sales.view")
def sale_item_workshop_job_card_pdf(request, public_id, item_id):
    from apps.reports.pdf import render_pdf

    sale = get_tenant_object(
        _sales_for_request(
            request,
            Sale.objects.select_related("customer", "branch", "business"),
        ),
        request.business,
        public_id=public_id,
    )
    sale_item = get_tenant_object(
        SaleItem.objects.select_related("sale", "product__unit", "variant"),
        request.business,
        pk=item_id,
        sale=sale,
    )
    tailoring_items = _ordered_tailoring_items(sale)
    sequence, total = _sale_item_position(sale_item, tailoring_items)
    if not sequence:
        raise Http404("This item is not a tailoring job.")
    pdf = render_pdf(
        "invoices/workshop_job_card.html",
        _job_card_context(
            sale,
            request,
            [sale_item],
            sale_item=sale_item,
            job_sequence=sequence,
            job_total=total,
        ),
    )
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="workshop-job-card-{sale.invoice_number}-'
        f'{sequence:02d}.pdf"'
    )
    return response


@require_POST
@module_permission_required(
    "customer_credit", "customers.payments", action=AccessAction.WRITE
)
def sale_payment_add(request, public_id):
    """Record a later payment against a credit/partially-paid sale."""
    sale = get_tenant_object(
        _sales_for_request(request), request.business, public_id=public_id,
    )
    try:
        subscriptions.require_operational(request.business)
        method = get_tenant_object(PaymentMethod, request.business,
                                   pk=request.POST.get("method_id"))
        payment_date = None
        raw_date = request.POST.get("payment_date", "").strip()
        if raw_date:
            import datetime as _dt

            payment_date = _dt.date.fromisoformat(raw_date)
        shift = register_services.get_open_shift(
            request.business,
            request.user,
            membership=request.membership,
        )
        payment = services.add_sale_payment(
            sale=sale,
            amount=D(request.POST.get("amount")),
            method=method,
            user=request.user,
            payment_date=payment_date,
            reference=request.POST.get("reference", ""),
            notes=request.POST.get("notes", ""),
            shift=shift,
            membership=request.membership,
            request=request,
        )
        messages.success(
            request,
            f"Payment {payment.amount} recorded — balance is now {sale.balance}.",
        )
    except (SaleError, ValueError, subscriptions.SubscriptionInactive) as exc:
        messages.error(request, str(exc))
    return redirect("sales:detail", public_id=public_id)


@require_POST
@module_permission_required("pos_core", "sales.delete")
def sale_delete(request, public_id):
    sale = get_tenant_object(
        _sales_for_request(request), request.business, public_id=public_id,
    )
    try:
        services.delete_sale(
            sale=sale,
            user=request.user,
            membership=request.membership,
            request=request,
        )
        messages.success(request, "Draft sale deleted.")
        return redirect("sales:list")
    except SaleError as exc:
        messages.error(request, str(exc))
        return redirect("sales:detail", public_id=public_id)


@require_POST
@module_permission_required("tailoring", "sales.create")
def sale_set_delivery(request, public_id):
    sale = get_tenant_object(
        _sales_for_request(request), request.business, public_id=public_id,
    )
    try:
        sale = services.set_delivery_status(
            sale=sale,
            status=request.POST.get("delivery_status", ""),
            user=request.user,
            membership=request.membership,
            request=request,
        )
        messages.success(request, f"Delivery status updated to "
                                  f"{sale.get_delivery_status_display()}.")
    except SaleError as exc:
        messages.error(request, str(exc))
    return redirect("sales:detail", public_id=public_id)


@require_POST
@module_permission_required("pos_core", "sales.void")
def sale_void(request, public_id):
    sale = get_tenant_object(
        _sales_for_request(request), request.business, public_id=public_id,
    )
    reason = request.POST.get("reason", "").strip()
    if not reason:
        messages.error(request, "A reason is required to void a sale.")
        return redirect("sales:detail", public_id=public_id)
    try:
        subscriptions.require_operational(request.business)
        services.void_sale(
            sale=sale,
            user=request.user,
            reason=reason,
            membership=request.membership,
            request=request,
        )
        messages.success(request, f"Invoice {sale.invoice_number} voided.")
    except (SaleError, subscriptions.SubscriptionInactive) as exc:
        messages.error(request, str(exc))
    return redirect("sales:detail", public_id=public_id)


# ---------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------
@module_permission_required("pos_core", "sales.refund")
def return_list(request):
    qs = (
        SaleReturn.objects.for_business(request.business)
        .select_related("sale", "customer", "processed_by")
    )
    allowed = request.membership.allowed_branch_ids
    if allowed is not None:
        qs = qs.filter(branch_id__in=allowed)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(return_number__icontains=q) |
                       Q(sale__invoice_number__icontains=q) |
                       Q(customer__full_name__icontains=q))
    date_from, date_to = resolve_date_range(request.GET, request.business)
    qs = qs.filter(
        created_at__date__gte=date_from,
        created_at__date__lte=date_to,
    )
    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "sales/return_list.html", {
        "page_obj": page_obj, "q": q, "active_nav": "returns",
        "date_from": date_from, "date_to": date_to,
        "querystring": _qs_without_page(request, date_from, date_to),
    })


@module_permission_required("pos_core", "sales.refund")
def return_create(request, public_id):
    sale = get_tenant_object(
        _sales_for_request(
            request,
            Sale.objects.select_related("customer", "warehouse"),
        ),
        request.business, public_id=public_id,
    )
    items = list(sale.items.select_related("product", "variant"))
    returnable = [i for i in items if i.returnable_quantity > 0]
    credit_refund_write = evaluate_access(
        request,
        "customer_credit",
        permission_code="sales.refund",
        action=AccessAction.WRITE,
    ).allowed
    refund_methods = list(SaleReturn.RefundMethod.choices)
    if not credit_refund_write:
        refund_methods = [
            choice
            for choice in refund_methods
            if choice[0]
            not in (
                SaleReturn.RefundMethod.STORE_CREDIT,
                SaleReturn.RefundMethod.CUSTOMER_ACCOUNT,
            )
        ]
    if request.method == "POST":
        selected = []
        for item in items:
            raw = request.POST.get(f"qty_{item.pk}", "").strip()
            if raw:
                qty = D(raw)
                if qty > 0:
                    selected.append({
                        "sale_item": item, "quantity": qty,
                        "restock": request.POST.get(f"restock_{item.pk}") == "on",
                    })
        refund_method = request.POST.get("refund_method", "")
        if refund_method not in dict(SaleReturn.RefundMethod.choices):
            messages.error(request, "Choose a refund method.")
        else:
            try:
                subscriptions.require_operational(request.business)
                shift = register_services.get_open_shift(
                    request.business,
                    request.user,
                    membership=request.membership,
                )
                if shift is not None and shift.branch_id != sale.branch_id:
                    shift = None
                sale_return = services.process_return(
                    sale=sale,
                    items=selected,
                    refund_method=refund_method,
                    user=request.user,
                    reason=request.POST.get("reason", ""),
                    restock=True,
                    shift=shift,
                    membership=request.membership,
                    request=request,
                )
                messages.success(
                    request,
                    f"Return {sale_return.return_number} processed — refund "
                    f"{sale_return.refund_amount} via "
                    f"{sale_return.get_refund_method_display()}.",
                )
                return redirect("sales:detail", public_id=sale.public_id)
            except (SaleError, subscriptions.SubscriptionInactive) as exc:
                messages.error(request, str(exc))
            except Exception as exc:
                messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    return render(request, "sales/return_form.html", {
        "sale": sale, "items": returnable, "active_nav": "returns",
        "refund_methods": refund_methods,
    })
